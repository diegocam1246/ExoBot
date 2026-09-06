"""
Birthday & Event Reminder Bot
-----------------------------
Features:
- Sets a custom bot name (nickname) per server on startup
- /setbirthday, /removebirthday, /birthdays   -> manage birthdays (all local,
  no calendar integration)
- /addevent, /removeevent, /events            -> manage one-off/annual
  events, stored entirely locally
- /addevent posts an announcement (tagging whichever members/roles you pass
  via `notify`) to the configured channel with a generic Google Calendar
  quick-add link + .ics file attached, so anyone can add it to their own
  calendar — the bot itself never talks to the Google Calendar API
- /notifyevent, /addmentionall, /removementionall -> control who gets
  @mentioned in an event's day-of reminder
- /setchannel                                 -> pick where announcements/reminders post
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
5. Run: python bot.py
"""

import os
import io
import csv
import json
import re
import sqlite3
import datetime
import traceback
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
BOT_NAME = os.getenv("BOT_NAME", "Reminder Bot")  # the "custom name" you want
TIMEZONE = os.getenv("TIMEZONE", "America/Toronto")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "reminders.db"))
# Birthday reminders always fire on the birthday itself, at this one fixed
# hour for every guild — unlike event reminders, which are per-guild
# configurable via /setreminderhour.
BIRTHDAY_REMINDER_HOUR = int(os.getenv("BIRTHDAY_REMINDER_HOUR", "9"))

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
    if "discord_event_id" not in existing_cols:
        conn.execute("ALTER TABLE events ADD COLUMN discord_event_id INTEGER")
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
    if "birthday_message" not in existing_settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN birthday_message TEXT")
    if "event_reminder" not in existing_settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN event_reminder TEXT")
    if "event_reminder_days_before" not in existing_settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN event_reminder_days_before INTEGER")
    if "reminder_hour" not in existing_settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN reminder_hour INTEGER")
    if "last_reminder_date" not in existing_settings_cols:
        # Tracks the last date event reminders were sent (birthdays use their
        # own separate last_birthday_reminder_date column below), preventing
        # re-sending multiple times within the same configured hour.
        conn.execute("ALTER TABLE settings ADD COLUMN last_reminder_date TEXT")
    if "last_birthday_reminder_date" not in existing_settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN last_birthday_reminder_date TEXT")
    return conn


