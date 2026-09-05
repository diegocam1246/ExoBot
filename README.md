# Birthday & Event Reminder Bot

A Discord bot with a custom name that tracks birthdays and events for a server
and posts daily reminders.

## 1. Create the bot application
1. Go to https://discord.com/developers/applications -> **New Application**.
2. Name it whatever you want (this is just the app name, not necessarily the
   in-server nickname — the bot sets its own nickname per server at runtime).
3. Go to **Bot** in the sidebar -> **Reset Token** -> copy it.
4. Under **Privileged Gateway Intents**, enable **Server Members Intent**.

## 2. Invite it to your server
1. Go to **OAuth2 -> URL Generator**.
2. Scopes: `bot`, `applications.commands`.
3. Bot permissions: `Send Messages`, `Read Message History`, `Manage Nickname`,
   `Use Slash Commands`.
4. Open the generated URL and add the bot to your server.

## 3. Configure and run
```bash
cd birthday-bot
pip install -r requirements.txt
cp .env.example .env
# edit .env: paste your token, set BOT_NAME to whatever custom name you want,
# set your TIMEZONE and preferred REMINDER_HOUR
python bot.py
```

On startup the bot automatically sets its **server nickname** to `BOT_NAME`
from your `.env` — that's the "custom name" shown in the member list and
messages. (Discord only lets you change the global *username* twice an hour
via the API, so nickname-per-server is the reliable way to give it a custom
display name instantly.)

## 4. Set up a server
In any channel, run:
```
/setchannel #general
```
(pick whatever channel you want reminders posted in — needs Manage Server
permission to run)

## 5. Add birthdays and events
```
/setbirthday month:6 day:15
/setbirthday month:3 day:2 member:@someone
/birthdays

/addevent name:"Team standup" month:9 day:10 calendar:Chefs
/addevent name:"Anniversary" month:9 day:10 year:2027 calendar:Général   (one-off, won't repeat)
/events
```

To bulk-load birthdays instead of setting them one by one, attach a JSON file to `/importbirthdays` — a list of `{"user_id": ..., "month": ..., "day": ...}` objects:
```json
[
  {"user_id": 123456789012345678, "month": 6, "day": 15},
  {"user_id": 234567890123456789, "month": 3, "day": 2}
]
```
`user_id` is the member's numeric Discord ID (right-click a member -> Copy User ID; requires Developer Mode on in Discord settings). Re-importing updates existing entries rather than duplicating them. `/exportbirthdays` gives you the reverse (a CSV, not JSON) if you want to check what's currently saved.

`calendar` is a required dropdown — pick which team calendar the event belongs to (see setup below for how these are configured).

When `/addevent` runs, it:
1. Creates a matching event on the chosen team's Google Calendar, if configured
   (yearly events get an annual recurrence rule automatically).
2. Generates a personal **"Add to your own calendar"** link (a Google Calendar
   quick-add URL — works instantly for anyone, no setup needed) plus a
   universal `.ics` file attachment that works with Google, Outlook, Apple
   Calendar, or anything else.
3. Posts an announcement to the reminder channel with both of those.

The personal add-link and `.ics` file work even if you never set up the
Google Calendar integration below — that part only affects the shared
server-wide calendar.

The announcement text defaults to `📢 New event added: **<name>** — <when>`.
You can customize it per-server:
```
/seteventmessage template:"🚨 Heads up! **{name}** is happening {when}"
```
`{name}` and `{when}` get filled in automatically. You can also override it
for a single event:
```
/addevent name:"Surprise party" month:10 day:5 announcement:"🎉 Shhh, don't tell them"
```

To notify specific members (rather than the whole channel) about an event, type their `@mentions` into `notify` — they'll get tagged both in the creation announcement and again in the reminder on the day:
```
/addevent name:"Client demo" month:9 day:20 calendar:Général notify:"@Alice @Bob"
```

You can set or change who gets notified for an *existing* event too (its ID comes from `/events`) — useful for events imported from a calendar, which don't have anyone tagged by default:
```
/notifyevent event_id:12 notify:"@Alice @Bob"
```
This replaces the event's notify list entirely (not additive) — always type everyone who should be tagged.

## 6. Set up Google Calendar integration (optional)
This uses a single **service account** (a robot Google account), not your
personal login, so the bot can create events without you re-authenticating —
and it's shared across all your team calendars, so you only set it up once.

1. Go to https://console.cloud.google.com/ and create a project (or reuse one).
2. **APIs & Services -> Library** -> search "Google Calendar API" -> Enable.
3. **APIs & Services -> Credentials -> Create Credentials -> Service Account**.
   Give it any name, no extra roles needed.
