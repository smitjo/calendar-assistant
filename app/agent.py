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
BINDING_BETA = "thinking-binding-controls-2026-08-01"
MAX_HISTORY_MESSAGES = 40
MAX_TOOL_ITERATIONS = 12

SYSTEM_STABLE = """You are a personal assistant whose main job is the user's calendar. The user talks to you over Telegram text messages from their phone, so for calendar matters reply the way a sharp human assistant would text: short, warm, no markdown symbols (no **, #, or backticks), no bullet lists unless listing several events.

Questions beyond the calendar:
- Calendar is the default. Small talk, scheduling, "what's on", reminders: stay in that short texting mode.
- When the user asks a genuine question or for help with something else (explain a concept, make a study guide, draft a message, work through a decision, math, advice), answer it properly and completely, the way Claude would. Length should match the task: a study guide or explanation can be long, with numbered steps or short labeled sections written in plain text (line breaks and numbers are fine, markdown symbols are not). Do not refuse or deflect because it is not about the calendar, and do not pad it with calendar talk.
- When that is done, drop back to calendar mode. If the answer has a natural calendar hook (an exam date, a deadline), offer once to put it on the calendar, then let it go.
- You have no web access or files. If a question needs current information you do not have, say so plainly and answer from what you know.

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


def _is_user_text(m: dict[str, Any]) -> bool:
    """A real user turn (not a tool_result message)."""
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, str):
        return True
    return bool(c) and isinstance(c, list) and all(getattr(b, "type", None) == "text" or (isinstance(b, dict) and b.get("type") == "text") for b in c)


def _strip_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove thinking blocks. Used only after a trim, when the retained turns are no
    longer a verbatim prefix of the conversation that produced those blocks."""
    out = []
    for m in messages:
        c = m.get("content")
        if m.get("role") == "assistant" and isinstance(c, list):
            kept = [b for b in c if getattr(b, "type", None) not in ("thinking", "redacted_thinking")]
            out.append({"role": "assistant", "content": kept})
        else:
            out.append(m)
    return out


def _trim(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep the tail of the history, cutting only at a user text turn so tool pairs stay
    intact. The API binds a model's thinking blocks to the exact conversation prefix, so
    anything we keep after a cut has its thinking blocks removed (allowed) rather than
    replayed against a changed prefix (rejected)."""
    if len(messages) <= limit:
        return messages
    for i in range(len(messages) - limit, len(messages)):
        if _is_user_text(messages[i]):
            return _strip_thinking(messages[i:])
    # No user turn inside the window (one long tool round): keep the last full exchange.
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_text(messages[i]):
            return _strip_thinking(messages[i:])
    return []


def context_note(now: datetime, settings) -> str:
    return (
        f"[Context: now {now.astimezone(settings.tz).strftime('%A %Y-%m-%d %H:%M')} {settings.timezone}; "
        f"defaults: {settings.reminder_count} reminder(s) per event, first one {settings.lead_minutes} min before.]"
    )


class Assistant:
    def __init__(self, config: Config, ctx: ToolContext, client: anthropic.Anthropic | None = None):
        self._config = config
        self._ctx = ctx
        self._client = client or anthropic.Anthropic(timeout=120.0, max_retries=2)
        self._tools = build_tools(ctx)
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._fallbacks_ok = True
        self._binding_ok = True
        # Frozen for the life of the process: the API binds thinking blocks to the system
        # prompt, so anything that changes per turn goes into the user message instead.
        system = SYSTEM_STABLE
        if ctx.settings_url:
            system += f"\nSettings page (share it when the user wants to change defaults or see the dropdown): {ctx.settings_url}\n"
        self._system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def forget(self, chat_id: str) -> None:
        with self._lock:
            self._history.pop(chat_id, None)

    def reply(self, chat_id: str, text: str) -> str:
        with self._lock:
            messages = list(self._history.get(chat_id, []))
        note = context_note(self._ctx.now(), self._ctx.settings.load())
        messages.append({"role": "user", "content": [{"type": "text", "text": note}, {"type": "text", "text": text}]})
        final, messages = self._run_with_recovery(messages)
        with self._lock:
            self._history[chat_id] = _trim(messages, MAX_HISTORY_MESSAGES)
        if final is None:
            return "I didn't get a response. Try again?"
        if final.stop_reason == "refusal":
            return "I can't help with that one, but I'm happy to keep going on your calendar."
        reply = "\n".join(b.text for b in final.content if getattr(b, "type", "") == "text").strip()
        return reply or "Done."

    def _run_with_recovery(self, messages: list[dict[str, Any]]):
        start = list(messages)
        for attempt in range(3):
            try:
                return self._run(list(start))
            except anthropic.BadRequestError as e:
                msg = str(e).lower()
                if self._fallbacks_ok and "fallback" in msg:
                    log.warning("fallbacks not accepted, retrying without: %s", e)
                    self._fallbacks_ok = False
                elif self._binding_ok and "block_binding" in msg:
                    log.warning("block_binding not accepted, retrying without: %s", e)
                    self._binding_ok = False
                elif "thinking" in msg or "signature" in msg:
                    log.warning("history rejected, retrying without old thinking blocks: %s", e)
                    start = _strip_thinking(start)
                else:
                    raise
        return self._run(list(start))

    def _run(self, messages: list[dict[str, Any]]):
        kwargs: dict[str, Any] = {}
        betas: list[str] = []
        if self._fallbacks_ok:
            betas.append(FALLBACK_BETA)
            kwargs["fallbacks"] = "default"
        if self._binding_ok:
            betas.append(BINDING_BETA)
            kwargs["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        if betas:
            kwargs["betas"] = betas
        runner = self._client.beta.messages.tool_runner(
            model=self._config.claude_model,
            max_tokens=16000,
            system=self._system,
            tools=self._tools,
            messages=messages,
            max_iterations=MAX_TOOL_ITERATIONS,
            output_config={"effort": "low"},
            **kwargs,
        )
        final = None
        for message in runner:
            final = message
            dropped = [t for t in (getattr(message, "input_transformations", None) or []) if getattr(t, "type", "") == "thinking_dropped"]
            if dropped:
                log.warning("API dropped %d stale thinking block(s): %s", len(dropped), dropped[0])
            messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                messages.append(tool_response)
        return final, messages