def get_reminder_channel(guild_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT channel_id FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


DEFAULT_REMINDER_HOUR = 9


def get_reminder_hour(guild_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT reminder_hour FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else DEFAULT_REMINDER_HOUR


def get_last_event_reminder_date(guild_id: int) -> str | None:
    conn = get_db()
    row = conn.execute(
        "SELECT last_reminder_date FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def set_last_event_reminder_date(guild_id: int, date_str: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, last_reminder_date) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET last_reminder_date = excluded.last_reminder_date",
        (guild_id, date_str),
    )
    conn.commit()
    conn.close()


def get_last_birthday_reminder_date(guild_id: int) -> str | None:
    conn = get_db()
    row = conn.execute(
        "SELECT last_birthday_reminder_date FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def set_last_birthday_reminder_date(guild_id: int, date_str: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, last_birthday_reminder_date) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET last_birthday_reminder_date = excluded.last_birthday_reminder_date",
        (guild_id, date_str),
    )
    conn.commit()
    conn.close()


DEFAULT_BIRTHDAY_MESSAGE = "🎂 Happy Birthday {member}! 🎉"


def get_birthday_message_template(guild_id: int) -> str:
    conn = get_db()
    row = conn.execute(
        "SELECT birthday_message FROM settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else DEFAULT_BIRTHDAY_MESSAGE


DEFAULT_EVENT_REMINDER_SAME_DAY = "📅 Reminder: **{name}** is today!"
DEFAULT_EVENT_REMINDER_ADVANCE = "📅 Reminder: **{name}** is in {days} day(s)!"


def get_event_reminder_settings(guild_id: int) -> tuple[str, int]:
    """Returns (template, days_before). days_before defaults to 0 (same-day,
    the original behavior) if never configured. template falls back to a
    same-day or advance-notice default depending on days_before, unless a
    custom one has been set via /seteventreminder."""
    conn = get_db()
    row = conn.execute(
        "SELECT event_reminder, event_reminder_days_before FROM settings WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    conn.close()
    template, days_before = (row[0], row[1]) if row else (None, None)
    days_before = days_before or 0
    if not template:
        template = DEFAULT_EVENT_REMINDER_SAME_DAY if days_before == 0 else DEFAULT_EVENT_REMINDER_ADVANCE
    return template, days_before


def extract_mention_tokens(text: str | None) -> list[str]:
    """Extracts @member and @role mention tokens as-is (e.g. '<@123>',
    '<@&456>') from typed text, normalizing the nickname-mention form
    '<@!123>' down to '<@123>'. Also preserves the special '@everyone' and
    '@here' mentions, which stay as literal plain text (no ID to bracket)
    rather than being converted to '<@...>' markup like member/role
    mentions are. Preserves the distinction between mention types, unlike
    extracting bare IDs, so the same stored value can be re-emitted directly
    as a working mention later."""
    if not text:
        return []
    tokens = []
    for tok in re.findall(r"<@&?\d+>|<@!\d+>|@everyone|@here", text):
        if tok.startswith("<@!"):
            tok = "<@" + tok[3:]
        tokens.append(tok)
    return tokens


def describe_mention_token(guild: discord.Guild, token: str) -> str:
    """Human-readable label for a stored mention token, for CSV export."""
    m = re.match(r"<@&(\d+)>", token)
    if m:
        role = guild.get_role(int(m.group(1)))
        return f"@{role.name}" if role else token
    m = re.match(r"<@(\d+)>", token)
    if m:
        member = guild.get_member(int(m.group(1)))
        return f"@{member.display_name}" if member else token
    return token


def build_csv_file(filename: str, header: list[str], rows: list[list]) -> discord.File:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename=filename)




def parse_event_time(time: str | None) -> tuple[int, int]:
    """Parses a 24h 'HH:MM' string. Raises ValueError if malformed."""
    hour_str, minute_str = time.split(":")
    hour, minute = int(hour_str), int(minute_str)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("hour/minute out of range")
    return hour, minute


def event_datetime_bounds(month: int, day: int, year: int, time: str | None, duration: int | None = None):
    """Returns (is_all_day, start, end). For an all-day event, start/end are
    dates; for a timed one (time given), they're UTC datetimes `duration`
    minutes apart (default 60), computed from TIMEZONE."""
    event_date = datetime.date(year, month, day)
    if not time:
        return True, event_date, event_date + datetime.timedelta(days=1)
    hour, minute = parse_event_time(time)
    start = datetime.datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo(TIMEZONE)
    ).astimezone(datetime.timezone.utc)
    return False, start, start + datetime.timedelta(minutes=duration or 60)


def calendar_add_link(
    name: str, month: int, day: int, year: int, time: str | None = None,
    duration: int | None = None, location: str | None = None,
) -> str:
    """Builds a generic Google Calendar 'quick add' URL for a single-date
    event. Clicking it lets ANY user (no login/API needed on our end) add
    the event to whichever of their own calendars they choose in the
    create-event form — nothing here is tied to a specific organization
    calendar, since the bot's service account only has view access and
    can't create events itself."""
    is_all_day, start, end = event_datetime_bounds(month, day, year, time, duration)
    params = {"action": "TEMPLATE", "text": name}
    if is_all_day:
        params["dates"] = f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}"
    else:
        params["dates"] = f"{start.strftime('%Y%m%dT%H%M%SZ')}/{end.strftime('%Y%m%dT%H%M%SZ')}"
    if location:
        params["location"] = location
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def build_ics_file(
    name: str, month: int, day: int, year: int, time: str | None = None,
    duration: int | None = None, location: str | None = None,
) -> discord.File:
    """Builds a universal .ics calendar file for a single-date event (works
    with Google, Outlook, Apple Calendar, etc.) that anyone can download and
    import directly."""
    is_all_day, start, end = event_datetime_bounds(month, day, year, time, duration)
    uid = f"{start.isoformat()}-{abs(hash(name))}@birthday-bot"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//birthday-bot//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
    ]
    if is_all_day:
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}")
        lines.append(f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}")
    lines.append(f"SUMMARY:{name}")
    if location:
        lines.append(f"LOCATION:{location}")
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


