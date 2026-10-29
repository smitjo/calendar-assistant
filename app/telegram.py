"""Minimal Telegram Bot API client (plain HTTPS, no framework)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)
MAX_LEN = 4096


@dataclass
class Incoming:
    chat_id: str
    text: str
    message_id: int
    sender: str


def parse_update(update: dict[str, Any]) -> Incoming | None:
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return None
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p) or sender.get("username", "")
    return Incoming(chat_id=str(chat.get("id", "")), text=msg["text"], message_id=int(msg.get("message_id", 0)), sender=name)


class TelegramClient:
    def __init__(self, token: str, timeout: float = 20.0):
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.Client(timeout=timeout)

    def _call(self, method: str, http_timeout: float | None = None, **params: Any) -> Any:
        resp = self._http.post(f"{self._base}/{method}", json=params, timeout=http_timeout)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', resp.text)}")
        return data.get("result")

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def send_message(self, chat_id: str, text: str) -> None:
        text = text or "…"
        for i in range(0, len(text), MAX_LEN):
            self._call("sendMessage", chat_id=chat_id, text=text[i : i + MAX_LEN], disable_web_page_preview=True)

    def send_typing(self, chat_id: str) -> None:
        try:
            self._call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:  # cosmetic only
            pass

    def set_webhook(self, url: str, secret: str) -> None:
        self._call("setWebhook", url=url, secret_token=secret, allowed_updates=["message"], drop_pending_updates=False)

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", drop_pending_updates=False)

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", http_timeout=timeout + 10, **params) or []
