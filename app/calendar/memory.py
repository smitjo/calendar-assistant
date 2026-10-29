"""In-memory calendar: used by the tests and when no iCloud credentials are set."""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any

from .base import Event


class InMemoryCalendar:
    name = "memory"

    def __init__(self) -> None:
        self._events: dict[str, Event] = {}
        self._lock = threading.Lock()

    def list_events(self, start: datetime, end: datetime) -> list[Event]:
        with self._lock:
            out = []
            for ev in self._events.values():
                ev_end = ev.end or ev.start + (timedelta(days=1) if ev.all_day else timedelta(hours=1))
                if ev_end > start and ev.start < end:
                    out.append(copy.deepcopy(ev))
        return sorted(out, key=lambda e: e.start)

    def get_event(self, uid: str) -> Event | None:
        with self._lock:
            ev = self._events.get(uid)
            return copy.deepcopy(ev) if ev else None

    def create_event(self, event: Event) -> Event:
        ev = copy.deepcopy(event)
        ev.uid = ev.uid or str(uuid.uuid4())
        with self._lock:
            self._events[ev.uid] = ev
        return copy.deepcopy(ev)

    def update_event(self, uid: str, changes: dict[str, Any]) -> Event | None:
        with self._lock:
            ev = self._events.get(uid)
            if ev is None:
                return None
            for k, v in changes.items():
                if hasattr(ev, k) and k not in ("uid", "sent"):
                    setattr(ev, k, v)
            return copy.deepcopy(ev)

    def delete_event(self, uid: str) -> bool:
        with self._lock:
            return self._events.pop(uid, None) is not None

    def mark_sent(self, uid: str, key: str) -> None:
        with self._lock:
            ev = self._events.get(uid)
            if ev is not None:
                ev.sent.add(key)