@bot.tree.command(description="Set what hour EVENT reminders post at (24h, per TIMEZONE)")
@app_commands.describe(hour="0-23 — birthdays have their own fixed time, set via BIRTHDAY_REMINDER_HOUR")
@app_commands.checks.has_permissions(administrator=True)
async def setreminderhour(interaction: discord.Interaction, hour: app_commands.Range[int, 0, 23]):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, reminder_hour) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET reminder_hour = excluded.reminder_hour",
        (interaction.guild_id, hour),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Event reminders will now post at {hour:02d}:00 ({TIMEZONE}). "
        "(Birthday reminders use a separate fixed time — see BIRTHDAY_REMINDER_HOUR.)",
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Slash commands: birthdays
# ---------------------------------------------------------------------------
@bot.tree.command(description="Set a member's birthday (Administrator only)")
@app_commands.describe(month="1-12", day="1-31", member="Whose birthday this is")
@app_commands.checks.has_permissions(administrator=True)
async def setbirthday(
    interaction: discord.Interaction,
    month: app_commands.Range[int, 1, 12],
    day: app_commands.Range[int, 1, 31],
    member: discord.Member,
):
    try:
        datetime.date(2024, month, day)  # validate, 2024 = leap year so Feb 29 works
    except ValueError:
        await interaction.response.send_message("That's not a real date.", ephemeral=True)
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO birthdays (guild_id, user_id, month, day) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET month=excluded.month, day=excluded.day",
        (interaction.guild_id, member.id, month, day),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Saved {member.display_name}'s birthday as {month:02d}/{day:02d}."
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


@bot.tree.command(description="Set the day-of birthday message (use {member})")
@app_commands.checks.has_permissions(administrator=True)
async def setbirthdaymessage(interaction: discord.Interaction, template: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, birthday_message) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET birthday_message = excluded.birthday_message",
        (interaction.guild_id, template),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Birthday messages will now use:\n> {template}", ephemeral=True
    )


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
@bot.tree.command(description="Add an event and get a link to add it to a calendar")
@app_commands.describe(
    name="What the event is",
    month="1-12",
    day="1-31",
    year="Leave empty to use the current year",
    time="Optional start time, 24h format HH:MM (defaults to an all-day event)",
    duration="Duration in minutes, only used with time (defaults to 60)",
    location="Optional location",
    notify="@mention the members/roles this event concerns",
)
@app_commands.checks.has_permissions(administrator=True)
async def addevent(
    interaction: discord.Interaction,
    name: str,
    month: app_commands.Range[int, 1, 12],
    day: app_commands.Range[int, 1, 31],
    year: int = None,
    time: str = None,
    duration: int = None,
    location: str = None,
    notify: str = None,
):
    year = year or datetime.date.today().year

    try:
        datetime.date(year, month, day)
    except ValueError:
        await interaction.response.send_message("That's not a real date.", ephemeral=True)
        return

    if time is not None:
        try:
            parse_event_time(time)
        except ValueError:
            await interaction.response.send_message(
                "Time must be in 24h HH:MM format, e.g. 14:30.", ephemeral=True
            )
            return

    if duration is not None and duration < 1:
        await interaction.response.send_message(
            "Duration must be a positive number of minutes.", ephemeral=True
        )
        return

    notify_tokens = extract_mention_tokens(notify)

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO events (guild_id, name, month, day, year, created_by, notify_user_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            interaction.guild_id, name, month, day, year, interaction.user.id,
            " ".join(notify_tokens) or None,
        ),
    )
    event_id = cursor.lastrowid
    conn.commit()
    conn.close()

    link = calendar_add_link(name, month, day, year, time=time, duration=duration, location=location)

    await interaction.response.send_message(
        f"New event added (#{event_id})! If you wish to add it to your calendar, click on this link: {link}",
        file=build_ics_file(name, month, day, year, time=time, duration=duration, location=location),
    )

    channel_id = get_reminder_channel(interaction.guild_id)
    if channel_id:
        channel = interaction.guild.get_channel(channel_id)
        if channel:
            text = f"📢 New event: **{name}**"
            if notify:
                text += f" {notify}"
            text += f"\n➕ [Add to your calendar]({link})"
            await channel.send(
                text, file=build_ics_file(name, month, day, year, time=time, duration=duration, location=location)
            )


