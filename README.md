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
/setbirthday month:6 day:15 member:@someone
/birthdays
```
`member` is required — `/setbirthday` always sets a specific member's
birthday, there's no "leave empty to set your own" shortcut.

Customize the day-of birthday message per-server (defaults to
`🎂 Happy Birthday {member}! 🎉`):
```
/setbirthdaymessage template:"🎉 Happy Birthday {member}, hope it's a great one!"
```

```
/addevent name:"Team standup" month:9 day:10 notify:"@Chefs"        (uses the current year)
/addevent name:"Anniversary" month:9 day:10 year:2027
/addevent name:"Kickoff meeting" month:9 day:20 time:14:30 duration:90 location:"Room A-201" notify:"@Alice @Bob"
/events
/removeevent event_id:12
```

Everything about events is stored **locally in the bot's own database** —
there's no Google Calendar API integration at all, so nothing needs to be
shared/synced with an external account. `/addevent`:
1. Saves the event locally right away (so it shows up in `/events` and gets
   a day-of Discord reminder — see below).
2. Replies with a generic **Google Calendar quick-add link** — clicking it
   opens the normal "Create event" panel pre-filled with the name/date/time,
   letting whoever opens it add it to whichever of *their own* calendars
   they choose (personal or a shared one they have access to). The bot
   itself never touches Google Calendar's API — this is just a plain URL,
   no auth needed.
3. Also attaches a universal `.ics` file (works with Google, Outlook, Apple
   Calendar, etc.) as an alternative to the link.
4. Posts the same announcement + link to the channel set via `/setchannel`,
   tagging whatever you passed in `notify`.

`/addevent` always creates a single-date event (no repeating/annual option)
— `year` is optional and just defaults to the current year if left out, it
doesn't mean "repeat every year." `time` (24h `HH:MM`), `duration` (minutes,
only relevant with `time`, defaults to 60), and `location` are all optional.

`/removeevent event_id:12` deletes an event from the bot's local tracking
(its ID comes from `/events`) — this is now the only way to remove one,
since events are no longer tied to any external calendar to sync deletions
from.

Typical setup: run `/setchannel` pointing at a **public** announcement channel,
then run `/addevent` from a separate **private/admin-only** channel (that
restriction is just normal Discord channel permissions, nothing to configure
in the bot) — the command's reply goes to wherever you ran it, but the actual
announcement always posts to the channel from `/setchannel`, regardless of
where the command was run.

To bulk-load birthdays instead of setting them one by one, attach a JSON file to `/importbirthdays` — a list of `{"user_id": ..., "month": ..., "day": ...}` objects:
```json
[
  {"user_id": 123456789012345678, "month": 6, "day": 15},
  {"user_id": 234567890123456789, "month": 3, "day": 2}
]
```
`user_id` is the member's numeric Discord ID (right-click a member -> Copy User ID; requires Developer Mode on in Discord settings). Re-importing updates existing entries rather than duplicating them. `/exportbirthdays` and `/exportevents` give you the reverse (CSV downloads) if you want to check what's currently saved.

Same idea for events — attach a JSON file to `/importevents`, a list of `{"name": ..., "month": ..., "day": ..., "year": optional, "notify": optional}` objects:
```json
[
  {"name": "Team standup", "month": 9, "day": 10},
  {"name": "Anniversary", "month": 9, "day": 10, "year": 2027}
]
```
`year` defaults to the current year if omitted (same as `/addevent`). Each
imported event is created exactly like running `/addevent` would (including
`notify`), just without the calendar link/announcement — it's a straight
data load. Unlike birthdays, re-importing doesn't merge with existing
entries — running it twice creates duplicates, so only import once per file.

**Who gets @mentioned in the day-of reminder:** whatever you passed as
`notify` to `/addevent` — individual `@members` and/or `@roles`, whatever
you typed (Discord turns it into real mention markup before the bot sees
it). That exact list gets tagged every year on the day (or on the one date,
for a one-off event). If you didn't pass `notify` at all, no one gets tagged
beyond the plain reminder text. You can change it after the fact (its ID
comes from `/events`):
```
/notifyevent event_id:12 notify:"@Alice @Bob"
```
This replaces that event's mention list entirely (not additive). To bulk-edit
across every tracked event at once instead:
```
/addmentionall notify:"@Alice"        (adds Alice to every event's list)
/removementionall notify:"@Alice"     (removes her from every event's list)
```

## 6. How reminders work
Every day at `REMINDER_HOUR` (your local time, per `TIMEZONE`), the bot checks
the database and posts to the configured channel:
- `🎂 Happy Birthday @user!` for anyone whose birthday is today (customizable
  per-server via `/setbirthdaymessage template:"..."`, using `{member}`)
- `📅 Reminder: <event> is today!` for any matching event, with whoever's
  concerned tagged after it (customizable per-server via
  `/seteventreminder template:"..."`, using `{name}` and optionally
  `{notify}` — if you don't include `{notify}`, the mention is just appended
  after your message automatically, same as the default)

## 7. Deploying to Railway
1. **Push to GitHub.** `.env` is already listed in `.gitignore` — never
   commit it (it holds your bot token). Only commit `bot.py`,
   `requirements.txt`, `Procfile`, `.gitignore`, `README.md`.
2. **Create a project** at https://railway.app -> **New Project -> Deploy
   from GitHub repo** -> pick this repo. Railway auto-detects it's Python
   (via `requirements.txt`) and reads the `Procfile` to know to run
   `python bot.py` as a background **worker** — not a web server, so it
   won't wait for an HTTP port to open (important: if it's misdetected as a
   "web" service, go to Settings and remove any exposed port / health check).
3. **Set environment variables** — Settings -> Variables -> add each one from
   your local `.env`: `DISCORD_TOKEN`, `BOT_NAME`, `TIMEZONE`, `REMINDER_HOUR`.
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
