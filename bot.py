"""
Birthday & Event Reminder Bot
-----------------------------
Features:
- Sets a custom bot name (nickname) per server on startup
- /setbirthday, /removebirthday, /birthdays   -> manage birthdays
- /addevent, /removeevent, /events            -> manage one-off/annual events
- /addevent auto-creates a linked Google Calendar event and posts an
  announcement to the reminder channel (customizable via /seteventmessage)
- /setchannel                                 -> pick where reminders/announcements post
- /seteventmessage                            -> set the default new-event announcement text
- Daily background task checks the DB and posts birthday/event reminders

Setup:
1. pip install -r requirements.txt
2. Create a bot at https://discord.com/developers/applications
   - Enable "Message Content Intent" is NOT required (we only use slash commands)
   - Under Bot > Privileged Gateway Intents, enable "Server Members Intent"
     (needed to resolve/display member names nicely)
3. Copy .env.example to .env and fill in DISCORD_TOKEN and BOT_NAME
4. Invite the bot with scopes: bot, applications.commands
   Permissions needed: Send Messages, Read Message History, Manage Nickname
5. (Optional) Set up Google Calendar — see README.md for the full walkthrough
6. Run: python bot.py
"""

import os
import io
import csv
import json
import re
import sqlite3
import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
BOT_NAME = os.getenv("BOT_NAME", "Reminder Bot")  # the "custom name" you want
TIMEZONE = os.getenv("TIMEZONE", "America/Toronto")
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))  # 24h local time to post reminders
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "reminders.db"))

# Google Calendar (optional — if not configured, calendar features are skipped)
# Maps a friendly team name to its Google Calendar ID, e.g.:
# {"Chefs": "abc@group.calendar.google.com", "Énergie": "..."}
try:
    GOOGLE_CALENDARS = json.loads(os.getenv("GOOGLE_CALENDARS") or "{}")
except json.JSONDecodeError:
    print("GOOGLE_CALENDARS is not valid JSON — calendar features disabled.")
    GOOGLE_CALENDARS = {}

# Choices shown in the /addevent calendar dropdown; falls back to a single
# placeholder if no calendars are configured, since Discord requires at
# least one choice.
CALENDAR_CHOICES = [
    app_commands.Choice(name=k, value=k) for k in GOOGLE_CALENDARS.keys()
] or [app_commands.Choice(name="(no calendars configured)", value="")]
# Either point to a local key file (fine for a VPS/Pi)...
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    os.path.join(os.path.dirname(__file__), "service_account.json"),
)
# ...or paste the key file's raw JSON content directly as an env var (needed
# on platforms like Railway where you can't just drop a file next to bot.py
# without committing a secret to your repo).
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

INTENTS = discord.Intents.default()
INTENTS.members = True  # needed to fetch member display names

bot = commands.Bot(command_prefix="!", intents=INTENTS)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS birthdays (
            guild_id INTEGER,
            user_id INTEGER,
            month INTEGER,
            day INTEGER,
            PRIMARY KEY (guild_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            name TEXT,
            month INTEGER,
            day INTEGER,
            year INTEGER,       -- NULL for events that repeat every year
            created_by INTEGER,
            calendar_event_id TEXT,
            calendar_link TEXT
        )"""
    )
    # Migration for DBs created before calendar support was added
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "calendar_event_id" not in existing_cols:
        conn.execute("ALTER TABLE events ADD COLUMN calendar_event_id TEXT")
    if "calendar_link" not in existing_cols:
        conn.execute("ALTER TABLE events ADD COLUMN calendar_link TEXT")
    if "notify_user_ids" not in existing_cols:
        conn.execute("ALTER TABLE events ADD COLUMN notify_user_ids TEXT")
    if "calendar_key" not in existing_cols:
        conn.execute("ALTER TABLE events ADD COLUMN calendar_key TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            event_announcement TEXT   -- custom message template for new events
        )"""
    )
    existing_settings_cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
    if "event_announcement" not in existing_settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN event_announcement TEXT")
    return conn


