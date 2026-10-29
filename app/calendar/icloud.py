"""Apple Calendar (iCloud) over CalDAV.

Reminder configuration and delivery markers live on the VEVENT itself as
custom X- properties, so the calendar is the only state the app needs.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import caldav
from caldav.lib import error as caldav_error
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from .base import CalendarError, Event, decode_sent, encode_sent

log = logging.getLogger(__name__)

ICLOUD_URL = "https://caldav.icloud.com/"
X_COUNT = "X-ASSIST-COUNT"
X_LEAD = "X-ASSIST-LEAD-MIN"
X_SENT = "X-ASSIST-SENT"
_MANAGED = ("DTSTART", "DTEND", "DURATION", "SUMMARY", "LOCATION", "DESCRIPTION", X_COUNT, X_LEAD, X_SENT)


def _to_aware(value: date | datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Normalise a DTSTART/DTEND value to an aware datetime plus an all-day flag."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value, False
    return datetime.combine(value, time(0), tzinfo=tz), True


def _opt_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def parse_component(comp: IEvent, tz: ZoneInfo) -> Event | None:
    if comp.get("DTSTART") is None:
        return None
    start, all_day = _to_aware(comp.decoded("DTSTART"), tz)
    end: datetime | None = None
    if comp.get("DTEND") is not None:
        end, _ = _to_aware(comp.decoded("DTEND"), tz)
    elif comp.get("DURATION") is not None:
        dur = comp.decoded("DURATION")
        if isinstance(dur, timedelta):
            end = start + dur
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
    return Event(
        uid=str(comp.get("UID", "")),
        summary=str(comp.get("SUMMARY", "")),
        start=start,
        end=end,
        all_day=all_day,
        location=str(comp.get("LOCATION", "") or ""),
        notes=str(comp.get("DESCRIPTION", "") or ""),
        reminder_count=_opt_int(comp.get(X_COUNT)),
        lead_minutes=_opt_int(comp.get(X_LEAD)),
        sent=decode_sent(comp.get(X_SENT)),
    )


def apply_event(comp: IEvent, ev: Event) -> None:
    """Write every managed field of `ev` onto the VEVENT component."""
    for key in _MANAGED:
        comp.pop(key, None)
    if ev.all_day:
        end = ev.end or ev.start + timedelta(days=1)
        comp.add("DTSTART", ev.start.date())
        comp.add("DTEND", end.date() if end.date() > ev.start.date() else ev.start.date() + timedelta(days=1))
    else:
        end = ev.end or ev.start + timedelta(hours=1)
        comp.add("DTSTART", ev.start.astimezone(UTC))
        comp.add("DTEND", end.astimezone(UTC))
    comp.add("SUMMARY", ev.summary or "(untitled)")
    if ev.location:
        comp.add("LOCATION", ev.location)
    if ev.notes:
        comp.add("DESCRIPTION", ev.notes)
    if ev.reminder_count:
        comp.add(X_COUNT, str(ev.reminder_count))
    if ev.lead_minutes:
        comp.add(X_LEAD, str(ev.lead_minutes))
    if ev.sent:
        comp.add(X_SENT, encode_sent(ev.sent))


def build_ical(ev: Event) -> bytes:
    cal = ICalendar()
    cal.add("PRODID", "-//Calendar Assistant//EN")
    cal.add("VERSION", "2.0")
    comp = IEvent()
    comp.add("UID", ev.uid)
    comp.add("DTSTAMP", datetime.now(UTC))
    apply_event(comp, ev)
    cal.add_component(comp)
    return cal.to_ical()


def _master_component(obj) -> IEvent:
    """The VEVENT without a RECURRENCE-ID (the series master), else the first one."""
    comps = [c for c in obj.icalendar_instance.walk("VEVENT")]
    for c in comps:
        if c.get("RECURRENCE-ID") is None:
            return c
    if not comps:
        raise CalendarError("Calendar object has no VEVENT")
    return comps[0]


class ICloudCalendar:
    name = "icloud"

    def __init__(self, username: str, password: str, tz: ZoneInfo, calendar_name: str = ""):
        self._client = caldav.DAVClient(url=ICLOUD_URL, username=username, password=password)
        self._tz = tz
        self._calendar_name = calendar_name
        self._cal: caldav.Calendar | None = None
        self._lock = threading.Lock()

    # -- connection ---------------------------------------------------------
    def _calendar(self) -> caldav.Calendar:
        with self._lock:
            if self._cal is not None:
                return self._cal
            try:
                calendars = self._client.principal().calendars()
            except caldav_error.AuthorizationError as e:
                raise CalendarError("iCloud rejected the username or app-specific password") from e
            except Exception as e:  # network, DNS, 5xx
                raise CalendarError(f"Could not reach iCloud: {e}") from e
            if not calendars:
                raise CalendarError("No calendars found on this iCloud account")
            self._cal = self._pick(calendars)
            log.info("Using iCloud calendar %r", self._cal.name)
            return self._cal

    def _pick(self, calendars: list[caldav.Calendar]) -> caldav.Calendar:
        if self._calendar_name:
            for cal in calendars:
                if (cal.name or "").strip().lower() == self._calendar_name.lower():
                    return cal
            names = ", ".join(repr(c.name) for c in calendars)
            raise CalendarError(f"Calendar {self._calendar_name!r} not found. Available: {names}")
        for cal in calendars:
            try:
                comps = cal.get_supported_components()
            except Exception:
                comps = None
            if comps and "VEVENT" not in comps:
                continue  # e.g. a Reminders (VTODO) list
            return cal
        return calendars[0]

    def list_calendar_names(self) -> list[str]:
        return [c.name or "" for c in self._client.principal().calendars()]

    # -- CalendarBackend ---------------------------------------------------
    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        cal = self._calendar()
        try:
            objs = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:
            raise CalendarError(f"iCloud search failed: {e}") from e
        events: list[Event] = []
        for obj in objs:
            try:
                ev = parse_component(obj.icalendar_component, self._tz)
            except Exception as e:  # one bad event should not hide the rest
                log.warning("Skipping unparsable event %s: %s", getattr(obj, "url", "?"), e)
                continue
            if ev is not None and ev.uid:
                events.append(ev)
        return sorted(events, key=lambda e: e.start)

    def _fetch(self, uid: str):
        cal = self._calendar()
        try:
            return cal.event_by_uid(uid)
        except caldav_error.NotFoundError:
            return None
        except Exception as e:
            raise CalendarError(f"iCloud lookup failed: {e}") from e

    def get_event(self, uid: str) -> Event | None:
        obj = self._fetch(uid)
        if obj is None:
            return None
        return parse_component(_master_component(obj), self._tz)

    def create_event(self, event: Event) -> Event:
        cal = self._calendar()
        event.uid = event.uid or str(uuid.uuid4())
        try:
            cal.save_event(build_ical(event).decode())
        except Exception as e:
            raise CalendarError(f"iCloud refused to create the event: {e}") from e
        return event

    def update_event(self, uid: str, changes: dict[str, Any]) -> Event | None:
        obj = self._fetch(uid)
        if obj is None:
            return None
        comp = _master_component(obj)
        ev = parse_component(comp, self._tz)
        if ev is None:
            return None
        for k, v in changes.items():
            if hasattr(ev, k) and k not in ("uid", "sent"):
                setattr(ev, k, v)
        apply_event(comp, ev)
        self._save(obj)
        return ev

    def delete_event(self, uid: str) -> bool:
        obj = self._fetch(uid)
        if obj is None:
            return False
        try:
            obj.delete()
        except Exception as e:
            raise CalendarError(f"iCloud refused to delete the event: {e}") from e
        return True

    def mark_sent(self, uid: str, key: str) -> None:
        obj = self._fetch(uid)
        if obj is None:
            return
        comp = _master_component(obj)
        sent = decode_sent(comp.get(X_SENT))
        sent.add(key)
        comp.pop(X_SENT, None)
        comp.add(X_SENT, encode_sent(sent))
        self._save(obj)

    def _save(self, obj) -> None:
        try:
            obj.save()
        except Exception as e:
            raise CalendarError(f"iCloud refused the update: {e}") from e