@bot.tree.command(description="Set who gets @mentioned for an existing event (replaces the list)")
@app_commands.describe(
    event_id="The event's ID (see /events)",
    notify="@mention the members/roles this event concerns",
)
@app_commands.checks.has_permissions(administrator=True)
async def notifyevent(interaction: discord.Interaction, event_id: int, notify: str):
    tokens = extract_mention_tokens(notify)
    if not tokens:
        await interaction.response.send_message(
            "Couldn't find any @mentions in that — make sure to actually @-mention the members/roles.",
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
        (" ".join(tokens), event_id, interaction.guild_id),
    )
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"Event #{event_id} will now notify: {' '.join(tokens)}")


@bot.tree.command(description="Add a member/role to every tracked event's notify list (Administrator only)")
@app_commands.describe(notify="@mention the member(s)/role(s) to add to every event")
@app_commands.checks.has_permissions(administrator=True)
async def addmentionall(interaction: discord.Interaction, notify: str):
    add_tokens = set(extract_mention_tokens(notify))
    if not add_tokens:
        await interaction.response.send_message(
            "Couldn't find any @mentions in that.", ephemeral=True
        )
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT id, notify_user_ids FROM events WHERE guild_id = ?", (interaction.guild_id,)
    ).fetchall()
    for event_id, notify_user_ids in rows:
        existing = set(extract_mention_tokens(notify_user_ids))
        updated = existing | add_tokens
        conn.execute(
            "UPDATE events SET notify_user_ids = ? WHERE id = ?",
            (" ".join(updated), event_id),
        )
    conn.commit()
    conn.close()

    mentions = " ".join(add_tokens)
    await interaction.response.send_message(f"Added {mentions} to {len(rows)} event(s).")


@bot.tree.command(description="Remove a member/role from every tracked event's notify list (Admin only)")
@app_commands.describe(notify="@mention the member(s)/role(s) to remove from every event")
@app_commands.checks.has_permissions(administrator=True)
async def removementionall(interaction: discord.Interaction, notify: str):
    remove_tokens = set(extract_mention_tokens(notify))
    if not remove_tokens:
        await interaction.response.send_message(
            "Couldn't find any @mentions in that.", ephemeral=True
        )
        return

    conn = get_db()
    rows = conn.execute(
        "SELECT id, notify_user_ids FROM events WHERE guild_id = ? AND notify_user_ids IS NOT NULL",
        (interaction.guild_id,),
    ).fetchall()
    changed = 0
    for event_id, notify_user_ids in rows:
        existing = set(extract_mention_tokens(notify_user_ids))
        updated = existing - remove_tokens
        if updated == existing:
            continue
        changed += 1
        new_value = " ".join(updated) or None
        conn.execute("UPDATE events SET notify_user_ids = ? WHERE id = ?", (new_value, event_id))
    conn.commit()
    conn.close()

    mentions = " ".join(remove_tokens)
    await interaction.response.send_message(
        f"Removed {mentions} from {changed} event(s). Any event left with an empty list will "
        "no longer tag anyone in its day-of reminder."
    )