def get_reminder_channel(guild_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT channel_id FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_announcement_template(guild_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT event_announcement FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ---------------------------------------------------------------------------
# Google Calendar integration (optional)
# ---------------------------------------------------------------------------
_calendar_service = None
_calendar_checked = False


def get_calendar_service():
    """Lazily builds and caches the Google Calendar API client. Returns None
    (and calendar features silently no-op) if it isn't configured. Accepts
    credentials either as raw JSON in GOOGLE_SERVICE_ACCOUNT_JSON (for
    platforms without file uploads, e.g. Railway) or as a key file on disk."""
    global _calendar_service, _calendar_checked
    if _calendar_checked:
        return _calendar_service
    _calendar_checked = True

    if not GOOGLE_CALENDARS:
        print("Google Calendar not configured — skipping calendar event creation.")
        return None

    try:
        scopes = ["https://www.googleapis.com/auth/calendar"]
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        elif os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            creds = service_account.Credentials.from_service_account_file(
                GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
            )
        else:
            print("Google Calendar not configured — skipping calendar event creation.")
            return None
        _calendar_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        print(f"Failed to initialize Google Calendar client: {e}")
        _calendar_service = None

    return _calendar_service


def resolve_event_date(month: int, day: int, year: int | None) -> datetime.date:
    """The concrete calendar date to use for a one-off or next occurrence of
    a yearly event, given today's date."""
    today = datetime.date.today()
    if year:
        return datetime.date(year, month, day)
    event_date = datetime.date(today.year, month, day)
    if event_date < today:
        event_date = datetime.date(today.year + 1, month, day)
    return event_date


def create_calendar_event(calendar_id: str | None, name: str, month: int, day: int, year: int | None):
    """Creates a matching all-day event on the given team calendar.
    One-off events (year given) are a single day; yearly events (year=None)
    get an annual RRULE recurrence. Returns (event_id, html_link) or
    (None, None) if calendar integration isn't set up, no calendar was
    picked, or the call fails."""
    service = get_calendar_service()
    if service is None or not calendar_id:
        return None, None

    event_date = resolve_event_date(month, day, year)

    body = {
        "summary": name,
        "start": {"date": event_date.isoformat()},
        "end": {"date": (event_date + datetime.timedelta(days=1)).isoformat()},
    }
    if not year:
        body["recurrence"] = ["RRULE:FREQ=YEARLY"]

    try:
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
        return created.get("id"), created.get("htmlLink")
    except HttpError as e:
        print(f"Google Calendar API error creating event: {e}")
        return None, None


def delete_calendar_event(calendar_id: str | None, calendar_event_id: str | None):
    if not calendar_id or not calendar_event_id:
        return
    service = get_calendar_service()
    if service is None:
        return
    try:
        service.events().delete(calendarId=calendar_id, eventId=calendar_event_id).execute()
    except HttpError as e:
        print(f"Google Calendar API error deleting event: {e}")


def parse_mentions(text: str | None) -> list[int]:
    """Extracts user IDs from typed @mentions (e.g. '@Alice @Bob')."""
    if not text:
        return []
    return [int(uid) for uid in re.findall(r"<@!?(\d+)>", text)]


def build_csv_file(filename: str, header: list[str], rows: list[list]) -> discord.File:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename)


def fetch_calendar_series() -> tuple[dict, set]:
    """Lists upcoming events across all configured team calendars and
    collapses each recurring series down to a single entry (keyed by
    (calendar_key, series id)), using the next occurrence found in the
    window as that series' reference date. This snapshot is used both to
    pick up events created directly on a calendar's website, and — by
    noticing what's now absent from it — to prune ones deleted the same way.

    Also returns the set of calendar_keys whose listing failed this round,
    so callers can skip treating "absent" as "deleted" for those — a
    transient API error must never be mistaken for everything on that
    calendar having been removed."""
    service = get_calendar_service()
    if service is None:
        return {}, set()

    now = datetime.datetime.utcnow()
    time_min = (now - datetime.timedelta(days=1)).isoformat() + "Z"
    time_max = (now + datetime.timedelta(days=400)).isoformat() + "Z"

    series = {}
    failed_calendars = set()
    for calendar_key, calendar_id in GOOGLE_CALENDARS.items():
        page_token = None
        try:
            while True:
                resp = service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    pageToken=page_token,
                ).execute()
                for ev in resp.get("items", []):
                    if ev.get("status") == "cancelled":
                        continue
                    series_id = ev.get("recurringEventId") or ev["id"]
                    key = (calendar_key, series_id)
                    if key in series:
                        continue  # already recorded this series' next occurrence
                    start = ev["start"].get("date") or ev["start"].get("dateTime")
                    if not start:
                        continue
                    d = datetime.date.fromisoformat(start[:10])
                    is_recurring = "recurringEventId" in ev
                    series[key] = {
                        "name": ev.get("summary") or "Untitled event",
                        "month": d.month,
                        "day": d.day,
                        "year": None if is_recurring else d.year,
                        "calendar_key": calendar_key,
                        "calendar_event_id": series_id,
                        "calendar_link": ev.get("htmlLink"),
                    }
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as e:
            print(f"Google Calendar API error listing events on '{calendar_key}': {e}")
            failed_calendars.add(calendar_key)

    return series, failed_calendars


