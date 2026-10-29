from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.calendar.base import Event
from app.calendar.memory import InMemoryCalendar
from app.reminders import ReminderService, compute_schedule, due_reminders, format_reminder, reminder_key
from app.settings import Settings, SettingsStore

TZ = ZoneInfo("America/New_York")
START = datetime(2026, 10, 30, 15, 0, tzinfo=TZ)


def test_three_reminders_are_lead_then_even_nudges():
    times = compute_schedule(START, 3, 180)
    assert times == [START - timedelta(hours=3), START - timedelta(hours=2), START - timedelta(hours=1)]


def test_one_reminder_is_just_the_lead():
    assert compute_schedule(START, 1, 120) == [START - timedelta(hours=2)]


def test_due_respects_sent_markers_and_window():
    ev = Event(uid="a", summary="Dentist", start=START, end=START + timedelta(hours=1))
    now = START - timedelta(hours=2, minutes=30)  # between reminder 1 (3h) and 2 (2h)
    due = due_reminders([ev], now, 3, 180, TZ)
    assert [d.index for d in due] == [0]
    ev.sent.add(reminder_key(START, 0))
    assert due_reminders([ev], now, 3, 180, TZ) == []
    now2 = START - timedelta(minutes=30)  # reminders 2 and 3 both overdue, 1 already sent
    assert [d.index for d in due_reminders([ev], now2, 3, 180, TZ)] == [1, 2]


def test_started_events_are_skipped():
    ev = Event(uid="a", summary="Dentist", start=START)
    assert due_reminders([ev], START + timedelta(minutes=1), 3, 180, TZ) == []


def test_per_event_override_beats_defaults():
    ev = Event(uid="a", summary="Flight", start=START, reminder_count=1, lead_minutes=24 * 60)
    now = START - timedelta(hours=23)
    due = due_reminders([ev], now, 3, 60, TZ)
    assert len(due) == 1 and due[0].count == 1


def test_all_day_events_schedule_from_nine_am():
    ev = Event(uid="a", summary="Trash day", start=datetime(2026, 11, 2, 0, 0, tzinfo=TZ), all_day=True)
    now = datetime(2026, 11, 2, 8, 1, tzinfo=TZ)
    due = due_reminders([ev], now, 1, 60, TZ)
    assert len(due) == 1
    assert due[0].fire_at == datetime(2026, 11, 2, 8, 0, tzinfo=TZ)


def test_format_reminder_reads_naturally():
    ev = Event(uid="a", summary="Dentist", start=START, location="Main St")
    ev.sent.add(reminder_key(START, 0))  # the 3 h text already went out
    now = START - timedelta(hours=2)
    d = due_reminders([ev], now, 3, 180, TZ)[0]
    text = format_reminder(d, now, TZ)
    assert text.startswith("⏰ Reminder 2 of 3: Dentist — today at 3:00 PM (in 2 h)")
    assert "📍 Main St" in text


def test_service_tick_sends_once_and_records_marker(tmp_path: Path):
    cal = InMemoryCalendar()
    cal.create_event(Event(uid="a", summary="Dentist", start=START, end=START + timedelta(hours=1)))
    store = SettingsStore(tmp_path / "s.json", Settings(reminder_count=3, lead_minutes=180, timezone="America/New_York"))
    sent: list[str] = []
    svc = ReminderService(cal, sent.append, store)
    now = START - timedelta(hours=3)
    assert svc.run_tick(now) == 1
    assert svc.run_tick(now) == 0
    assert svc.run_tick(now + timedelta(minutes=5)) == 0
    assert svc.run_tick(START - timedelta(hours=2)) == 1
    assert len(sent) == 2 and "1 of 3" in sent[0] and "2 of 3" in sent[1]
    assert cal.get_event("a").sent == {reminder_key(START, 0), reminder_key(START, 1)}


def test_dropdown_change_applies_to_events_without_override(tmp_path: Path):
    cal = InMemoryCalendar()
    cal.create_event(Event(uid="a", summary="Dentist", start=START))
    store = SettingsStore(tmp_path / "s.json", Settings(reminder_count=3, lead_minutes=60, timezone="America/New_York"))
    sent: list[str] = []
    svc = ReminderService(cal, sent.append, store)
    store.update(reminder_count=1)
    assert svc.run_tick(START - timedelta(minutes=60)) == 1
    assert svc.run_tick(START - timedelta(minutes=10)) == 0  # with 1 reminder, no nudges follow
    assert "Reminder:" in sent[0]