@bot.tree.command(description="Set the event reminder message and/or advance notice (Admin only)")
@app_commands.describe(
    template="Optional: new reminder message (use {name}, {days}, {notify})",
    days="Optional: how many days before the event to remind (0 = same day, the default)",
)
@app_commands.checks.has_permissions(administrator=True)
async def seteventreminder(interaction: discord.Interaction, template: str = None, days: int = None):
    if template is None and days is None:
        await interaction.response.send_message(
            "Provide at least one of template or days.", ephemeral=True
        )
        return

    if days is not None and days < 0:
        await interaction.response.send_message(
            "Days must be zero or a positive number.", ephemeral=True
        )
        return

    conn = get_db()
    conn.execute(
        "INSERT INTO settings (guild_id, event_reminder, event_reminder_days_before) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "event_reminder = COALESCE(excluded.event_reminder, settings.event_reminder), "
        "event_reminder_days_before = COALESCE(excluded.event_reminder_days_before, settings.event_reminder_days_before)",
        (interaction.guild_id, template, days),
    )
    conn.commit()
    conn.close()

    parts = []
    if template is not None:
        parts.append(f"message:\n> {template}")
    if days is not None:
        parts.append(f"reminding {days} day(s) before the event" if days else "reminding on the same day")
    await interaction.response.send_message("Updated event reminders — " + "; ".join(parts), ephemeral=True)


@bot.tree.command(description="Remove an event by its listed ID (see /events)")
@app_commands.checks.has_permissions(administrator=True)
async def removeevent(interaction: discord.Interaction, event_id: int):
    conn = get_db()
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
        "SELECT id, name, month, day, year FROM events WHERE guild_id = ?",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No events saved yet.")
        return

    today = datetime.date.today()
    entries = []
    for event_id, name, month, day, year in rows:
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
        entries.append((d, event_id, name))

    entries.sort()
    lines = [
        f"`#{eid}` **{name}** — {d.strftime('%Y-%m-%d')} (in {(d - today).days} days)"
        for d, eid, name in entries
    ]
    await interaction.response.send_message("\n".join(lines) if lines else "No upcoming events.")


@bot.tree.command(description="Export all saved events as a CSV file (Administrator only)")
@app_commands.checks.has_permissions(administrator=True)
async def exportevents(interaction: discord.Interaction):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, month, day, year, created_by, notify_user_ids FROM events "
        "WHERE guild_id = ? ORDER BY id",
        (interaction.guild_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("No events saved yet.", ephemeral=True)
        return

    csv_rows = []
    for event_id, name, month, day, year, created_by, notify_user_ids in rows:
        creator = interaction.guild.get_member(created_by)
        creator_name = creator.display_name if creator else "(left server)"
        notify_names = [
            describe_mention_token(interaction.guild, tok)
            for tok in extract_mention_tokens(notify_user_ids)
        ]
        csv_rows.append(
            [event_id, name, month, day, year or "", creator_name, created_by, ", ".join(notify_names)]
        )

    file = build_csv_file(
        "events.csv",
        ["ID", "Name", "Month", "Day", "Year", "Created By", "Created By User ID", "Notify"],
        csv_rows,
    )
    await interaction.response.send_message(file=file, ephemeral=True)


@bot.tree.command(description="Bulk-import events from a JSON file (Administrator only)")
@app_commands.describe(
    file='A JSON file: a list of {"name": ..., "month": 1-12, "day": 1-31, "year": optional, "notify": optional}'
)
@app_commands.checks.has_permissions(administrator=True)
async def importevents(interaction: discord.Interaction, file: discord.Attachment):
    await interaction.response.defer(ephemeral=True)

    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        await interaction.followup.send(f"Couldn't parse that file as JSON: {e}", ephemeral=True)
        return

    if not isinstance(data, list):
        await interaction.followup.send(
            'Expected a JSON array of {"name", "month", "day"} objects.', ephemeral=True
        )
        return

    conn = get_db()
    imported = 0
    errors = []
    for i, entry in enumerate(data):
        try:
            name = str(entry["name"])
            month = int(entry["month"])
            day = int(entry["day"])
            year = int(entry["year"]) if entry.get("year") else datetime.date.today().year
            datetime.date(year, month, day)  # validate
        except (KeyError, TypeError, ValueError):
            errors.append(f"Entry {i}: invalid or missing name/month/day/year")
            continue
        notify_tokens = extract_mention_tokens(entry.get("notify"))
        conn.execute(
            "INSERT INTO events (guild_id, name, month, day, year, created_by, notify_user_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                interaction.guild_id, name, month, day, year, interaction.user.id,
                " ".join(notify_tokens) or None,
            ),
        )
        imported += 1
    conn.commit()
    conn.close()

    summary = f"Imported {imported} event(s)."
    if errors:
        shown = errors[:10]
        summary += "\n" + "\n".join(shown)
        if len(errors) > 10:
            summary += f"\n...and {len(errors) - 10} more errors."
    await interaction.followup.send(summary, ephemeral=True)


