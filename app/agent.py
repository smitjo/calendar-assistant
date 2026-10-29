"""The Claude-powered assistant: one reply per incoming text, with calendar tools."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

import anthropic
from anthropic import beta_tool

from . import tools as T
from .calendar.base import CalendarError
from .config import Config
from .tools import ToolContext, ToolError

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_HISTORY_MESSAGES = 40
MAX_TOOL_ITERATIONS = 12

SYSTEM_STABLE = """You are a personal calendar assistant. The user talks to you over Telegram text messages from their phone, so reply the way a sharp human assistant would text: short, warm, no markdown, no bullet lists unless listing several events.

This is a real conversation, not a form:
- Gather what you need before acting. An event needs at least a title and a start time. If the day or the time is missing ("dentist next week", "lunch with Sam"), ask for it in one short message instead of inventing it. You may ask for two small things at once ("What day and time?") but never a questionnaire.
- Keep the thread. When the user answers your question, carry on with what you were doing. "Yes", "do it", "sure" mean go ahead with what you just proposed. "Move it", "that one", "the dentist" refer to what you were just discussing.
- Be helpful one step ahead, lightly. After adding something, offer one useful follow-up only when it matters, like whether they want a location added, more lead time for an early appointment, or 3 reminders instead of 1. Then let it go if they decline.
- Chat like a person. Answer a thanks or an aside briefly, then come back to the calendar if there is something pending.

What you do:
- Manage the user's Apple Calendar with the tools. Never say an event was added, changed or removed unless the matching tool call succeeded in this turn.
- Reminders: every event gets either 1 or 3 reminder texts. The first lands at the lead time before the event; with 3, the other two are spaced evenly from there up to the start (3 hours lead -> 3 h, 2 h, 1 h before). The user picks the global default from a dropdown on the settings page; set_reminders overrides it for one event when the user asks (e.g. "remind me three times", "just one reminder for this", "start reminding me a day before").
- Interpreting times: use the user's timezone. "Tomorrow at 3" means 3 PM unless they clearly mean morning. Default duration is 1 hour. Plain dates with no time are all-day events.
- When you create or change an event, confirm in one line with the weekday, date, time and the reminder plan, e.g. "Added: Dentist, Thu Oct 30 at 3:00 PM. I'll text you 3 times starting 2 hours before."
- If something is ambiguous in a way that matters (which Tuesday, AM or PM, which of two matching events), ask one short question instead of guessing. Otherwise just do it.
- To answer "what's on today / this week", call list_events for that window and summarize in local time.
- You cannot send texts yourself; the reminder scheduler does that automatically from the calendar, checking once a minute. If a reminder time is already in the past when an event is created or changed (e.g. "a day before" for something tomorrow afternoon), that text goes out within a minute rather than being skipped. Say so when it applies.
- Tool errors come back as text starting with "Error:". Fix the input and retry once, or tell the user plainly what went wrong.
"""


def _run(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except (ToolError, CalendarError) as e:
        return f"Error: {e}"
    except Exception as e:  # keep the loop alive; the model will relay this
        log.exception("tool failed")
        return f"Error: unexpected failure: {e}"


def build_tools(ctx: ToolContext) -> list[Any]:
    @beta_tool
    def list_events(start: str, end: str) -> str:
        """List calendar events in a time window.

        Args:
            start: Window start, ISO 8601 in the user's timezone, e.g. 2026-10-30T00:00.
            end: Window end (exclusive), ISO 8601, e.g. 2026-10-31T00:00.
        """
        return _run(lambda: T.list_events(ctx, start, end))

    @beta_tool
    def create_event(title: str, start: str, end: str | None = None, all_day: bool = False, location: str = "",
                     notes: str = "", reminder_count: int | None = None, lead_minutes: int | None = None) -> str:
        """Create a calendar event.

        Args:
            title: Short event title.
            start: Start, ISO 8601 in the user's timezone (e.g. 2026-10-30T15:00). For all-day events a date like 2026-10-30.
            end: End, ISO 8601. Defaults to one hour after start (or the next day for all-day events).
            all_day: True for an all-day event.
            location: Optional place.
            notes: Optional free-text notes.
            reminder_count: 1 or 3. Omit to use the user's default from the settings dropdown.
            lead_minutes: Minutes before the event for the first reminder. Omit to use the default.
        """
        return _run(lambda: T.create_event(ctx, title, start, end, all_day, location, notes, reminder_count, lead_minutes))

    @beta_tool
    def update_event(uid: str, title: str | None = None, start: str | None = None, end: str | None = None,
                     all_day: bool | None = None, location: str | None = None, notes: str | None = None) -> str:
        """Change an existing event. Only the fields you pass are changed. Moving start keeps the duration unless end is given.

        Args:
            uid: The event uid from list_events.
            title: New title.
            start: New start, ISO 8601 in the user's timezone.
            end: New end, ISO 8601.
            all_day: Switch to/from all-day.
            location: New location (empty string clears it).
            notes: New notes (empty string clears them).
        """
        return _run(lambda: T.update_event(ctx, uid, title, start, end, all_day, location, notes))

    @beta_tool
    def delete_event(uid: str) -> str:
        """Delete an event. Confirm with the user first if more than one event could match what they said.

        Args:
            uid: The event uid from list_events.
        """
        return _run(lambda: T.delete_event(ctx, uid))

    @beta_tool
    def set_reminders(uid: str, reminder_count: int | None = None, lead_minutes: int | None = None,
                      use_defaults: bool = False) -> str:
        """Override how one event is reminded.

        Args:
            uid: The event uid from list_events.
            reminder_count: 1 or 3 texts.
            lead_minutes: Minutes before the event for the first text (e.g. 1440 for a day before).
            use_defaults: True to drop any override and follow the global settings again.
        """
        return _run(lambda: T.set_reminders(ctx, uid, reminder_count, lead_minutes, use_defaults))

    @beta_tool
    def get_defaults() -> str:
        """Read the global reminder defaults (count, lead time, timezone) and the settings page link."""
        return _run(lambda: T.get_defaults(ctx))

    @beta_tool
    def set_defaults(reminder_count: int | None = None, lead_minutes: int | None = None, timezone: str | None = None) -> str:
        """Change the global reminder defaults, the same thing the settings page dropdown does.

        Args:
            reminder_count: 1 or 3.
            lead_minutes: Minutes before an event for the first reminder.
            timezone: IANA timezone name such as America/Chicago.
        """
        return _run(lambda: T.set_defaults(ctx, reminder_count, lead_minutes, timezone))

    return [list_events, create_event, update_event, delete_event, set_reminders, get_defaults, set_defaults]


def _trim(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Drop oldest turns, cutting only at a plain user text message so tool pairs stay intact."""
    if len(messages) <= limit:
        return messages
    for i in range(len(messages) - limit, len(messages)):
        m = messages[i]
        if m["role"] == "user" and isinstance(m["content"], str):
            return messages[i:]
    return messages[-1:] if messages and isinstance(messages[-1].get("content"), str) else []