def personal_add_link(name: str, month: int, day: int, year: int | None) -> str:
    """Builds a Google Calendar 'quick add' URL. Clicking it lets ANY user
    (no login/API needed on our end) add the event to their own personal
    Google Calendar — separate from the shared calendar the bot manages."""
    event_date = resolve_event_date(month, day, year)
    end_date = event_date + datetime.timedelta(days=1)
    params = {
        "action": "TEMPLATE",
        "text": name,
        "dates": f"{event_date.strftime('%Y%m%d')}/{end_date.strftime('%Y%m%d')}",
    }
    if not year:
        params["recur"] = "RRULE:FREQ=YEARLY"
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def build_ics_file(name: str, month: int, day: int, year: int | None) -> discord.File:
    """Builds a universal .ics calendar file (works with Google, Outlook,
    Apple Calendar, etc.) that anyone can download and import directly."""
    event_date = resolve_event_date(month, day, year)
    end_date = event_date + datetime.timedelta(days=1)
    uid = f"{event_date.isoformat()}-{abs(hash(name))}@birthday-bot"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//birthday-bot//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{event_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}",
        f"SUMMARY:{name}",
    ]
    if not year:
        lines.append("RRULE:FREQ=YEARLY")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    ics_bytes = ("\r\n".join(lines) + "\r\n").encode("utf-8")

    safe_name = "".join(c if c.isalnum() else "_" for c in name)[:40] or "event"
    return discord.File(io.BytesIO(ics_bytes), filename=f"{safe_name}.ics")


# ---------------------------------------------------------------------------
# Startup: set the bot's display name (nickname) in each server it's in
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")

    for guild in bot.guilds:
        try:
            me = guild.me
            if me.nick != BOT_NAME:
                await me.edit(nick=BOT_NAME)
                print(f"Set nickname to '{BOT_NAME}' in {guild.name}")
        except discord.Forbidden:
            print(f"Missing 'Manage Nickname' permission in {guild.name}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    if not daily_check.is_running():
        daily_check.start()
    if not sync_calendar_events.is_running():
        sync_calendar_events.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await guild.me.edit(nick=BOT_NAME)
    except discord.Forbidden:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need **Administrator** permission to do that.", ephemeral=True
        )
        return
    # Fall back to logging anything unexpected instead of failing silently
    print(f"Unhandled app command error: {error}")
    if not interaction.response.is_done():
        await interaction.response.send_message(
            "Something went wrong running that command.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Slash commands: setup
# ---------------------------------------------------------------------------
@bot.tree.command(description="Set the channel where birthday/event reminders are posted")
@app_commands.checks.has_permissions(administrator=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, channel_id) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id",
        (interaction.guild_id, channel.id),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Reminders will now be posted in {channel.mention}.", ephemeral=True
    )


