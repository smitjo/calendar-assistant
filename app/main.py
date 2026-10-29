"""FastAPI app: Telegram webhook (or long polling), reminder tick, settings page."""
from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .agent import Assistant
from .calendar import CalendarError, build_calendar
from .config import load_config
from .reminders import ReminderService
from .settings import Settings, SettingsStore, render_settings_page
from .telegram import Incoming, TelegramClient, parse_update
from .tools import ToolContext

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

config = load_config()
store = SettingsStore(
    config.data_dir / "settings.json",
    Settings(reminder_count=config.default_reminder_count, lead_minutes=config.default_lead_minutes,
             timezone=config.timezone, chat_id=config.telegram_chat_id),
)
calendar = build_calendar(config, store.load().tz)
telegram = TelegramClient(config.telegram_token) if config.telegram_token else None


def settings_url() -> str:
    base = config.public_url or "http://localhost:8000"
    return f"{base}/settings?key={config.settings_key}"


def now() -> datetime:
    return datetime.now(store.load().tz)


ctx = ToolContext(calendar=calendar, settings=store, now=now, settings_url=settings_url())
assistant = Assistant(config, ctx)


def paired_chat_id() -> str:
    return config.telegram_chat_id or store.load().chat_id


def send_to_user(text: str) -> None:
    chat_id = paired_chat_id()
    if not telegram or not chat_id:
        log.info("(no Telegram chat paired) %s", text)
        return
    telegram.send_message(chat_id, text)


reminders = ReminderService(calendar, send_to_user, store, now_fn=now)
_chat_locks: dict[str, threading.Lock] = {}
_chat_locks_guard = threading.Lock()


def _lock_for(chat_id: str) -> threading.Lock:
    with _chat_locks_guard:
        return _chat_locks.setdefault(chat_id, threading.Lock())


HELP = (
    "Text me like you'd text a person:\n"
    "• \"dentist Thursday at 3pm\"\n"
    "• \"what's on tomorrow?\"\n"
    "• \"move the dentist to 4\"\n"
    "• \"remind me 3 times for that, starting a day before\"\n\n"
    "Commands: /settings for the reminder dropdown, /forget to clear our chat history, /help."
)


def handle_message(msg: Incoming) -> None:
    """Runs in a worker thread. Replies via Telegram."""
    if telegram is None:
        return
    text = msg.text.strip()
    paired = paired_chat_id()
    if not paired:
        if text.startswith("/start"):
            store.update(chat_id=msg.chat_id)
            log.info("Paired with Telegram chat %s (%s)", msg.chat_id, msg.sender)
            telegram.send_message(msg.chat_id, f"Hi {msg.sender or 'there'}! I'm your calendar assistant. {HELP}")
        else:
            telegram.send_message(msg.chat_id, "Send /start to pair this chat with your calendar assistant.")
        return
    if msg.chat_id != paired:
        log.warning("Ignoring message from unpaired chat %s", msg.chat_id)
        return
    if text.startswith("/start") or text.startswith("/help"):
        telegram.send_message(msg.chat_id, HELP)
        return
    if text.startswith("/settings"):
        telegram.send_message(msg.chat_id, f"Reminder settings: {settings_url()}")
        return
    if text.startswith("/forget"):
        assistant.forget(msg.chat_id)
        telegram.send_message(msg.chat_id, "Cleared our conversation history. Your calendar is untouched.")
        return
    with _lock_for(msg.chat_id):
        telegram.send_typing(msg.chat_id)
        try:
            reply = assistant.reply(msg.chat_id, text)
        except CalendarError as e:
            log.error("calendar error: %s", e)
            reply = f"I couldn't reach your calendar: {e}"
        except Exception as e:
            log.exception("assistant failed")
            reply = f"Something went wrong on my end ({type(e).__name__}). Try again in a moment."
        telegram.send_message(msg.chat_id, reply)


# ---------------------------------------------------------------------------
# background work
# ---------------------------------------------------------------------------

def _poll_forever(stop: threading.Event) -> None:
    assert telegram is not None
    offset = None
    try:
        telegram.delete_webhook()
    except Exception as e:
        log.warning("deleteWebhook failed: %s", e)
    log.info("Telegram long polling started")
    while not stop.is_set():
        try:
            for update in telegram.get_updates(offset, timeout=25):
                offset = update["update_id"] + 1
                msg = parse_update(update)
                if msg:
                    threading.Thread(target=handle_message, args=(msg,), daemon=True).start()
        except Exception as e:
            log.warning("polling error: %s", e)
            stop.wait(5)


