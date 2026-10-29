"""User-adjustable defaults (the dropdown) and the settings web page."""
from __future__ import annotations

import html
import json
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar.base import Event
from .config import valid_timezone

ALLOWED_COUNTS = (1, 3)


@dataclass(frozen=True)
class Settings:
    reminder_count: int = 3
    lead_minutes: int = 120
    timezone: str = "America/New_York"
    chat_id: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(valid_timezone(self.timezone))


def normalize(s: Settings) -> Settings:
    count = s.reminder_count if s.reminder_count in ALLOWED_COUNTS else 3
    try:
        lead = max(1, int(s.lead_minutes))
    except (TypeError, ValueError):
        lead = 120
    return replace(s, reminder_count=count, lead_minutes=lead, timezone=valid_timezone(s.timezone), chat_id=str(s.chat_id or ""))


class SettingsStore:
    """Tiny JSON file. Env-derived defaults apply when the file is missing or unreadable."""

    def __init__(self, path: Path, defaults: Settings):
        self._path = path
        self._defaults = normalize(defaults)
        self._lock = threading.Lock()
        self._cache: Settings | None = None

    def load(self) -> Settings:
        with self._lock:
            if self._cache is None:
                self._cache = self._read()
            return self._cache

    def _read(self) -> Settings:
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return self._defaults
        if not isinstance(data, dict):
            return self._defaults
        clean = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
        return normalize(replace(self._defaults, **clean))

    def update(self, **changes) -> Settings:
        with self._lock:
            current = self._cache or self._read()
            new = normalize(replace(current, **changes))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(new), indent=2))
            tmp.replace(self._path)
            self._cache = new
            return new


# ----------------------------------------------------------------------------
# Settings page
# ----------------------------------------------------------------------------

_CSS = """
:root { --bg:#f6f6f4; --card:#fff; --ink:#1c1c1a; --muted:#6b6b66; --line:#e4e4df; --accent:#2f6fed; }
@media (prefers-color-scheme: dark) { :root { --bg:#141414; --card:#1e1e1e; --ink:#ececea; --muted:#9a9a94; --line:#333; --accent:#5b8dff; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--ink); font:16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width: 760px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
p.sub { color: var(--muted); margin: 0 0 20px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px; }
.row { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: flex-end; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--muted); }
select, input[type=number], input[type=text] { font: inherit; color: var(--ink); background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; min-width: 120px; }
button { font: inherit; background: var(--accent); color: #fff; border: 0; border-radius: 8px; padding: 9px 14px; cursor: pointer; }
.notice { background: #e8f3e8; color: #1f5f2a; border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; }
@media (prefers-color-scheme: dark) { .notice { background: #1d2f1f; color: #9fd9a8; } }
.event { border-top: 1px solid var(--line); padding: 14px 0; }
.event:first-of-type { border-top: 0; padding-top: 0; }
.event .title { font-weight: 600; }
.event .when { color: var(--muted); font-size: 14px; margin-bottom: 8px; }
.empty { color: var(--muted); }
"""


def _fmt_when(ev: Event, tz: ZoneInfo) -> str:
    start = ev.start.astimezone(tz)
    if ev.all_day:
        return start.strftime("%a %b %-d") + " (all day)"
    return start.strftime("%a %b %-d, %-I:%M %p")


def _count_options(selected: int | None, include_default: bool) -> str:
    out = []
    if include_default:
        out.append(f'<option value="default"{" selected" if selected is None else ""}>Use default</option>')
    for c in ALLOWED_COUNTS:
        label = "1 reminder" if c == 1 else f"{c} reminders"
        out.append(f'<option value="{c}"{" selected" if selected == c else ""}>{label}</option>')
    return "".join(out)


def render_settings_page(settings: Settings, events: list[Event], key: str, now: datetime, message: str = "") -> str:
    tz = settings.tz
    k = html.escape(key, quote=True)
    notice = f'<div class="notice">{html.escape(message)}</div>' if message else ""
    rows = []
    for ev in events:
        rows.append(
            f"""
<div class="event">
  <div class="title">{html.escape(ev.summary or "(untitled)")}</div>
  <div class="when">{_fmt_when(ev, tz)}{" · " + html.escape(ev.location) if ev.location else ""}</div>
  <form method="post" action="/settings" class="row">
    <input type="hidden" name="key" value="{k}">
    <input type="hidden" name="action" value="event">
    <input type="hidden" name="uid" value="{html.escape(ev.uid, quote=True)}">
    <label>Reminders <select name="reminder_count">{_count_options(ev.reminder_count, True)}</select></label>
    <label>First reminder (minutes before, blank = default)
      <input type="number" name="lead_minutes" min="1" step="1" value="{ev.lead_minutes if ev.lead_minutes else ""}" placeholder="{settings.lead_minutes}">
    </label>
    <button type="submit">Save</button>
  </form>
</div>"""
        )
    events_html = "".join(rows) if rows else '<p class="empty">No events in the next two weeks.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reminder settings</title><style>{_CSS}</style></head>
<body><main>
<h1>Reminder settings</h1>
<p class="sub">It is {now.astimezone(tz).strftime("%A %b %-d, %-I:%M %p")} in {html.escape(settings.timezone)}.</p>
{notice}
<div class="card">
<form method="post" action="/settings" class="row">
  <input type="hidden" name="key" value="{k}">
  <input type="hidden" name="action" value="global">
  <label>Reminders per event <select name="reminder_count">{_count_options(settings.reminder_count, False)}</select></label>
  <label>First reminder (minutes before) <input type="number" name="lead_minutes" min="1" step="1" value="{settings.lead_minutes}"></label>
  <label>Timezone <input type="text" name="timezone" value="{html.escape(settings.timezone, quote=True)}"></label>
  <button type="submit">Save defaults</button>
</form>
<p class="sub" style="margin:12px 0 0">With 3 reminders the texts arrive at the lead time, then two more evenly spaced up to the event. With 1 reminder you get only the first.</p>
</div>
<h2>Upcoming events</h2>
<div class="card">{events_html}</div>
</main></body></html>"""
