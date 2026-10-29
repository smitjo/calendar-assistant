"""Reminder scheduling: when to text, what to say, and the periodic tick."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .calendar.base import CalendarBackend, CalendarError, Event
from .settings import SettingsStore

log = logging.getLogger(__name__)

ALL_DAY_REMINDER_HOUR = 9  # all-day events are treated as starting at 9:00 local
LOOKAHEAD = timedelta(days=14)


def effective_start(event: Event, tz: ZoneInfo) -> datetime:
    if event.all_day:
        local = event.start.astimezone(tz)
        return local.replace(hour=ALL_DAY_REMINDER_HOUR, minute=0, second=0, microsecond=0)
    return event.start


def compute_schedule(start: datetime, count: int, lead_minutes: int) -> list[datetime]:
    """Send times for `count` reminders. The first is `lead_minutes` before the
    event; the rest are spaced evenly from there up to the event start.

    3 reminders, 3 h lead -> 3 h, 2 h and 1 h before.
    1 reminder -> only the 3 h one.
    """
    count = max(1, int(count))
    lead = timedelta(minutes=max(1, int(lead_minutes)))
    return [start - lead * (count - i) / count for i in range(count)]


def reminder_key(start: datetime, index: int) -> str:
    return f"{start.isoformat()}/{index}"


@dataclass
class DueReminder:
    event: Event
    index: int  # 0-based position in the sequence
    count: int
    fire_at: datetime
    start: datetime  # effective start used for scheduling

    @property
    def key(self) -> str:
        return reminder_key(self.start, self.index)


def due_reminders(events: list[Event], now: datetime, default_count: int, default_lead: int, tz: ZoneInfo) -> list[DueReminder]:
    """Reminders whose time has come, are not yet sent, and whose event has not started."""
    due: list[DueReminder] = []
    for ev in events:
        count = ev.reminder_count or default_count
        lead = ev.lead_minutes or default_lead
        start = effective_start(ev, tz)
        if start <= now:
            continue
        for i, fire_at in enumerate(compute_schedule(start, count, lead)):
            if fire_at <= now and reminder_key(start, i) not in ev.sent:
                due.append(DueReminder(ev, i, count, fire_at, start))
    due.sort(key=lambda d: (d.fire_at, d.start))
    return due


def _relative(delta: timedelta) -> str:
    minutes = int(round(delta.total_seconds() / 60))
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"in {minutes} min"
    hours = minutes / 60
    if hours < 24:
        h = int(hours)
        m = minutes - h * 60
        return f"in {h} h" + (f" {m} min" if m else "")
    days = int(hours // 24)
    return f"in {days} day" + ("s" if days != 1 else "")


def _day_label(when: datetime, now: datetime) -> str:
    d = when.date()
    if d == now.date():
        return "today"
    if d == now.date() + timedelta(days=1):
        return "tomorrow"
    return when.strftime("%A %b %-d")


def format_reminder(d: DueReminder, now: datetime, tz: ZoneInfo) -> str:
    ev = d.event
    local_start = ev.start.astimezone(tz)
    local_now = now.astimezone(tz)
    if ev.all_day:
        when = f"{_day_label(local_start, local_now)} (all day)"
    else:
        when = f"{_day_label(local_start, local_now)} at {local_start.strftime('%-I:%M %p')}"
    head = "Reminder" if d.count == 1 else f"Reminder {d.index + 1} of {d.count}"
    line = f"⏰ {head}: {ev.summary or '(untitled)'} — {when} ({_relative(d.start - now)})"
    if ev.location:
        line += f"\n📍 {ev.location}"
    return line


class ReminderService:
    """Runs once a minute. Safe to call more often; nothing is sent twice."""

    def __init__(self, calendar: CalendarBackend, send: Callable[[str], None], settings: SettingsStore,
                 lookahead: timedelta = LOOKAHEAD, now_fn: Callable[[], datetime] | None = None):
        self._calendar = calendar
        self._send = send
        self._settings = settings
        self._lookahead = lookahead
        self._now = now_fn or (lambda: datetime.now(settings.load().tz))
        self._recent: dict[str, datetime] = {}  # key -> fire_at, guards against a failed mark_sent write
        self._lock = threading.Lock()

    def run_tick(self, now: datetime | None = None) -> int:
        with self._lock:
            now = now or self._now()
            s = self._settings.load()
            tz = s.tz
            try:
                events = self._calendar.list_events(now - timedelta(hours=1), now + self._lookahead)
            except CalendarError as e:
                log.warning("tick: calendar unavailable: %s", e)
                return 0
            sent = 0
            for d in due_reminders(events, now, s.reminder_count, s.lead_minutes, tz):
                if d.key in self._recent:
                    continue
                try:
                    self._send(format_reminder(d, now, tz))
                except Exception as e:
                    log.error("tick: failed to send reminder for %s: %s", d.event.summary, e)
                    continue
                self._recent[d.key] = d.fire_at
                sent += 1
                try:
                    self._calendar.mark_sent(d.event.uid, d.key)
                except CalendarError as e:
                    log.warning("tick: sent but could not record marker for %s: %s", d.event.summary, e)
            cutoff = now - timedelta(days=2)
            self._recent = {k: t for k, t in self._recent.items() if t > cutoff}
            return sent
