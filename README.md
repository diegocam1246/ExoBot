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
/addevent name:"Kickoff meeting" month:9 day:20 time:14:30 location:"Room A-201" notify:"@Alice @Bob"
/events
```

`/addevent` always creates a single-date event (no repeating/annual option)
— `year` is optional and just defaults to the current year if you leave it
out, it doesn't mean "repeat every year." If something genuinely repeats
every year, set that up as an actual recurring event directly on the
calendar (see Calendar setup below) instead of through this command.

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
`user_id` is the member's numeric Discord ID (right-click a member -> Copy User ID; requires Developer Mode on in Discord settings). Re-importing updates existing entries rather than duplicating them. `/exportbirthdays` gives you the reverse (a CSV, not JSON) if you want to check what's currently saved.

**Important:** the bot's Google service account only has *view* access to your
calendars (see setup below for why), so `/addevent` cannot create the event
on any calendar itself — it's purely an announcement + link generator:
1. Posts an announcement to the channel set via `/setchannel`, tagging
   whatever you passed in `notify` (individual `@members` and/or `@roles` —
   whatever you typed, since Discord already turns it into real mention
   markup before the bot sees it) alongside a generic **Google Calendar
   quick-add link**.
2. That link opens the normal "Create event" panel pre-filled with the
   name/date/time, with the destination **Calendar** dropdown left for the
   person opening it to choose. Anyone can use it to add the event to their
   own personal calendar; whoever manages your org's shared calendars can
   open the same link and pick the right team calendar (e.g.
   "Exocet - Chefs") from that dropdown before saving.
3. Attaches a universal `.ics` file (works with Google, Outlook, Apple
   Calendar, etc.) as an alternative to the link, in both the announcement
   and the command's own reply.

`time` (24h `HH:MM`) and `location` are both optional — omit `time` for an
all-day event.

**`notify` carries forward to the day-of reminder too.** If you pass
`notify` to `/addevent`, whoever manages the calendars later saves this same
event (same name, month, day and year) onto one of the team calendars, and
the periodic sync picks it up (every 6 hours — see Calendar setup below),
that synced copy automatically inherits the exact same mention list for its
day-of reminder. You don't need to set it again. This match is done purely
by name + date, so keep the name identical when actually creating it on the
calendar.

If an event was instead created **directly on a calendar** (never went
through `/addevent`, so there's no notify to inherit), the day-of reminder
defaults to tagging whichever Discord **role** has the same name as its
calendar (e.g. an event on the "Chefs" calendar pings your `@Chefs` role —
make sure that role actually exists in your server). Either way, you can
always override who gets tagged for one specific event (its ID comes from
`/events`, available only after the sync has picked the event up):
```
/notifyevent event_id:12 notify:"@Alice @Bob"
```
This replaces that event's mention list entirely (not additive), and takes
priority over both the inherited `/addevent` notify and the calendar-role
default. To bulk-edit across every tracked event at once instead:
```
/addmentionall notify:"@Alice"        (adds Alice to every event's list)
/removementionall notify:"@Alice"     (removes her from every event's list)
```
Careful with `/addmentionall`: any event that was relying on its calendar's
role default now gets an explicit list instead, which won't include that
role unless you add it too. Removing the last person from an event's list
via `/removementionall` reverts it back to the calendar-role default.

## 6. Set up Google Calendar integration (optional)
This uses a single **service account** (a robot Google account), not your
personal login, shared across all your team calendars so you only set it up
once. **The service account only needs (and in practice, for a Workspace
domain with restricted external sharing, may only be *allowed*) view access**
— it reads each calendar periodically to know what's there; it never writes
to Google Calendar itself. Actually creating/editing events on the shared
calendars is a manual, human step (see `/addevent` above).

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
   **Share with specific people** -> add the service account email. Pick the
   highest permission your organization's sharing policy allows you to grant
   to an external account — **"Make changes to events"** if allowed, but
   **"See all event details"** works fine too, since the bot only reads.
   (If your Workspace domain restricts external sharing to view-only, that's
   expected and not a problem here — it's exactly why the read-only design
   above exists.)
7. Still in each calendar's settings, scroll to **Integrate calendar** and
   copy its **Calendar ID** (looks like `abc123@group.calendar.google.com`) —
   not the "Public URL" or embed code further down that same section.
8. Set `GOOGLE_CALENDARS` in `.env` to a JSON object mapping each team's
   display name (matching a Discord role of the same name, used for default
   reminder mentions — see below) to its Calendar ID:
   ```
   GOOGLE_CALENDARS={"Chefs":"abc1@group.calendar.google.com","Embarqué":"abc2@group.calendar.google.com","Général":"abc3@group.calendar.google.com","Mécanique":"abc4@group.calendar.google.com","Énergie":"abc5@group.calendar.google.com","Birthdays":"abc6@group.calendar.google.com"}
   ```
   On Railway, paste this as the value of a single `GOOGLE_CALENDARS`
   variable (Variables tab accepts the raw JSON string directly).

If `GOOGLE_CALENDARS` or the service account file isn't set, calendar
features are silently skipped — everything else keeps working. Adding a new
team later is just adding one more entry to that JSON object (and creating a
matching Discord role for default mentions), no code changes needed.

Every 6 hours the bot checks every configured calendar for whatever's
currently on it and imports anything new into its own database, so it starts
getting Discord reminders — this is the **only** way an event ends up in the
bot's reminder system (see `/addevent` above). Recurring events are
collapsed into a single yearly-repeating entry; one-off events import with
their actual date.

The same sync also catches deletions: if an event is removed directly on a
calendar's website, it disappears from that calendar's listing on the next
sync and gets dropped from the bot's database too, so it stops being
reminded — this is the only way to remove a tracked event, there's no
Discord-side delete command since the bot can't write to the calendar
anyway. A calendar whose listing fails to load that round (e.g. a transient
API error) is skipped for deletion purposes that round, so an outage can't
be mistaken for everything on it having been deleted.

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