@bot.tree.command(description="Set a custom announcement template for new events (use {name} and {when})")
@app_commands.checks.has_permissions(administrator=True)
async def seteventmessage(interaction: discord.Interaction, template: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, event_announcement) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET event_announcement = excluded.event_announcement",
        (interaction.guild_id, template),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"New-event announcements will now use:\n> {template}", ephemeral=True
    )


# ---------------------------------------------------------------------------
# Slash commands: birthdays
# ---------------------------------------------------------------------------
@bot.tree.command(description="Set a member's birthday (Administrator only)")
@app_commands.describe(month="1-12", day="1-31", member="Leave empty to set your own")
@app_commands.checks.has_permissions(administrator=True)
async def setbirthday(
    interaction: discord.Interaction,
    month: app_commands.Range[int, 1, 12],
    day: app_commands.Range[int, 1, 31],
    member: discord.Member = None,
):
    target = member or interaction.user

    try:
        datetime.date(2024, month, day)  # validate, 2024 = leap year so Feb 29 works
    except ValueError:
        await interaction.response.send_message("That's not a real date.", ephemeral=True)
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO birthdays (guild_id, user_id, month, day) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET month=excluded.month, day=excluded.day",
        (interaction.guild_id, target.id, month, day),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Saved {target.display_name}'s birthday as {month:02d}/{day:02d}."
    )


@bot.tree.command(description="Remove a saved birthday (Administrator only)")
@app_commands.checks.has_permissions(administrator=True)
async def removebirthday(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user

    conn = get_db()
    conn.execute(
        "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?",
        (interaction.guild_id, target.id),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"Removed {target.display_name}'s birthday.")


@bot.tree.command(description="List all saved birthdays, soonest first")
async def birthdays(interaction: discord.Interaction):
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id, month, day FROM birthdays WHERE guild_id = ?",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No birthdays saved yet.")
        return

    today = datetime.date.today()

    def days_until(month, day):
        this_year = datetime.date(today.year, month, day) if _valid(today.year, month, day) else None
        next_year = datetime.date(today.year + 1, month, day)
        candidate = this_year if this_year and this_year >= today else next_year
        return (candidate - today).days

    def _valid(y, m, d):
        try:
            datetime.date(y, m, d)
            return True
        except ValueError:
            return False

    rows.sort(key=lambda r: days_until(r[1], r[2]))

    lines = []
    for user_id, month, day in rows:
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else f"<@{user_id}>"
        lines.append(f"**{name}** — {month:02d}/{day:02d} (in {days_until(month, day)} days)")

    await interaction.response.send_message("\n".join(lines))


