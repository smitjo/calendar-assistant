from pathlib import Path

from app.settings import Settings, SettingsStore, render_settings_page
from app.telegram import parse_update
from datetime import datetime
from zoneinfo import ZoneInfo


def test_store_defaults_update_and_reload(tmp_path: Path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path, Settings(reminder_count=3, lead_minutes=120, timezone="America/New_York"))
    assert store.load().reminder_count == 3
    store.update(reminder_count=1, lead_minutes=45, timezone="Not/AZone", chat_id="123")
    again = SettingsStore(path, Settings()).load()
    assert again.reminder_count == 1 and again.lead_minutes == 45 and again.chat_id == "123"
    assert again.timezone == "America/New_York"  # invalid zone falls back


def test_invalid_count_normalizes(tmp_path: Path):
    store = SettingsStore(tmp_path / "s.json", Settings())
    assert store.update(reminder_count=2).reminder_count == 3


def test_settings_page_renders_dropdown():
    s = Settings(reminder_count=1, lead_minutes=90, timezone="America/New_York")
    html = render_settings_page(s, [], "k", datetime(2026, 10, 28, 9, 0, tzinfo=ZoneInfo("America/New_York")), "Saved.")
    assert '<option value="1" selected>1 reminder</option>' in html
    assert 'value="90"' in html and "Saved." in html and "No events" in html


def test_parse_update():
    upd = {"update_id": 1, "message": {"message_id": 7, "text": "hi", "chat": {"id": 42}, "from": {"first_name": "G", "last_name": "S"}}}
    msg = parse_update(upd)
    assert msg.chat_id == "42" and msg.text == "hi" and msg.sender == "G S" and msg.message_id == 7
    assert parse_update({"update_id": 2, "message": {"chat": {"id": 1}, "sticker": {}}}) is None


def test_get_updates_passes_telegram_timeout_separately(monkeypatch):
    from app.telegram import TelegramClient

    seen = {}

    class FakeResp:
        text = ""

        def json(self):
            return {"ok": True, "result": []}

    client = TelegramClient("t")
    client._http.post = lambda url, json, timeout: (seen.update(url=url, json=json, timeout=timeout), FakeResp())[1]
    assert client.get_updates(5, timeout=25) == []
    assert seen["json"] == {"timeout": 25, "allowed_updates": ["message"], "offset": 5} and seen["timeout"] == 35
