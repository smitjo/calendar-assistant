import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import tools as T
from app.calendar.memory import InMemoryCalendar
from app.settings import Settings, SettingsStore
from app.tools import ToolContext, ToolError

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=TZ)


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    store = SettingsStore(tmp_path / "s.json", Settings(reminder_count=3, lead_minutes=120, timezone="America/Chicago"))
    return ToolContext(calendar=InMemoryCalendar(), settings=store, now=lambda: NOW, settings_url="http://x/settings?key=k")


def test_create_then_list(ctx: ToolContext):
    out = json.loads(T.create_event(ctx, "Dentist", "2026-10-30T15:00"))["created"]
    assert out["start"] == "2026-10-30T15:00-05:00" and out["end"] == "2026-10-30T16:00-05:00"
    assert out["reminders"] == {
        "count": 3, "first_minutes_before": 120, "uses_defaults": True,
        "send_at": ["2026-10-30T13:00-05:00", "2026-10-30T13:40-05:00", "2026-10-30T14:20-05:00"], "already_sent": 0,
    }
    listed = json.loads(T.list_events(ctx, "2026-10-30", "2026-10-31"))
    assert listed["count"] == 1 and listed["events"][0]["uid"] == out["uid"]
    assert json.loads(T.list_events(ctx, "2026-11-01", "2026-11-02"))["count"] == 0


def test_all_day_event(ctx: ToolContext):
    out = json.loads(T.create_event(ctx, "Trash", "2026-11-02", all_day=True))["created"]
    assert out["all_day"] and out["start"].startswith("2026-11-02T00:00") and out["end"].startswith("2026-11-03T00:00")


def test_update_keeps_duration_when_moving(ctx: ToolContext):
    uid = json.loads(T.create_event(ctx, "Dentist", "2026-10-30T15:00", end="2026-10-30T17:00"))["created"]["uid"]
    out = json.loads(T.update_event(ctx, uid, start="2026-10-30T16:00"))["updated"]
    assert out["start"].startswith("2026-10-30T16:00") and out["end"].startswith("2026-10-30T18:00")
    out = json.loads(T.update_event(ctx, uid, title="Dentist (moved)", location="Oak St"))["updated"]
    assert out["title"] == "Dentist (moved)" and out["location"] == "Oak St"


def test_set_reminders_override_and_reset(ctx: ToolContext):
    uid = json.loads(T.create_event(ctx, "Flight", "2026-11-05T06:00"))["created"]["uid"]
    out = json.loads(T.set_reminders(ctx, uid, reminder_count=1, lead_minutes=1440))["updated"]["reminders"]
    assert out["count"] == 1 and out["first_minutes_before"] == 1440 and not out["uses_defaults"]
    out = json.loads(T.set_reminders(ctx, uid, use_defaults=True))["updated"]["reminders"]
    assert out["uses_defaults"] and out["count"] == 3
    with pytest.raises(ToolError):
        T.set_reminders(ctx, uid, reminder_count=2)


def test_delete_and_missing_uid(ctx: ToolContext):
    uid = json.loads(T.create_event(ctx, "Gone", "2026-10-30T15:00"))["created"]["uid"]
    assert json.loads(T.delete_event(ctx, uid))["deleted"]["title"] == "Gone"
    with pytest.raises(ToolError):
        T.delete_event(ctx, uid)


def test_defaults_roundtrip(ctx: ToolContext):
    out = json.loads(T.set_defaults(ctx, reminder_count=1, lead_minutes=30))
    assert out["reminder_count"] == 1 and out["first_reminder_minutes_before"] == 30
    assert out["settings_page"] == "http://x/settings?key=k"
    with pytest.raises(ToolError):
        T.set_defaults(ctx, timezone="Mars/Olympus")


def test_validation_errors(ctx: ToolContext):
    with pytest.raises(ToolError):
        T.create_event(ctx, "", "2026-10-30T15:00")
    with pytest.raises(ToolError):
        T.create_event(ctx, "X", "not a time")
    with pytest.raises(ToolError):
        T.create_event(ctx, "X", "2026-10-30T15:00", end="2026-10-30T14:00")