@bot.tree.command(description="Export all saved birthdays as a CSV file (Administrator only)")
@app_commands.checks.has_permissions(administrator=True)
async def exportbirthdays(interaction: discord.Interaction):
    conn = get_db()
    rows = conn.execute(
        "SELECT user_id, month, day FROM birthdays WHERE guild_id = ? ORDER BY month, day",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No birthdays saved yet.", ephemeral=True)
        return

    csv_rows = []
    for user_id, month, day in rows:
        member = interaction.guild.get_member(user_id)
        name = member.display_name if member else "(left server)"
        csv_rows.append([name, user_id, month, day])

    file = build_csv_file(
        "birthdays.csv", ["Member", "User ID", "Month", "Day"], csv_rows
    )
    await interaction.response.send_message(file=file, ephemeral=True)


@bot.tree.command(description="Bulk-import birthdays from a JSON file (Administrator only)")
@app_commands.describe(
    file='A JSON file: a list of {"user_id": 123456789012345678, "month": 1-12, "day": 1-31}'
)
@app_commands.checks.has_permissions(administrator=True)
async def importbirthdays(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        await interaction.followup.send(f"Couldn't parse that file as JSON: {e}", ephemeral=True)
        return

    if not isinstance(data, list):
        await interaction.followup.send(
            'Expected a JSON array of {"user_id", "month", "day"} objects.', ephemeral=True
        )
        return

    conn = get_db()
    imported = 0
    errors = []
    for i, entry in enumerate(data):
        try:
            user_id = int(entry["user_id"])
            month = int(entry["month"])
            day = int(entry["day"])
            datetime.date(2024, month, day)  # validate, 2024 = leap year so Feb 29 works
        except (KeyError, TypeError, ValueError):
            errors.append(f"Entry {i}: invalid or missing user_id/month/day")
            continue
        conn.execute(
            "INSERT INTO birthdays (guild_id, user_id, month, day) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET month=excluded.month, day=excluded.day",
            (interaction.guild_id, user_id, month, day),
        )
        imported += 1
    conn.commit()
    conn.close()

    summary = f"Imported {imported} birthday(s)."
    if errors:
        shown = errors[:10]
        summary += "\n" + "\n".join(shown)
        if len(errors) > 10:
            summary += f"\n...and {len(errors) - 10} more errors."
    await interaction.followup.send(summary, ephemeral=True)


# ---------------------------------------------------------------------------
# Slash commands: events
# ---------------------------------------------------------------------------
@bot.tree.command(description="Add a reminder event (e.g. tournament, meeting, launch date)")
@app_commands.describe(
    name="What the event is",
    month="1-12",
    day="1-31",
    calendar="Which team calendar to add this event to",
    year="Leave empty for a yearly-repeating event",
    announcement="Optional one-off message for this event (overrides the server template)",
    notify="@mention the specific members this event concerns (they'll be tagged now and on the day)",
)
@app_commands.choices(calendar=CALENDAR_CHOICES)
@app_commands.checks.has_permissions(administrator=True)
async def addevent(
    interaction: discord.Interaction,
    name: str,
    month: app_commands.Range[int, 1, 12],
    day: app_commands.Range[int, 1, 31],
    calendar: app_commands.Choice[str],
    year: int = None,
    announcement: str = None,
    notify: str = None,
):
    try:
        datetime.date(year or 2024, month, day)
    except ValueError:
        await interaction.response.send_message("That's not a real date.", ephemeral=True)
        return

    calendar_key = calendar.value or None
    calendar_id = GOOGLE_CALENDARS.get(calendar_key) if calendar_key else None
    notify_ids = parse_mentions(notify)

    await interaction.response.defer()  # calendar API call can take a moment

    calendar_event_id, calendar_link = create_calendar_event(calendar_id, name, month, day, year)

    conn = get_db()
    conn.execute(
        "INSERT INTO events (guild_id, name, month, day, year, created_by, calendar_event_id, calendar_link, notify_user_ids, calendar_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            interaction.guild_id, name, month, day, year, interaction.user.id,
            calendar_event_id, calendar_link,
            ",".join(str(uid) for uid in notify_ids) or None,
            calendar_key,
        ),
    )
    conn.commit()

    when = f"{month:02d}/{day:02d}" + (f"/{year}" if year else " (yearly)")
    add_link = personal_add_link(name, month, day, year)
    mentions = " ".join(f"<@{uid}>" for uid in notify_ids)

    confirmation = f"Added event **{name}** on {when}" + (f" to **{calendar_key}**." if calendar_key else ".")
    if calendar_link:
        confirmation += f"\n📅 [View on server calendar]({calendar_link})"
    confirmation += f"\n➕ [Add to your own calendar]({add_link})"
    if mentions:
        confirmation += f"\n🔔 Notifying: {mentions}"
    await interaction.followup.send(confirmation, file=build_ics_file(name, month, day, year))

    # Post the announcement to the configured reminder channel, if one is set
    channel_id = get_reminder_channel(interaction.guild_id)
    if channel_id:
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            template = announcement or get_announcement_template(interaction.guild_id)
            if template:
                text = template.replace("{name}", name).replace("{when}", when)
            else:
                text = f"📢 New event added: **{name}** — {when}"
            text += f"\n➕ [Add to your own calendar]({add_link})"
            if mentions:
                text += f"\n🔔 {mentions}"
            await channel.send(text, file=build_ics_file(name, month, day, year))

    conn.close()


@bot.tree.command(description="Set which members get @mentioned for an existing event, replacing any previous list (see /events for IDs)")
@app_commands.describe(
    event_id="The event's ID (see /events)",
    notify="@mention the members this event concerns",
)
@app_commands.checks.has_permissions(administrator=True)
async def notifyevent(interaction: discord.Interaction, event_id: int, notify: str):
    notify_ids = parse_mentions(notify)
    if not notify_ids:
        await interaction.response.send_message(
            "Couldn't find any @mentions in that — make sure to actually @-mention the members.",
            ephemeral=True,
        )
        return

    conn = get_db()
    row = conn.execute(
        "SELECT id FROM events WHERE id = ? AND guild_id = ?", (event_id, interaction.guild_id)
    ).fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message(f"No event with ID #{event_id}.", ephemeral=True)
        return

    conn.execute(
        "UPDATE events SET notify_user_ids = ? WHERE id = ? AND guild_id = ?",
        (",".join(str(uid) for uid in notify_ids), event_id, interaction.guild_id),
    )
    conn.commit()
    conn.close()

    mentions = " ".join(f"<@{uid}>" for uid in notify_ids)
    await interaction.response.send_message(f"Event #{event_id} will now notify: {mentions}")


@bot.tree.command(description="Remove an event by its listed ID (see /events)")
@app_commands.checks.has_permissions(administrator=True)
async def removeevent(interaction: discord.Interaction, event_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT calendar_event_id, calendar_key FROM events WHERE id = ? AND guild_id = ?",
        (event_id, interaction.guild_id),
    ).fetchone()
    if row and row[0]:
        calendar_event_id, calendar_key = row
        delete_calendar_event(GOOGLE_CALENDARS.get(calendar_key), calendar_event_id)
    conn.execute(
        "DELETE FROM events WHERE id = ? AND guild_id = ?", (event_id, interaction.guild_id)
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"Removed event #{event_id}.")


@bot.tree.command(description="List all upcoming events")
async def events(interaction: discord.Interaction):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, month, day, year, calendar_link, calendar_key FROM events WHERE guild_id = ?",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No events saved yet.")
        return

    today = datetime.date.today()
    entries = []
    for event_id, name, month, day, year, calendar_link, calendar_key in rows:
        if year:
            try:
                d = datetime.date(year, month, day)
            except ValueError:
                continue
            if d < today:
                continue  # past one-off event
        else:
            d = datetime.date(today.year, month, day)
            if d < today:
                d = datetime.date(today.year + 1, month, day)
        entries.append((d, event_id, name, calendar_link, calendar_key))

    entries.sort()
    lines = []
    for d, eid, name, calendar_link, calendar_key in entries:
        line = f"`#{eid}` **{name}** — {d.strftime('%Y-%m-%d')} (in {(d - today).days} days)"
        if calendar_key:
            line += f" · {calendar_key}"
        if calendar_link:
            line += f" · [Server cal]({calendar_link})"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines) if lines else "No upcoming events.")


