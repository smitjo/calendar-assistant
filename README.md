# Calendar assistant

A personal AI you text on Telegram. It keeps your Apple Calendar and texts you reminders before events: either one reminder or a series of three, chosen from a dropdown.

- **Chat:** Telegram bot (free). Say things like "dentist Thursday at 3pm", "what's on tomorrow?", "move the dentist to 4", "remind me 3 times for that, starting a day before".
- **Calendar:** your iCloud calendar over CalDAV, so events show up on your iPhone immediately.
- **Reminders:** you pick the lead time for the first text. With 3 reminders the other two are spaced evenly up to the event (3 h lead → 3 h, 2 h, 1 h before). With 1 reminder you only get the first.
- **Brain:** Claude, billed to the API credit included with a Max subscription.
- **Hosting:** Render free tier, kept awake by a free pinger. Nothing is stored on the server except the dropdown defaults; reminder state lives inside the calendar events themselves.

## One-time setup (about 20 minutes, all in a browser)

### 1. Telegram bot
1. Install Telegram, sign in with your phone number.
2. Message **@BotFather**, send `/newbot`, pick a name and a username ending in `bot`.
3. Copy the token it gives you. That is `TELEGRAM_BOT_TOKEN`.

### 2. iCloud app-specific password
1. Go to <https://account.apple.com> → Sign-In and Security → App-Specific Passwords → generate one named "calendar assistant". Two-factor authentication must be on.
2. `ICLOUD_USERNAME` is your Apple Account email; `ICLOUD_APP_PASSWORD` is the generated password.
3. Optional: `ICLOUD_CALENDAR` is the exact name of the calendar to use (for example `Home`). Leave it empty to use your first event calendar.

### 3. Claude API key on your Max credit
1. You must have been on Max for at least 7 days.
2. In claude.ai → Settings → your plan, link one Claude Console organization. Create one at <https://console.anthropic.com> if you have none. No card is needed.
3. In that Console org create an API key. That is `ANTHROPIC_API_KEY`. Usage draws from the monthly credit ($100 on Max 5x, $200 on Max 20x; unused credit does not roll over).

### 4. Deploy to Render (free)
1. Push this folder to a GitHub repository.
2. At <https://render.com> create a **Blueprint** from that repo. `render.yaml` defines the free web service.
3. Fill in the env vars it asks for: the three secrets above, plus `TZ` (your IANA timezone, e.g. `America/Chicago`). Leave `TELEGRAM_CHAT_ID` empty for now.
4. When it is live, open `https://<your-service>.onrender.com/healthz`. You should see `"ok": true` and `"calendar": "icloud"`.

### 5. Pair your phone
Send `/start` to your bot in Telegram. The first chat to do that is paired and saved. To lock it permanently, copy the chat id from the Render logs into `TELEGRAM_CHAT_ID`.

### 6. Keep it awake (free)
Render's free tier sleeps after 15 minutes without a request. Create a free account at <https://cron-job.org> and add a job that fetches `https://<your-service>.onrender.com/tick` **every 5 minutes**. Each hit also runs the reminder check, so reminders go out even if the service had just woken up.

## Using it
- Text the bot naturally. It answers in a line or two and confirms every change with the day, time and reminder plan.
- `/settings` sends you the link to the settings page. The dropdown there sets **Reminders per event: 1 or 3** and the lead time. The same page lists upcoming events with a per-event dropdown to override the default for one event.
- `/forget` clears the chat history (the calendar is untouched). `/help` shows examples.

## Running locally
```bash
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt
cp .env.example .env   # fill in the values
./scripts/dev.sh       # long polling: no public URL needed
```
Check the iCloud connection on its own with `.venv/bin/python scripts/caldav_smoke.py` (add `--write` to create and delete a test event). Run the tests with `.venv/bin/python -m pytest`.

Without iCloud credentials the app falls back to an in-memory calendar so you can try the chat side first.

## How reminders work
- `/tick` and an internal once-a-minute loop both call the reminder check. It loads the next 14 days of events, works out each event's reminder times, sends any that are due and not yet sent, and records the delivery on the event as a custom `X-ASSIST-SENT` property. Nothing is sent twice, even across restarts or redeploys.
- Per-event overrides live on the event as `X-ASSIST-COUNT` and `X-ASSIST-LEAD-MIN`. Events without them follow the dropdown defaults, so changing the dropdown changes every future reminder at once.
- All-day events are treated as starting at 9:00 AM for reminder purposes.
- Reminders are only sent for events that have not started yet.

## Layout
```
app/main.py         FastAPI: /telegram webhook, /tick, /settings, /healthz, polling fallback
app/agent.py        Claude tool-use loop and system prompt
app/tools.py        Calendar tool handlers (testable without the model)
app/reminders.py    Schedule math, message text, the tick
app/calendar/       CalendarBackend interface, iCloud CalDAV, in-memory
app/settings.py     Dropdown defaults (JSON) and the settings page
app/telegram.py     Tiny Telegram Bot API client
tests/              pytest suite (no network)
scripts/            caldav_smoke.py, dev.sh
render.yaml         Render blueprint
```

## Swapping pieces later
- **Another chat channel** (SMS, iMessage via a Mac): replace `app/telegram.py` and the send function in `app/main.py`.
- **Your own calendar instead of iCloud**: implement the five methods in `app/calendar/base.py`.
- **Another model**: change `CLAUDE_MODEL`.
