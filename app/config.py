"""Environment configuration. Everything comes from env vars (or a local .env)."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_TZ = "America/New_York"


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def valid_timezone(name: str, fallback: str = DEFAULT_TZ) -> str:
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return fallback


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    icloud_username: str
    icloud_password: str
    icloud_calendar: str
    calendar_backend: str  # "icloud" or "memory"
    claude_model: str
    settings_key: str
    timezone: str
    default_reminder_count: int
    default_lead_minutes: int
    public_url: str
    polling: bool
    data_dir: Path

    @property
    def webhook_secret(self) -> str:
        """Stable secret Telegram echoes back on every webhook call."""
        return hashlib.sha256(f"webhook:{self.telegram_token}".encode()).hexdigest()[:40]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def use_webhook(self) -> bool:
        return bool(self.public_url) and not self.polling


def load_config() -> Config:
    token = _env("TELEGRAM_BOT_TOKEN")
    count = _int("DEFAULT_REMINDER_COUNT", 3)
    backend = _env("CALENDAR_BACKEND", "icloud").lower()
    if backend == "icloud" and not (_env("ICLOUD_USERNAME") and _env("ICLOUD_APP_PASSWORD")):
        backend = "memory"
    return Config(
        telegram_token=token,
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        icloud_username=_env("ICLOUD_USERNAME"),
        icloud_password=_env("ICLOUD_APP_PASSWORD"),
        icloud_calendar=_env("ICLOUD_CALENDAR"),
        calendar_backend=backend,
        claude_model=_env("CLAUDE_MODEL") or DEFAULT_MODEL,
        settings_key=_env("SETTINGS_KEY") or hashlib.sha256(f"settings:{token}".encode()).hexdigest()[:24],
        timezone=valid_timezone(_env("TZ") or DEFAULT_TZ),
        default_reminder_count=count if count in (1, 3) else 3,
        default_lead_minutes=max(1, _int("DEFAULT_LEAD_MINUTES", 120)),
        public_url=(_env("PUBLIC_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/"),
        polling=_env("POLLING", "0").lower() in ("1", "true", "yes"),
        data_dir=Path(_env("DATA_DIR") or "./data"),
    )