@bot.tree.command(description="Export all saved events as a CSV file (Administrator only)")
@app_commands.checks.has_permissions(administrator=True)
async def exportevents(interaction: discord.Interaction):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, month, day, year, created_by, calendar_link, notify_user_ids, calendar_key FROM events "
        "WHERE guild_id = ? ORDER BY id",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No events saved yet.", ephemeral=True)
        return

    csv_rows = []
    for event_id, name, month, day, year, created_by, calendar_link, notify_user_ids, calendar_key in rows:
        if created_by is None:
            creator_name = "(added on Google Calendar)"
        else:
            creator = interaction.guild.get_member(created_by)
            creator_name = creator.display_name if creator else "(left server)"
        notify_names = []
        for uid in (notify_user_ids or "").split(","):
            if not uid:
                continue
            member = interaction.guild.get_member(int(uid))
            notify_names.append(member.display_name if member else uid)
        csv_rows.append(
            [event_id, name, month, day, year or "", calendar_key or "", creator_name, created_by,
             calendar_link or "", ", ".join(notify_names)]
        )

    file = build_csv_file(
        "events.csv",
        ["ID", "Name", "Month", "Day", "Year", "Calendar", "Created By", "Created By User ID",
         "Calendar Link", "Notify"],
        csv_rows,
    )
    await interaction.response.send_message(file=file, ephemeral=True)


