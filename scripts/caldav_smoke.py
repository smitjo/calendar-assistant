"""Check the iCloud connection: list calendars and the next 7 days of events.

    .venv/bin/python scripts/caldav_smoke.py            # read-only
    .venv/bin/python scripts/caldav_smoke.py --write    # also create, re-read and delete a test event
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.calendar.base import Event
from app.calendar.icloud import ICloudCalendar
from app.config import load_config


def main() -> int:
    cfg = load_config()
    if not (cfg.icloud_username and cfg.icloud_password):
        print("Set ICLOUD_USERNAME and ICLOUD_APP_PASSWORD in .env first.")
        return 1
    cal = ICloudCalendar(cfg.icloud_username, cfg.icloud_password, cfg.tz, cfg.icloud_calendar)
    print("Calendars:", ", ".join(repr(n) for n in cal.list_calendar_names()))
    now = datetime.now(cfg.tz)
    events = cal.list_events(now, now + timedelta(days=7))
    print(f"Using calendar {cal._calendar().get_display_name()!r}; {len(events)} event(s) in the next 7 days:")
    for ev in events:
        when = ev.start.strftime("%a %b %-d") + (" all day" if ev.all_day else ev.start.strftime(" %-I:%M %p"))
        extra = f" [reminders={ev.reminder_count or 'default'}, lead={ev.lead_minutes or 'default'}, sent={len(ev.sent)}]"
        print(f"  - {when}: {ev.summary}{extra}")
    if "--write" in sys.argv:
        start = now + timedelta(days=1)
        ev = cal.create_event(Event(uid="", summary="Assistant smoke test (safe to delete)", start=start,
                                    end=start + timedelta(minutes=30), reminder_count=3, lead_minutes=60))
        print("Created", ev.uid)
        back = cal.get_event(ev.uid)
        assert back and back.reminder_count == 3 and back.lead_minutes == 60, back
        cal.mark_sent(ev.uid, "test/0")
        assert "test/0" in cal.get_event(ev.uid).sent
        print("Round-trip of custom properties OK; deleting...")
        cal.delete_event(ev.uid)
        print("Deleted. Check your iPhone: the event should have appeared and vanished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