# ---------------------------------------------------------------------------
# Background task: checks every few minutes, posting once per day per guild.
# Birthdays always fire at the fixed BIRTHDAY_REMINDER_HOUR; events fire at
# whatever hour that guild configured via /setreminderhour (default 9am).
# These can be different times, so they're gated and sent independently.
# ---------------------------------------------------------------------------
async def _send_birthday_reminders(conn, guild: discord.Guild, today: datetime.date):
    channel_id = get_reminder_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    messages = []
    template = None
    for user_id, in conn.execute(
        "SELECT user_id FROM birthdays WHERE guild_id = ? AND month = ? AND day = ?",
        (guild.id, today.month, today.day),
    ):
        if template is None:
            template = get_birthday_message_template(guild.id)
        messages.append(template.replace("{member}", f"<@{user_id}>"))

    if messages:
        await channel.send("\n".join(messages))


async def _send_event_reminders(conn, guild: discord.Guild, today: datetime.date):
    channel_id = get_reminder_channel(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return

    event_template, days_before = get_event_reminder_settings(guild.id)
    target_date = today + datetime.timedelta(days=days_before)

    messages = []
    for name, notify_user_ids in conn.execute(
        "SELECT name, notify_user_ids FROM events WHERE guild_id = ? AND month = ? AND day = ? "
        "AND (year = ? OR year IS NULL)",
        (guild.id, target_date.month, target_date.day, target_date.year),
    ):
        mention = notify_user_ids or ""
        text = event_template.replace("{name}", name).replace("{days}", str(days_before))
        if "{notify}" in text:
            line = text.replace("{notify}", mention)
        elif mention:
            line = f"{text} {mention}"
        else:
            line = text
        messages.append(line)

    if messages:
        await channel.send("\n".join(messages))


@tasks.loop(minutes=5)
async def daily_check():
    now_local = datetime.datetime.now(ZoneInfo(TIMEZONE))
    today = now_local.date()
    today_str = today.isoformat()
    conn = get_db()

    for guild in bot.guilds:
        if now_local.hour == BIRTHDAY_REMINDER_HOUR and get_last_birthday_reminder_date(guild.id) != today_str:
            try:
                await _send_birthday_reminders(conn, guild, today)
            except Exception:
                # Never let one guild's failure (missing channel permissions,
                # a deleted channel, etc.) kill the whole task — discord.py
                # only auto-retries on low-level connection errors, so
                # anything else here would otherwise silently stop reminders
                # for every guild until the bot restarts.
                print(f"Failed to send birthday reminders for guild {guild.id} ({guild.name}):")
                traceback.print_exc()
            else:
                # Only mark today as done once sending actually succeeded (or
                # there was nothing to send) — on failure, leave it unmarked
                # so later ticks this same hour keep retrying.
                set_last_birthday_reminder_date(guild.id, today_str)

        if now_local.hour == get_reminder_hour(guild.id) and get_last_event_reminder_date(guild.id) != today_str:
            try:
                await _send_event_reminders(conn, guild, today)
            except Exception:
                print(f"Failed to send event reminders for guild {guild.id} ({guild.name}):")
                traceback.print_exc()
            else:
                set_last_event_reminder_date(guild.id, today_str)

    conn.close()


@daily_check.before_loop
async def before_daily_check():
    await bot.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file first.")
    bot.run(TOKEN)