async def _tick_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            n = await asyncio.to_thread(reminders.run_tick)
            if n:
                log.info("tick: sent %d reminder(s)", n)
        except Exception:
            log.exception("tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_poll = threading.Event()
    stop_tick = asyncio.Event()
    poller = None
    if telegram is None:
        log.warning("TELEGRAM_BOT_TOKEN is not set: the web app runs, but nothing can text you.")
    elif config.use_webhook:
        url = f"{config.public_url}/telegram"
        try:
            await asyncio.to_thread(telegram.set_webhook, url, config.webhook_secret)
            log.info("Telegram webhook set to %s", url)
        except Exception as e:
            log.error("setWebhook failed (%s); falling back to long polling", e)
            poller = threading.Thread(target=_poll_forever, args=(stop_poll,), daemon=True)
            poller.start()
    else:
        poller = threading.Thread(target=_poll_forever, args=(stop_poll,), daemon=True)
        poller.start()
    log.info("Calendar backend: %s | model: %s | settings: %s", calendar.name, config.claude_model, settings_url())
    tick_task = asyncio.create_task(_tick_loop(stop_tick))
    try:
        yield
    finally:
        stop_tick.set()
        stop_poll.set()
        tick_task.cancel()


app = FastAPI(title="Calendar assistant", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "calendar": calendar.name, "telegram": telegram is not None,
            "mode": "webhook" if (telegram and config.use_webhook) else "polling", "paired": bool(paired_chat_id())}


@app.post("/telegram")
async def telegram_webhook(request: Request):
    if telegram is None:
        raise HTTPException(503, "Telegram not configured")
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secrets.compare_digest(header, config.webhook_secret):
        raise HTTPException(403, "bad secret")
    update = await request.json()
    msg = parse_update(update)
    if msg:
        asyncio.get_running_loop().run_in_executor(None, handle_message, msg)
    return {"ok": True}


@app.api_route("/tick", methods=["GET", "POST", "HEAD"])
async def tick():
    n = await asyncio.to_thread(reminders.run_tick)
    return {"ok": True, "sent": n}


def _check_key(key: str) -> None:
    if not key or not secrets.compare_digest(key, config.settings_key):
        raise HTTPException(403, "Missing or wrong key. Send /settings to the bot to get the link.")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(key: str = "", saved: str = ""):
    _check_key(key)
    s = store.load()
    t = now()
    try:
        events = await asyncio.to_thread(calendar.list_events, t - timedelta(hours=1), t + timedelta(days=14))
        message = "Saved." if saved else ""
    except CalendarError as e:
        events, message = [], f"Calendar unavailable: {e}"
    return render_settings_page(s, [e for e in events if e.start > t - timedelta(hours=1)], key, t, message)


@app.post("/settings")
async def settings_save(request: Request):
    form = {k: v[-1] for k, v in parse_qs((await request.body()).decode()).items()}
    key = form.get("key", "")
    _check_key(key)
    action = form.get("action", "")
    if action == "global":
        changes = {}
        if form.get("reminder_count"):
            changes["reminder_count"] = int(form["reminder_count"])
        if form.get("lead_minutes"):
            changes["lead_minutes"] = int(form["lead_minutes"])
        if form.get("timezone"):
            changes["timezone"] = form["timezone"].strip()
        store.update(**changes)
    elif action == "event":
        uid = form.get("uid", "")
        raw_count = form.get("reminder_count", "default")
        raw_lead = form.get("lead_minutes", "").strip()
        changes = {
            "reminder_count": None if raw_count == "default" else int(raw_count),
            "lead_minutes": int(raw_lead) if raw_lead else None,
        }
        if changes["reminder_count"] not in (None, 1, 3):
            raise HTTPException(400, "reminder_count must be 1 or 3")
        if changes["lead_minutes"] is not None and changes["lead_minutes"] < 1:
            raise HTTPException(400, "lead_minutes must be at least 1")
        try:
            await asyncio.to_thread(calendar.update_event, uid, changes)
        except CalendarError as e:
            raise HTTPException(502, str(e))
    else:
        raise HTTPException(400, "unknown action")
    return RedirectResponse(f"/settings?key={key}&saved=1", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