class Assistant:
    def __init__(self, config: Config, ctx: ToolContext, client: anthropic.Anthropic | None = None):
        self._config = config
        self._ctx = ctx
        self._client = client or anthropic.Anthropic(timeout=120.0, max_retries=2)
        self._tools = build_tools(ctx)
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._fallbacks_ok = True

    def forget(self, chat_id: str) -> None:
        with self._lock:
            self._history.pop(chat_id, None)

    def _system(self) -> list[dict[str, Any]]:
        s = self._ctx.settings.load()
        now: datetime = self._ctx.now()
        volatile = (
            f"Current date and time: {now.astimezone(s.tz).strftime('%A %Y-%m-%d %H:%M')} ({s.timezone}).\n"
            f"Current defaults: {s.reminder_count} reminder(s) per event, first one {s.lead_minutes} minutes before.\n"
            + (f"Settings page (share when the user asks to change defaults or see the dropdown): {self._ctx.settings_url}\n" if self._ctx.settings_url else "")
        )
        return [
            {"type": "text", "text": SYSTEM_STABLE, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": volatile},
        ]

    def reply(self, chat_id: str, text: str) -> str:
        with self._lock:
            messages = list(self._history.get(chat_id, []))
        messages.append({"role": "user", "content": text})
        try:
            final, messages = self._run(messages)
        except anthropic.BadRequestError as e:
            if self._fallbacks_ok and "fallback" in str(e).lower():
                log.warning("fallbacks not accepted, retrying without: %s", e)
                self._fallbacks_ok = False
                final, messages = self._run(messages)
            else:
                raise
        with self._lock:
            self._history[chat_id] = _trim(messages, MAX_HISTORY_MESSAGES)
        if final is None:
            return "I didn't get a response. Try again?"
        if final.stop_reason == "refusal":
            return "I can't help with that one, but I'm happy to keep going on your calendar."
        reply = "\n".join(b.text for b in final.content if getattr(b, "type", "") == "text").strip()
        return reply or "Done."

    def _run(self, messages: list[dict[str, Any]]):
        kwargs: dict[str, Any] = {}
        if self._fallbacks_ok:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        runner = self._client.beta.messages.tool_runner(
            model=self._config.claude_model,
            max_tokens=16000,
            system=self._system(),
            tools=self._tools,
            messages=messages,
            max_iterations=MAX_TOOL_ITERATIONS,
            output_config={"effort": "low"},
            **kwargs,
        )
        final = None
        for message in runner:
            final = message
            messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                messages.append(tool_response)
        return final, messages
