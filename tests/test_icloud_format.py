"""Pure iCalendar round-trips for the iCloud backend (no network)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar

from app.calendar.base import Event
from app.calendar.icloud import X_SENT, apply_event, build_ical, parse_component

TZ = ZoneInfo("America/New_York")


def _roundtrip(ev: Event) -> Event:
    cal = ICalendar.from_ical(build_ical(ev))
    comp = cal.walk("VEVENT")[0]
    return parse_component(comp, TZ)


def test_timed_event_roundtrip_keeps_custom_properties():
    start = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)
    ev = Event(uid="u1", summary="Dentist", start=start, end=start + timedelta(minutes=90), location="Main St",
               notes="bring card", reminder_count=3, lead_minutes=120, sent={f"{start.isoformat()}/0"})
    back = _roundtrip(ev)
    assert back.uid == "u1" and back.summary == "Dentist" and back.location == "Main St" and back.notes == "bring card"
    assert back.start == start and back.end == start + timedelta(minutes=90) and not back.all_day
    assert back.reminder_count == 3 and back.lead_minutes == 120 and back.sent == {f"{start.isoformat()}/0"}


def test_all_day_roundtrip():
    start = datetime(2026, 11, 2, 0, 0, tzinfo=TZ)
    back = _roundtrip(Event(uid="u2", summary="Trash", start=start, all_day=True))
    assert back.all_day and back.start == start and back.end == start + timedelta(days=1)
    assert back.reminder_count is None and back.lead_minutes is None and back.sent == set()


def test_apply_event_replaces_sent_markers_without_duplicates():
    start = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)
    ev = Event(uid="u3", summary="A", start=start, sent={"k1", "k2"})
    cal = ICalendar.from_ical(build_ical(ev))
    comp = cal.walk("VEVENT")[0]
    ev.sent.add("k3")
    apply_event(comp, ev)
    assert str(comp.get(X_SENT)) == "k1|k2|k3"
    assert len([k for k in comp.keys() if k.upper() == X_SENT]) == 1