# ---------------------------------------------------------------------------
# Background task: check daily and post reminders
# ---------------------------------------------------------------------------
def _next_run_time():
    tz = ZoneInfo(TIMEZONE)
    now = datetime.datetime.now(tz)
    target = now.replace(hour=REMINDER_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.time()


@tasks.loop(time=datetime.time(hour=REMINDER_HOUR, tzinfo=ZoneInfo(TIMEZONE)))
async def daily_check():
    today = datetime.date.today()
    conn = get_db()

    for guild in bot.guilds:
        channel_id = get_reminder_channel(guild.id)
        if not channel_id:
            continue
        channel = guild.get_channel(channel_id)
        if not channel:
            continue

        messages = []

        for user_id, in conn.execute(
            "SELECT user_id FROM birthdays WHERE guild_id = ? AND month = ? AND day = ?",
            (guild.id, today.month, today.day),
        ):
            messages.append(f"🎂 Happy Birthday <@{user_id}>! 🎉")

        for name, notify_user_ids in conn.execute(
            "SELECT name, notify_user_ids FROM events WHERE guild_id = ? AND month = ? AND day = ? "
            "AND (year = ? OR year IS NULL)",
            (guild.id, today.month, today.day, today.year),
        ):
            line = f"📅 Reminder: **{name}** is today!"
            if notify_user_ids:
                mentions = " ".join(f"<@{uid}>" for uid in notify_user_ids.split(","))
                line += f" {mentions}"
            messages.append(line)

        if messages:
            await channel.send("\n".join(messages))

    conn.close()


@daily_check.before_loop
async def before_daily_check():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Background task: import events created directly on the shared Google
# Calendar (rather than via /addevent) so they get reminded too
# ---------------------------------------------------------------------------
@tasks.loop(hours=6)
async def sync_calendar_events():
    if not GOOGLE_CALENDARS:
        return  # calendar integration not configured

    series, failed_calendars = fetch_calendar_series()

    conn = get_db()
    for guild in bot.guilds:
        tracked = conn.execute(
            "SELECT id, calendar_key, calendar_event_id FROM events WHERE guild_id = ? AND calendar_event_id IS NOT NULL",
            (guild.id,),
        ).fetchall()
        existing_ids = {(calendar_key, calendar_event_id) for _, calendar_key, calendar_event_id in tracked}

        for (calendar_key, calendar_event_id), info in series.items():
            if (calendar_key, calendar_event_id) in existing_ids:
                continue
            conn.execute(
                "INSERT INTO events (guild_id, name, month, day, year, created_by, calendar_event_id, calendar_link, calendar_key) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    guild.id, info["name"], info["month"], info["day"], info["year"],
                    calendar_event_id, info["calendar_link"], calendar_key,
                ),
            )

        # Prune events no longer on their calendar (deleted directly on the
        # website). Only trust absence for a calendar we actually queried
        # successfully this round — skip calendars that errored, that were
        # renamed/removed from GOOGLE_CALENDARS since, and legacy rows from
        # before per-calendar tracking existed (calendar_key is NULL) — none
        # of those can be verified, so "absent" doesn't mean "deleted."
        for row_id, calendar_key, calendar_event_id in tracked:
            if not calendar_key or calendar_key not in GOOGLE_CALENDARS or calendar_key in failed_calendars:
                continue
            if (calendar_key, calendar_event_id) not in series:
                conn.execute("DELETE FROM events WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


@sync_calendar_events.before_loop
async def before_sync_calendar_events():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    bot.run(TOKEN)
