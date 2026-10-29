"""Calendar backend interface. The app only talks to calendars through this."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

SENT_SEP = "|"


class CalendarError(Exception):
    """Raised when the calendar server cannot be reached or rejects a change."""


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime  # timezone-aware
    end: datetime | None = None
    all_day: bool = False
    location: str = ""
    notes: str = ""
    reminder_count: int | None = None  # None = use the global default
    lead_minutes: int | None = None  # None = use the global default
    sent: set[str] = field(default_factory=set)  # reminder keys already delivered


def encode_sent(sent: set[str]) -> str:
    return SENT_SEP.join(sorted(sent))


def decode_sent(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {part for part in str(raw).split(SENT_SEP) if part}


class CalendarBackend(Protocol):
    name: str

    def list_events(self, start: datetime, end: datetime) -> list[Event]: ...

    def get_event(self, uid: str) -> Event | None: ...

    def create_event(self, event: Event) -> Event: ...

    def update_event(self, uid: str, changes: dict[str, Any]) -> Event | None: ...

    def delete_event(self, uid: str) -> bool: ...

    def mark_sent(self, uid: str, key: str) -> None: ...