4. Open the new service account -> **Keys** tab -> **Add Key -> Create new
   key -> JSON**. This downloads a `.json` file — save it as
   `service_account.json` next to `bot.py` (or point
   `GOOGLE_SERVICE_ACCOUNT_FILE` at wherever you put it).
5. Note the service account's email address (looks like
   `something@your-project.iam.gserviceaccount.com`).
6. For **each** team calendar (Chefs, Embarqué, Général, Mécanique, Énergie,
   Birthdays, ...): open it in Google Calendar -> **Settings and sharing** ->
   **Share with specific people** -> add the service account email with
   **"Make changes to events"** permission.
7. Still in each calendar's settings, scroll to **Integrate calendar** and
   copy its **Calendar ID** (looks like `abc123@group.calendar.google.com`).
8. Set `GOOGLE_CALENDARS` in `.env` to a JSON object mapping each team's
   display name (exactly what you want to show up in the `/addevent`
   dropdown) to its Calendar ID:
   ```
   GOOGLE_CALENDARS={"Chefs":"abc1@group.calendar.google.com","Embarqué":"abc2@group.calendar.google.com","Général":"abc3@group.calendar.google.com","Mécanique":"abc4@group.calendar.google.com","Énergie":"abc5@group.calendar.google.com","Birthdays":"abc6@group.calendar.google.com"}
   ```
   On Railway, paste this as the value of a single `GOOGLE_CALENDARS`
   variable (Variables tab accepts the raw JSON string directly).

If `GOOGLE_CALENDARS` or the service account file isn't set, calendar
features are silently skipped — everything else keeps working. Adding a new
team later is just adding one more entry to that JSON object, no code changes
needed.

Events don't have to come from `/addevent` — every 6 hours the bot also
checks every configured calendar for anything added directly on its website
and imports it, so it gets Discord reminders too, tagged with whichever
calendar it came from. Recurring events are collapsed into a single
yearly-repeating entry; one-off events import with their actual date.
(Attendee tagging via `notify` only applies to events added through
`/addevent`, since the calendar has no concept of Discord members.)

The same sync also catches deletions: if an event (bot-created or
calendar-created) is removed directly on a calendar's website instead of via
`/removeevent`, it disappears from that calendar's listing on the next sync
and gets dropped from the bot's database too, so it stops being reminded. A
calendar whose listing fails to load that round (e.g. a transient API error)
is skipped for deletion purposes, so an outage can't be mistaken for
everything on it having been deleted.

## 7. How reminders work
Every day at `REMINDER_HOUR` (your local time, per `TIMEZONE`), the bot checks
the database and posts to the configured channel:
- `🎂 Happy Birthday @user!` for anyone whose birthday is today
- `📅 Reminder: <event> is today!` for any matching event

## 8. Deploying to Railway
1. **Push to GitHub.** `.env` and `service_account.json` are already listed
   in `.gitignore` — never commit either (they're credentials). Only commit
   `bot.py`, `requirements.txt`, `Procfile`, `.gitignore`, `README.md`.
2. **Create a project** at https://railway.app -> **New Project -> Deploy
   from GitHub repo** -> pick this repo. Railway auto-detects it's Python
   (via `requirements.txt`) and reads the `Procfile` to know to run
   `python bot.py` as a background **worker** — not a web server, so it
   won't wait for an HTTP port to open (important: if it's misdetected as a
   "web" service, go to Settings and remove any exposed port / health check).
3. **Set environment variables** — Settings -> Variables -> add each one from
   your local `.env`: `DISCORD_TOKEN`, `BOT_NAME`, `TIMEZONE`,
   `REMINDER_HOUR`, and if using Calendar: `GOOGLE_CALENDARS` +
   `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the **entire contents** of your
   `service_account.json` key file as the value — this is what lets you use
   Calendar integration without ever committing that file).
4. **Add a Volume for persistence** (important — without this, `reminders.db`
   is wiped on every redeploy since Railway containers are stateless).
   Project -> **+ New -> Volume**, mount it at e.g. `/data`, then set the env
   var `DB_PATH=/data/reminders.db`.
5. **Deploy.** Railway builds and starts the bot automatically. Every future
   `git push` to your default branch triggers a new build + restart.
6. Check **Deployments -> View Logs** to confirm you see `Logged in as
   <BotName>` — that means it connected successfully.

## Notes
- To run this 24/7 without Railway, any small VPS, Fly.io, or a Raspberry Pi
  works too — `python bot.py` just needs to stay running continuously.
- Slash commands can take up to an hour to show up globally the very first
  time; they usually appear within seconds in practice via `bot.tree.sync()`.
