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
import json
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
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
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

    if not GOOGLE_CALENDAR_ID:
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


def create_calendar_event(name: str, month: int, day: int, year: int | None):
    """Creates a matching all-day event on the shared Google Calendar.
    One-off events (year given) are a single day; yearly events (year=None)
    get an annual RRULE recurrence. Returns (event_id, html_link) or
    (None, None) if calendar integration isn't set up or the call fails."""
    service = get_calendar_service()
    if service is None:
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
        created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
        return created.get("id"), created.get("htmlLink")
    except HttpError as e:
        print(f"Google Calendar API error creating event: {e}")
        return None, None


def delete_calendar_event(calendar_event_id: str | None):
    if not calendar_event_id:
        return
    service = get_calendar_service()
    if service is None:
        return
    try:
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=calendar_event_id).execute()
    except HttpError as e:
        print(f"Google Calendar API error deleting event: {e}")


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


def can_manage_others(interaction: discord.Interaction) -> bool:
    """True if the invoking user is allowed to edit/remove OTHER people's
    birthdays. Server mods (Manage Server permission) or the server owner."""
    perms = interaction.user.guild_permissions
    return perms.manage_guild or interaction.user == interaction.guild.owner


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
            "You need the **Manage Server** permission to do that.", ephemeral=True
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
@app_commands.checks.has_permissions(manage_guild=True)
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
@app_commands.checks.has_permissions(manage_guild=True)
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
@bot.tree.command(description="Set your (or someone else's) birthday")
@app_commands.describe(month="1-12", day="1-31", member="Leave empty to set your own")
async def setbirthday(
    interaction: discord.Interaction,
    month: app_commands.Range[int, 1, 12],
    day: app_commands.Range[int, 1, 31],
    member: discord.Member = None,
):
    target = member or interaction.user

    if target != interaction.user and not can_manage_others(interaction):
        await interaction.response.send_message(
            "You can only set your own birthday. Ask a mod (Manage Server permission) to set it for someone else.",
            ephemeral=True,
        )
        return

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


@bot.tree.command(description="Remove a saved birthday")
async def removebirthday(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user

    if target != interaction.user and not can_manage_others(interaction):
        await interaction.response.send_message(
            "You can only remove your own birthday. Ask a mod (Manage Server permission) to remove it for someone else.",
            ephemeral=True,
        )
        return

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


# ---------------------------------------------------------------------------
# Slash commands: events
# ---------------------------------------------------------------------------
@bot.tree.command(description="Add a reminder event (e.g. tournament, meeting, launch date)")
@app_commands.describe(
    name="What the event is",
    month="1-12",
    day="1-31",
    year="Leave empty for a yearly-repeating event",
    announcement="Optional one-off message for this event (overrides the server template)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def addevent(
    interaction: discord.Interaction,
    name: str,
    month: app_commands.Range[int, 1, 12],
    day: app_commands.Range[int, 1, 31],
    year: int = None,
    announcement: str = None,
):
    try:
        datetime.date(year or 2024, month, day)
    except ValueError:
        await interaction.response.send_message("That's not a real date.", ephemeral=True)
        return

    await interaction.response.defer()  # calendar API call can take a moment

    calendar_event_id, calendar_link = create_calendar_event(name, month, day, year)

    conn = get_db()
    conn.execute(
        "INSERT INTO events (guild_id, name, month, day, year, created_by, calendar_event_id, calendar_link) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (interaction.guild_id, name, month, day, year, interaction.user.id, calendar_event_id, calendar_link),
    )
    conn.commit()

    when = f"{month:02d}/{day:02d}" + (f"/{year}" if year else " (yearly)")
    add_link = personal_add_link(name, month, day, year)

    confirmation = f"Added event **{name}** on {when}."
    if calendar_link:
        confirmation += f"\n📅 [View on server calendar]({calendar_link})"
    confirmation += f"\n➕ [Add to your own calendar]({add_link})"
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
            await channel.send(text, file=build_ics_file(name, month, day, year))

    conn.close()


@bot.tree.command(description="Remove an event by its listed ID (see /events)")
@app_commands.checks.has_permissions(manage_guild=True)
async def removeevent(interaction: discord.Interaction, event_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT calendar_event_id FROM events WHERE id = ? AND guild_id = ?",
        (event_id, interaction.guild_id),
    ).fetchone()
    if row and row[0]:
        delete_calendar_event(row[0])
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
        "SELECT id, name, month, day, year, calendar_link FROM events WHERE guild_id = ?",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No events saved yet.")
        return

    today = datetime.date.today()
    entries = []
    for event_id, name, month, day, year, calendar_link in rows:
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
        entries.append((d, event_id, name, calendar_link))

    entries.sort()
    lines = []
    for d, eid, name, calendar_link in entries:
        line = f"`#{eid}` **{name}** — {d.strftime('%Y-%m-%d')} (in {(d - today).days} days)"
        if calendar_link:
            line += f" · [Server cal]({calendar_link})"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines) if lines else "No upcoming events.")


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

        for name, in conn.execute(
            "SELECT name FROM events WHERE guild_id = ? AND month = ? AND day = ? "
            "AND (year = ? OR year IS NULL)",
            (guild.id, today.month, today.day, today.year),
        ):
            messages.append(f"📅 Reminder: **{name}** is today!")

        if messages:
            await channel.send("\n".join(messages))

    conn.close()


@daily_check.before_loop
async def before_daily_check():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    bot.run(TOKEN)
