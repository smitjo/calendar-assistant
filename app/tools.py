"""Tool handlers the assistant calls. Plain functions on a context, so tests
can call them directly without the model in the loop."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .calendar.base import CalendarBackend, Event
from .config import valid_timezone
from .reminders import compute_schedule, effective_start
from .settings import ALLOWED_COUNTS, Settings, SettingsStore


class ToolError(ValueError):
    """A problem the model can fix by changing its input."""


@dataclass
class ToolContext:
    calendar: CalendarBackend
    settings: SettingsStore
    now: Callable[[], datetime]
    settings_url: str = ""


def parse_dt(raw: str, tz: ZoneInfo, field: str = "time") -> datetime:
    raw = (raw or "").strip()
    if not raw:
        raise ToolError(f"{field} is required (ISO 8601, e.g. 2026-10-30T15:00)")
    try:
        if len(raw) == 10:
            return datetime.combine(date.fromisoformat(raw), time(0), tzinfo=tz)
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"{field} is not valid ISO 8601: {raw!r}") from e
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _check_count(count: int | None) -> int | None:
    if count is None:
        return None
    if int(count) not in ALLOWED_COUNTS:
        raise ToolError(f"reminder_count must be one of {list(ALLOWED_COUNTS)}")
    return int(count)


def _check_lead(lead: int | None) -> int | None:
    if lead is None:
        return None
    if int(lead) < 1:
        raise ToolError("lead_minutes must be at least 1")
    return int(lead)


def event_to_dict(ev: Event, s: Settings) -> dict[str, Any]:
    tz = s.tz
    count = ev.reminder_count or s.reminder_count
    lead = ev.lead_minutes or s.lead_minutes
    start = effective_start(ev, tz)
    return {
        "uid": ev.uid,
        "title": ev.summary,
        "start": ev.start.astimezone(tz).isoformat(timespec="minutes"),
        "end": ev.end.astimezone(tz).isoformat(timespec="minutes") if ev.end else None,
        "all_day": ev.all_day,
        "location": ev.location,
        "notes": ev.notes,
        "reminders": {
            "count": count,
            "first_minutes_before": lead,
            "uses_defaults": ev.reminder_count is None and ev.lead_minutes is None,
            "send_at": [t.astimezone(tz).isoformat(timespec="minutes") for t in compute_schedule(start, count, lead)],
            "already_sent": len(ev.sent),
        },
    }


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def list_events(ctx: ToolContext, start: str, end: str) -> str:
    s = ctx.settings.load()
    a = parse_dt(start, s.tz, "start")
    b = parse_dt(end, s.tz, "end")
    if b <= a:
        raise ToolError("end must be after start")
    events = ctx.calendar.list_events(a, b)
    return _dump({"count": len(events), "events": [event_to_dict(e, s) for e in events]})


def create_event(ctx: ToolContext, title: str, start: str, end: str | None = None, all_day: bool = False,
                 location: str = "", notes: str = "", reminder_count: int | None = None,
                 lead_minutes: int | None = None) -> str:
    s = ctx.settings.load()
    title = (title or "").strip()
    if not title:
        raise ToolError("title is required")
    start_dt = parse_dt(start, s.tz, "start")
    if all_day:
        start_dt = datetime.combine(start_dt.date(), time(0), tzinfo=s.tz)
        end_dt = parse_dt(end, s.tz, "end") if end else start_dt + timedelta(days=1)
        end_dt = datetime.combine(end_dt.date(), time(0), tzinfo=s.tz)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(days=1)
    else:
        end_dt = parse_dt(end, s.tz, "end") if end else start_dt + timedelta(hours=1)
        if end_dt <= start_dt:
            raise ToolError("end must be after start")
    ev = Event(
        uid=str(uuid.uuid4()), summary=title, start=start_dt, end=end_dt, all_day=all_day,
        location=(location or "").strip(), notes=(notes or "").strip(),
        reminder_count=_check_count(reminder_count), lead_minutes=_check_lead(lead_minutes),
    )
    created = ctx.calendar.create_event(ev)
    return _dump({"created": event_to_dict(created, s)})


def update_event(ctx: ToolContext, uid: str, title: str | None = None, start: str | None = None,
                 end: str | None = None, all_day: bool | None = None, location: str | None = None,
                 notes: str | None = None) -> str:
    s = ctx.settings.load()
    current = ctx.calendar.get_event(uid)
    if current is None:
        raise ToolError(f"No event with uid {uid!r}. Use list_events to find the right uid.")
    changes: dict[str, Any] = {}
    if title is not None:
        if not title.strip():
            raise ToolError("title cannot be empty")
        changes["summary"] = title.strip()
    if all_day is not None:
        changes["all_day"] = all_day
    new_all_day = changes.get("all_day", current.all_day)
    new_start = parse_dt(start, s.tz, "start") if start else current.start
    if start is not None:
        changes["start"] = new_start
    if end is not None:
        changes["end"] = parse_dt(end, s.tz, "end")
    elif start is not None and current.end is not None:
        changes["end"] = new_start + (current.end - current.start)  # keep the duration
    if new_all_day:
        changes["start"] = datetime.combine(new_start.date(), time(0), tzinfo=s.tz)
        e = changes.get("end", current.end) or changes["start"] + timedelta(days=1)
        e = datetime.combine(e.date(), time(0), tzinfo=s.tz)
        changes["end"] = e if e > changes["start"] else changes["start"] + timedelta(days=1)
    elif changes.get("end", current.end) is not None and changes.get("end", current.end) <= changes.get("start", current.start):
        raise ToolError("end must be after start")
    if location is not None:
        changes["location"] = location.strip()
    if notes is not None:
        changes["notes"] = notes.strip()
    if not changes:
        raise ToolError("Nothing to change")
    updated = ctx.calendar.update_event(uid, changes)
    if updated is None:
        raise ToolError(f"No event with uid {uid!r}")
    return _dump({"updated": event_to_dict(updated, s)})


def delete_event(ctx: ToolContext, uid: str) -> str:
    current = ctx.calendar.get_event(uid)
    if current is None:
        raise ToolError(f"No event with uid {uid!r}")
    ctx.calendar.delete_event(uid)
    return _dump({"deleted": {"uid": uid, "title": current.summary}})


def set_reminders(ctx: ToolContext, uid: str, reminder_count: int | None = None, lead_minutes: int | None = None,
                  use_defaults: bool = False) -> str:
    s = ctx.settings.load()
    current = ctx.calendar.get_event(uid)
    if current is None:
        raise ToolError(f"No event with uid {uid!r}")
    if use_defaults:
        changes: dict[str, Any] = {"reminder_count": None, "lead_minutes": None}
    else:
        changes = {}
        if reminder_count is not None:
            changes["reminder_count"] = _check_count(reminder_count)
        if lead_minutes is not None:
            changes["lead_minutes"] = _check_lead(lead_minutes)
        if not changes:
            raise ToolError("Give reminder_count and/or lead_minutes, or use_defaults=true")
    updated = ctx.calendar.update_event(uid, changes)
    if updated is None:
        raise ToolError(f"No event with uid {uid!r}")
    return _dump({"updated": event_to_dict(updated, s)})


def get_defaults(ctx: ToolContext) -> str:
    s = ctx.settings.load()
    return _dump({
        "reminder_count": s.reminder_count,
        "first_reminder_minutes_before": s.lead_minutes,
        "timezone": s.timezone,
        "settings_page": ctx.settings_url or None,
    })


def set_defaults(ctx: ToolContext, reminder_count: int | None = None, lead_minutes: int | None = None,
                 timezone: str | None = None) -> str:
    changes: dict[str, Any] = {}
    if reminder_count is not None:
        changes["reminder_count"] = _check_count(reminder_count)
    if lead_minutes is not None:
        changes["lead_minutes"] = _check_lead(lead_minutes)
    if timezone is not None:
        if valid_timezone(timezone, "") != timezone:
            raise ToolError(f"Unknown timezone {timezone!r}. Use an IANA name like America/Chicago.")
        changes["timezone"] = timezone
    if not changes:
        raise ToolError("Nothing to change")
    ctx.settings.update(**changes)
    return get_defaults(ctx)
