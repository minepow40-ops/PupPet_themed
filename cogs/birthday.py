"""
cogs/birthday.py — Birthday System — PupPet
─────────────────────────────────────────────────────────────────────────────
• /mybd year month day age      — Set / update your birthday
• /birthday view                — View your saved birthday + countdown
• /birthday remove              — Remove your birthday entry
• /birthday list                — List all guild birthdays (paginated)
• /birthday upcoming            — Next 10 upcoming birthdays
• /bdpanel                      — [Admin] Post a persistent birthday panel

Background task (every 60 s):
  • At exactly 00:00 Asia/Colombo: announces birthdays, assigns role,
    sets 🎂 nickname prefix, updates last_announced_year.
  • Removes birthday role + restores nickname from yesterday's birthdays.

Birthday role (ID: 1513192633327947796) is added at midnight and
removed the following day.

DB: data/birthdays.db
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytz

from utils.migrations import Migration, run_migrations
from utils.helpers import admin_check

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("PupPet.birthday")

# ── Timezone ──────────────────────────────────────────────────────────────────
TZ_COLOMBO = pytz.timezone("Asia/Colombo")

# ── Constants ─────────────────────────────────────────────────────────────────
ANNOUNCE_CHANNEL_ID : int = 1551515703952277580   # #birthday-announcements
PING_ROLE_ID        : int = 1551515669026177077   # 📣 Role mentioned in announcement
BIRTHDAY_ROLE_ID    : int = 1551515672301801544   # 🎂 Role given to user for 24h
PAGE_SIZE           : int = 10                    # entries per /birthday list page

BIRTHDAY_PREFIX     : str = "🎂 "
ASSETS_DIR          : Path = Path(__file__).parent.parent / "assets"
BANNER_PATH         : Path = ASSETS_DIR / "puppybd.png"

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent.parent / "data" / "birthdays.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS birthdays (
    user_id              INTEGER NOT NULL,
    guild_id             INTEGER NOT NULL,
    year                 INTEGER,
    month                INTEGER NOT NULL,
    day                  INTEGER NOT NULL,
    age                  INTEGER,
    last_announced_year  INTEGER,
    created_at           TEXT    NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);
"""

CREATE_NICKNAMES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS birthday_nicknames (
    user_id           INTEGER NOT NULL,
    guild_id          INTEGER NOT NULL,
    original_nickname TEXT,
    PRIMARY KEY (user_id, guild_id)
);
"""

# ── Colour palette ─────────────────────────────────────────────────────────────
COL_GOLD    = 0xFFC107
COL_ORANGE  = 0xFF8C00
COL_SUCCESS = 0x2ECC71
COL_ERROR   = 0xE74C3C
COL_INFO    = 0x5865F2
COL_PINK    = 0xFF69B4


# ══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ══════════════════════════════════════════════════════════════════════════════

# Every schema change for this database gets ONE new entry appended here.
# NEVER edit an existing entry once it has shipped — add a new one instead,
# even for something as small as a typo in a column name.
MIGRATIONS = [
    Migration(1, "initial schema — birthdays + birthday_nicknames", (
        CREATE_TABLE_SQL + CREATE_NICKNAMES_TABLE_SQL
    )),
]


async def init_db() -> None:
    """Create the data directory and all tables if missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Migrations run synchronously — this only happens once at cog load,
    # so blocking briefly here is fine and keeps version-tracking logic
    # in one place instead of duplicating it for aiosqlite.
    con = sqlite3.connect(DB_PATH)
    try:
        run_migrations(con, MIGRATIONS, db_name="birthdays.db")
    finally:
        con.close()
    log.info("[DB] birthdays.db initialised.")


async def db_upsert_birthday(
    user_id: int,
    guild_id: int,
    year: Optional[int],
    month: int,
    day: int,
    age: Optional[int],
) -> None:
    now = _now_colombo().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO birthdays
                (user_id, guild_id, year, month, day, age, last_announced_year, created_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT (user_id, guild_id) DO UPDATE SET
                year                = excluded.year,
                month               = excluded.month,
                day                 = excluded.day,
                age                 = excluded.age,
                last_announced_year = NULL
            """,
            (user_id, guild_id, year, month, day, age, now),
        )
        await db.commit()


async def db_get_birthday(user_id: int, guild_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM birthdays WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ) as cursor:
            return await cursor.fetchone()


async def db_delete_birthday(user_id: int, guild_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM birthdays WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def db_list_guild_birthdays(guild_id: int) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM birthdays WHERE guild_id=? ORDER BY month, day",
            (guild_id,),
        ) as cursor:
            return await cursor.fetchall()


async def db_todays_birthdays(
    guild_id: int, month: int, day: int, current_year: int
) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM birthdays
            WHERE guild_id=?
              AND month=?
              AND day=?
              AND (last_announced_year IS NULL OR last_announced_year != ?)
            """,
            (guild_id, month, day, current_year),
        ) as cursor:
            return await cursor.fetchall()


async def db_mark_announced(user_id: int, guild_id: int, year: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE birthdays SET last_announced_year=? WHERE user_id=? AND guild_id=?",
            (year, user_id, guild_id),
        )
        await db.commit()


# ── Nickname DB helpers ────────────────────────────────────────────────────────

async def db_save_original_nickname(
    user_id: int, guild_id: int, original_nickname: Optional[str]
) -> None:
    """Store the original nickname before applying the birthday prefix."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO birthday_nicknames (user_id, guild_id, original_nickname)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id, guild_id) DO UPDATE SET
                original_nickname = excluded.original_nickname
            """,
            (user_id, guild_id, original_nickname),
        )
        await db.commit()


async def db_get_original_nickname(
    user_id: int, guild_id: int
) -> Optional[str]:
    """Retrieve the stored original nickname. Returns None if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT original_nickname FROM birthday_nicknames WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return row[0]  # may be None if they had no nickname


async def db_delete_nickname_record(user_id: int, guild_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM birthday_nicknames WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        )
        await db.commit()


# ══════════════════════════════════════════════════════════════════════════════
# Date / time utilities
# ══════════════════════════════════════════════════════════════════════════════

def _now_colombo() -> datetime:
    return datetime.now(TZ_COLOMBO)


def _today_colombo() -> date:
    return _now_colombo().date()


def _is_valid_date(year: Optional[int], month: int, day: int) -> bool:
    ref_year = year if year else 2000
    try:
        date(ref_year, month, day)
        return True
    except ValueError:
        return False


def _days_until_next_birthday(month: int, day: int) -> int:
    today = _today_colombo()
    try:
        next_bd = date(today.year, month, day)
    except ValueError:
        next_bd = date(today.year, 3, 1)
    if next_bd < today:
        try:
            next_bd = date(today.year + 1, month, day)
        except ValueError:
            next_bd = date(today.year + 1, 3, 1)
    return (next_bd - today).days


def _ordinal(n: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    if 11 <= n % 100 <= 13:
        suffix = "th"
    return f"{n}{suffix}"


def _format_date(month: int, day: int, year: Optional[int]) -> str:
    month_name = calendar.month_name[month]
    base = f"{month_name} {day}"
    return f"{base}, {year}" if year else base


# ══════════════════════════════════════════════════════════════════════════════
# Persistent UI — Birthday Panel (survives bot restarts)
# ══════════════════════════════════════════════════════════════════════════════

class BirthdaySetModal(discord.ui.Modal, title="🎂 Set Your Birthday"):
    """Modal for setting a birthday via the panel."""

    bd_year  = discord.ui.TextInput(
        label="Birth Year (optional — enter 0 to skip)",
        placeholder="e.g. 1998  or  0",
        required=True,
        max_length=4,
    )
    bd_month = discord.ui.TextInput(
        label="Birth Month (1–12)",
        placeholder="e.g. 6",
        required=True,
        max_length=2,
    )
    bd_day   = discord.ui.TextInput(
        label="Birth Day (1–31)",
        placeholder="e.g. 7",
        required=True,
        max_length=2,
    )
    bd_age   = discord.ui.TextInput(
        label="Your Current Age",
        placeholder="e.g. 22",
        required=True,
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # ── Parse ────────────────────────────────────────────────────────────
        try:
            year_raw  = int(self.bd_year.value.strip())
            month_raw = int(self.bd_month.value.strip())
            day_raw   = int(self.bd_day.value.strip())
            age_raw   = int(self.bd_age.value.strip())
        except ValueError:
            await interaction.response.send_message(
                embed=_error_embed("All fields must be numbers."), ephemeral=True
            )
            return

        clean_year: Optional[int] = year_raw if year_raw > 0 else None

        # ── Validate ─────────────────────────────────────────────────────────
        if not 1 <= month_raw <= 12:
            await interaction.response.send_message(
                embed=_error_embed("Month must be between **1** and **12**."), ephemeral=True
            )
            return

        if not _is_valid_date(clean_year, month_raw, day_raw):
            await interaction.response.send_message(
                embed=_error_embed(
                    f"**{day_raw}/{month_raw}** is not a valid date. "
                    "Check day/month (e.g. February only has 28–29 days)."
                ),
                ephemeral=True,
            )
            return

        if clean_year and (clean_year < 1900 or clean_year > _today_colombo().year):
            await interaction.response.send_message(
                embed=_error_embed("Birth year must be between **1900** and the current year."),
                ephemeral=True,
            )
            return

        if not 0 <= age_raw <= 150:
            await interaction.response.send_message(
                embed=_error_embed("Age must be between **0** and **150**."), ephemeral=True
            )
            return

        # ── Save ─────────────────────────────────────────────────────────────
        try:
            await db_upsert_birthday(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                year=clean_year,
                month=month_raw,
                day=day_raw,
                age=age_raw,
            )
        except Exception as exc:
            log.error("[Birthday] Modal DB upsert failed: %s", exc, exc_info=True)
            await interaction.response.send_message(
                embed=_error_embed("Database error while saving. Please try again."),
                ephemeral=True,
            )
            return

        date_str = _format_date(month_raw, day_raw, clean_year)
        embed = discord.Embed(
            title="🎂 Birthday Saved!",
            description=f"Your birthday has been set to **{date_str}**.",
            colour=COL_SUCCESS,
        )
        embed.add_field(name="🎈 Age", value=str(age_raw), inline=True)
        embed.set_footer(text="🐶 PupPet Birthday System")
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # If today IS their birthday, announce immediately
        today = _today_colombo()
        if today.month == month_raw and today.day == day_raw:
            row_data = {
                "user_id": interaction.user.id,
                "guild_id": interaction.guild_id,
                "year": clean_year,
                "month": month_raw,
                "day": day_raw,
                "age": age_raw,
                "last_announced_year": None,
            }
            if isinstance(interaction.user, discord.Member):
                cog: Optional[Birthday] = interaction.client.get_cog("Birthday")  # type: ignore[assignment]
                if cog:
                    await cog._immediate_birthday_announce(interaction, interaction.user, row_data)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("[Birthday] Modal error: %s", error, exc_info=True)
        await interaction.response.send_message(
            embed=_error_embed("An unexpected error occurred."), ephemeral=True
        )


class BirthdayPanelView(discord.ui.View):
    """
    Persistent panel view — timeout=None so it survives restarts.
    Register this view with bot.add_view() on startup.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Set Birthday",
        emoji="🎂",
        style=discord.ButtonStyle.success,
        custom_id="bdpanel:set",
    )
    async def set_birthday_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(BirthdaySetModal())

    @discord.ui.button(
        label="View Birthday",
        emoji="📅",
        style=discord.ButtonStyle.primary,
        custom_id="bdpanel:view",
    )
    async def view_birthday_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        row = await db_get_birthday(interaction.user.id, interaction.guild_id)
        if not row:
            await interaction.response.send_message(
                embed=_error_embed("You haven't set a birthday yet. Click **🎂 Set Birthday**!"),
                ephemeral=True,
            )
            return

        today       = _today_colombo()
        days_left   = _days_until_next_birthday(row["month"], row["day"])
        date_str    = _format_date(row["month"], row["day"], row["year"])

        if row["year"]:
            age_display = str(today.year - row["year"])
        elif row["age"]:
            age_display = str(row["age"])
        else:
            age_display = "Not provided"

        countdown = "🎉 **Today is your birthday!**" if days_left == 0 else f"**{days_left}** days away"

        embed = discord.Embed(
            title=f"🎂 {interaction.user.display_name}'s Birthday",
            colour=COL_PINK,
        )
        embed.add_field(name="📅 Birthday",      value=date_str,    inline=True)
        embed.add_field(name="🎈 Age",           value=age_display, inline=True)
        embed.add_field(name="⏳ Next Birthday", value=countdown,   inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="🐶 PupPet Birthday System")
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Remove Birthday",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="bdpanel:remove",
    )
    async def remove_birthday_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        deleted = await db_delete_birthday(interaction.user.id, interaction.guild_id)
        if deleted:
            embed = discord.Embed(
                title="🗑️ Birthday Removed",
                description="Your birthday has been removed from the database.",
                colour=COL_SUCCESS,
            )
        else:
            embed = _error_embed("You don't have a birthday saved, so there's nothing to remove.")
        embed.set_footer(text="🐶 PupPet Birthday System")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class Birthday(commands.Cog):
    """
    Birthday System — tracks member birthdays, announces at midnight
    Asia/Colombo, assigns the birthday role + 🎂 nickname prefix for 24h.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_midnight_date: Optional[date] = None

    # ── Cog lifecycle ─────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        await init_db()
        # Register the persistent view so buttons work after restarts
        self.bot.add_view(BirthdayPanelView())
        self.birthday_loop.start()
        log.info("[Birthday] Cog loaded, background loop started.")

    async def cog_unload(self) -> None:
        self.birthday_loop.cancel()
        log.info("[Birthday] Background loop stopped.")

    # ── Background task ───────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def birthday_loop(self) -> None:
        now   = _now_colombo()
        today = now.date()

        if now.hour != 0 or now.minute != 0:
            return
        if self._last_midnight_date == today:
            return
        self._last_midnight_date = today

        yesterday = today - timedelta(days=1)
        log.info("[Birthday] Midnight triggered for %s.", today.isoformat())

        for guild in self.bot.guilds:
            # 1. Restore yesterday's birthday members (role + nickname)
            await self._restore_yesterday_birthdays(guild, yesterday)

            # 2. Announce today's birthdays
            rows = await db_todays_birthdays(guild.id, today.month, today.day, today.year)
            if not rows:
                continue

            channel = guild.get_channel(ANNOUNCE_CHANNEL_ID)
            if not isinstance(channel, discord.TextChannel):
                log.warning(
                    "[Birthday] Announce channel %d not found in guild %s.",
                    ANNOUNCE_CHANNEL_ID, guild.name,
                )
                continue

            for row in rows:
                member = guild.get_member(row["user_id"])
                if not member:
                    log.debug(
                        "[Birthday] Member %d not in guild %s, skipping.",
                        row["user_id"], guild.name,
                    )
                    await db_mark_announced(row["user_id"], guild.id, today.year)
                    continue

                await self._announce_birthday(channel, member, row, today)
                await self._give_birthday_role(guild, member)
                await self._apply_birthday_nickname(guild, member)
                await db_mark_announced(member.id, guild.id, today.year)

    @birthday_loop.before_loop
    async def _before_birthday_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _announce_birthday(
        self,
        channel: discord.TextChannel,
        member: discord.Member,
        row: aiosqlite.Row,
        today: date,
    ) -> None:
        """Send the redesigned birthday announcement with banner image."""
        # ── Age string ───────────────────────────────────────────────────────
        age_str = "🎂 Unknown"
        if row["age"]:
            if row["year"]:
                age_now = today.year - row["year"]
                age_str = f"🎈 {_ordinal(age_now)} Birthday!"
            else:
                age_str = f"🎈 {_ordinal(row['age'])} Birthday!"

        date_str = _format_date(row["month"], row["day"], row["year"])

        # ── Build embed ──────────────────────────────────────────────────────
        embed = discord.Embed(
            title="🎉  Happy Birthday!",
            description=(
                f"🎉 It's time to celebrate!\n\n"
                f"Today is {member.mention}'s birthday! 🥳\n"
                f"Everyone drop some birthday wishes below! 🎊"
            ),
            colour=COL_GOLD,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="🎂 Age",            value=age_str,                 inline=True)
        embed.add_field(name="🎈 Date",           value=date_str,                inline=True)
        embed.add_field(name="🐶 Birthday Reward",value="Special birthday role + nickname!", inline=False)
        embed.set_footer(text="🐶 PupPet Birthday System")
        embed.timestamp = discord.utils.utcnow()

        # ── Banner image ─────────────────────────────────────────────────────
        file: Optional[discord.File] = None
        if BANNER_PATH.exists():
            file = discord.File(str(BANNER_PATH), filename="puppybd.png")
            embed.set_image(url="attachment://puppybd.png")
        else:
            log.warning(
                "[Birthday] Banner image not found at %s — sending without image.",
                BANNER_PATH,
            )

        ping_role = channel.guild.get_role(PING_ROLE_ID)
        ping      = ping_role.mention if ping_role else ""

        try:
            if file:
                await channel.send(content=ping, embed=embed, file=file)
            else:
                await channel.send(content=ping, embed=embed)
            log.info(
                "[Birthday] Announced birthday for %s (%d) in guild %s.",
                member.display_name, member.id, channel.guild.name,
            )
        except discord.Forbidden:
            log.warning("[Birthday] Missing send permission in channel %d.", channel.id)
        except discord.HTTPException as exc:
            log.error("[Birthday] Failed to send announcement: %s", exc)

    async def _give_birthday_role(self, guild: discord.Guild, member: discord.Member) -> None:
        role = guild.get_role(BIRTHDAY_ROLE_ID)
        if not role:
            log.warning("[Birthday] Birthday role %d not found in guild %s.", BIRTHDAY_ROLE_ID, guild.name)
            return
        if role in member.roles:
            return
        try:
            await member.add_roles(role, reason="Birthday 🎂")
            log.info("[Birthday] Gave birthday role to %s.", member.display_name)
        except discord.Forbidden:
            log.warning("[Birthday] Missing permissions to assign role in guild %s.", guild.name)
        except discord.HTTPException as exc:
            log.error("[Birthday] Failed to add role: %s", exc)

    async def _apply_birthday_nickname(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        """
        Save original nickname and prepend 🎂 prefix.
        Skips safely if:
          • Nickname already starts with the prefix (no duplicates).
          • Bot lacks Manage Nicknames permission.
          • Member is the guild owner (Discord restriction).
        """
        current_nick = member.nick  # None if they use their username

        # Already has the prefix — skip
        display = current_nick or member.name
        if display.startswith(BIRTHDAY_PREFIX):
            log.debug("[Birthday] %s already has birthday prefix, skipping.", member.display_name)
            return

        # Store original before mutating
        await db_save_original_nickname(member.id, guild.id, current_nick)

        new_nick = f"{BIRTHDAY_PREFIX}{current_nick or member.name}"
        # Discord nickname cap is 32 characters
        new_nick = new_nick[:32]

        try:
            await member.edit(nick=new_nick, reason="Birthday prefix 🎂")
            log.info("[Birthday] Applied birthday prefix to %s → '%s'.", member.name, new_nick)
        except discord.Forbidden:
            log.warning(
                "[Birthday] Cannot change nickname for %s in %s — missing perms or guild owner.",
                member.name, guild.name,
            )
        except discord.HTTPException as exc:
            log.error("[Birthday] Failed to set nickname for %s: %s", member.name, exc)

    async def _restore_yesterday_birthdays(
        self, guild: discord.Guild, yesterday: date
    ) -> None:
        """
        For every member whose birthday was yesterday:
          1. Remove birthday role.
          2. Restore original nickname.
          3. Delete the nickname record.
        """
        role = guild.get_role(BIRTHDAY_ROLE_ID)
        rows = await db_list_guild_birthdays(guild.id)

        for row in rows:
            if row["month"] != yesterday.month or row["day"] != yesterday.day:
                continue

            member = guild.get_member(row["user_id"])
            if not member:
                # Still clean up the nickname record if it exists
                await db_delete_nickname_record(row["user_id"], guild.id)
                continue

            # ── Remove birthday role ──────────────────────────────────────
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Birthday over 🎂")
                    log.info("[Birthday] Removed birthday role from %s.", member.display_name)
                except discord.Forbidden:
                    log.warning("[Birthday] Cannot remove role from %s — missing perms.", member.display_name)
                except discord.HTTPException as exc:
                    log.error("[Birthday] Failed to remove role: %s", exc)

            # ── Restore original nickname ─────────────────────────────────
            original_nick = await db_get_original_nickname(member.id, guild.id)
            if original_nick is not None or (
                member.nick and member.nick.startswith(BIRTHDAY_PREFIX)
            ):
                try:
                    await member.edit(nick=original_nick, reason="Birthday over — restoring nickname")
                    log.info(
                        "[Birthday] Restored nickname for %s → '%s'.",
                        member.name,
                        original_nick or "(none)",
                    )
                except discord.Forbidden:
                    log.warning(
                        "[Birthday] Cannot restore nickname for %s — missing perms or guild owner.",
                        member.name,
                    )
                except discord.HTTPException as exc:
                    log.error("[Birthday] Failed to restore nickname for %s: %s", member.name, exc)

            await db_delete_nickname_record(member.id, guild.id)

    async def _immediate_birthday_announce(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        row_data: dict,
    ) -> None:
        """Called when user sets their birthday and today IS their birthday."""
        guild = interaction.guild
        if not guild:
            return

        channel = guild.get_channel(ANNOUNCE_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            log.warning("[Birthday] Announce channel not found for immediate announce.")
            return

        today = _today_colombo()

        class _FakeRow:
            def __getitem__(self, key):
                return row_data[key]

        await self._announce_birthday(channel, member, _FakeRow(), today)
        await self._give_birthday_role(guild, member)
        await self._apply_birthday_nickname(guild, member)
        await db_mark_announced(member.id, guild.id, today.year)

    # ── Slash commands ────────────────────────────────────────────────────────

    @app_commands.command(
        name="mybd",
        description="Set or update your birthday. Usage: /mybd <year> <month> <day> <age>",
    )
    @app_commands.describe(
        year="Your birth year (optional, e.g. 1998) — use 0 to omit",
        month="Your birth month (1–12)",
        day="Your birth day (1–31)",
        age="Your current age",
    )
    async def mybd(
        self,
        interaction: discord.Interaction,
        year: int,
        month: int,
        day: int,
        age: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        clean_year: Optional[int] = year if year > 0 else None

        if not 1 <= month <= 12:
            await interaction.followup.send(
                embed=_error_embed("Invalid month. Please enter a value between 1 and 12."),
                ephemeral=True,
            )
            return

        if not _is_valid_date(clean_year, month, day):
            await interaction.followup.send(
                embed=_error_embed(
                    f"**{day}/{month}** is not a valid date. "
                    "Check the day/month combination (e.g. Feb only has 28–29 days)."
                ),
                ephemeral=True,
            )
            return

        if clean_year and (clean_year < 1900 or clean_year > _today_colombo().year):
            await interaction.followup.send(
                embed=_error_embed("Birth year must be between 1900 and the current year."),
                ephemeral=True,
            )
            return

        if not 0 <= age <= 150:
            await interaction.followup.send(
                embed=_error_embed("Age must be between 0 and 150."),
                ephemeral=True,
            )
            return

        try:
            await db_upsert_birthday(
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
                year=clean_year,
                month=month,
                day=day,
                age=age,
            )
        except Exception as exc:
            log.error("[Birthday] DB upsert failed: %s", exc, exc_info=True)
            await interaction.followup.send(
                embed=_error_embed("Database error while saving your birthday. Please try again."),
                ephemeral=True,
            )
            return

        date_str = _format_date(month, day, clean_year)
        embed = discord.Embed(
            title="🎂 Birthday Saved!",
            description=f"Your birthday has been set to **{date_str}**.",
            colour=COL_SUCCESS,
        )
        embed.add_field(name="🎈 Age", value=str(age), inline=True)
        embed.set_footer(text="🐶 PupPet Birthday System")
        await interaction.followup.send(embed=embed, ephemeral=True)

        today = _today_colombo()
        if today.month == month and today.day == day:
            row_data = {
                "user_id": interaction.user.id,
                "guild_id": interaction.guild_id,
                "year": clean_year,
                "month": month,
                "day": day,
                "age": age,
                "last_announced_year": None,
            }
            if isinstance(interaction.user, discord.Member):
                await self._immediate_birthday_announce(interaction, interaction.user, row_data)

    # ── /bdpanel ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="bdpanel",
        description="[Admin] Post the persistent birthday management panel.",
    )
    @admin_check()
    async def bdpanel(self, interaction: discord.Interaction) -> None:
        """Posts a public, persistent panel everyone can use (no command needed)."""
        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="🎂  Birthday Center",
            description=(
                "Set or manage your birthday using the buttons below.\n\n"
                "• 🎂 **Set Birthday** — Add or update your birthday\n"
                "• 📅 **View Birthday** — Check your saved birthday\n"
                "• ❌ **Remove Birthday** — Delete your birthday entry\n\n"
                "*You'll receive a special role and nickname on your big day!* 🎉"
            ),
            colour=COL_GOLD,
        )
        embed.set_footer(text="🐶 PupPet Birthday System")
        embed.timestamp = discord.utils.utcnow()

        # Attach banner if available
        file: Optional[discord.File] = None
        if BANNER_PATH.exists():
            file = discord.File(str(BANNER_PATH), filename="puppybd.png")
            embed.set_image(url="attachment://puppybd.png")
        else:
            log.warning("[Birthday] Banner not found at %s for /bdpanel.", BANNER_PATH)

        view = BirthdayPanelView()

        if isinstance(interaction.channel, discord.TextChannel):
            try:
                if file:
                    await interaction.channel.send(embed=embed, view=view, file=file)
                else:
                    await interaction.channel.send(embed=embed, view=view)
                await interaction.followup.send(
                    embed=discord.Embed(
                        description="✅ Birthday panel posted!",
                        colour=COL_SUCCESS,
                    ),
                    ephemeral=True,
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    embed=_error_embed("I don't have permission to send messages in this channel."),
                    ephemeral=True,
                )
        else:
            await interaction.followup.send(
                embed=_error_embed("This command must be used in a text channel."),
                ephemeral=True,
            )

    # ── /birthday group ───────────────────────────────────────────────────────

    birthday_group = app_commands.Group(
        name="birthday",
        description="Birthday commands",
    )

    @birthday_group.command(name="view", description="View your saved birthday and countdown.")
    async def birthday_view(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        row = await db_get_birthday(interaction.user.id, interaction.guild_id)
        if not row:
            await interaction.followup.send(
                embed=_error_embed("You haven't set a birthday yet. Use **/mybd** to add one!"),
                ephemeral=True,
            )
            return

        today     = _today_colombo()
        days_left = _days_until_next_birthday(row["month"], row["day"])
        date_str  = _format_date(row["month"], row["day"], row["year"])

        if row["year"]:
            age_display = str(today.year - row["year"])
        elif row["age"]:
            age_display = str(row["age"])
        else:
            age_display = "Not provided"

        countdown = "🎉 **Today is your birthday!**" if days_left == 0 else f"**{days_left}** days away"

        embed = discord.Embed(
            title=f"🎂 {interaction.user.display_name}'s Birthday",
            colour=COL_PINK,
        )
        embed.add_field(name="📅 Birthday",      value=date_str,    inline=True)
        embed.add_field(name="🎈 Age",           value=age_display, inline=True)
        embed.add_field(name="⏳ Next Birthday", value=countdown,   inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="🐶 PupPet Birthday System")
        embed.timestamp = discord.utils.utcnow()

        await interaction.followup.send(embed=embed, ephemeral=True)

    @birthday_group.command(name="remove", description="Remove your birthday from the database.")
    async def birthday_remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        deleted = await db_delete_birthday(interaction.user.id, interaction.guild_id)
        if deleted:
            embed = discord.Embed(
                title="🗑️ Birthday Removed",
                description="Your birthday has been removed from the database.",
                colour=COL_SUCCESS,
            )
        else:
            embed = _error_embed("You don't have a birthday saved, so there's nothing to remove.")

        embed.set_footer(text="🐶 PupPet Birthday System")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @birthday_group.command(name="list", description="List all birthdays in this server.")
    async def birthday_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        rows = await db_list_guild_birthdays(interaction.guild_id)
        if not rows:
            await interaction.followup.send(
                embed=_error_embed("No birthdays have been set in this server yet."),
                ephemeral=True,
            )
            return

        pages = _build_list_pages(rows, interaction.guild, interaction.guild_id)
        view  = _PaginatorView(pages, interaction)
        await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    @birthday_group.command(
        name="upcoming",
        description="Show the next 10 upcoming birthdays in this server.",
    )
    async def birthday_upcoming(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        rows = await db_list_guild_birthdays(interaction.guild_id)
        if not rows:
            await interaction.followup.send(
                embed=_error_embed("No birthdays have been set in this server yet."),
                ephemeral=True,
            )
            return

        sorted_rows = sorted(rows, key=lambda r: _days_until_next_birthday(r["month"], r["day"]))
        top = sorted_rows[:10]

        embed = discord.Embed(
            title="🎉 Upcoming Birthdays",
            description=f"Next **{len(top)}** birthdays in **{interaction.guild.name}**",
            colour=COL_PINK,
        )

        for i, row in enumerate(top, start=1):
            days_left = _days_until_next_birthday(row["month"], row["day"])
            member    = interaction.guild.get_member(row["user_id"])
            name      = member.display_name if member else f"User {row['user_id']}"
            date_str  = _format_date(row["month"], row["day"], None)

            if days_left == 0:
                countdown = "🎂 **Today!**"
            elif days_left == 1:
                countdown = "**Tomorrow!**"
            else:
                countdown = f"in **{days_left}** days"

            embed.add_field(
                name=f"{i}. {name}",
                value=f"📅 {date_str} — {countdown}",
                inline=False,
            )

        embed.set_footer(text="🐶 PupPet Birthday System")
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# Paginator view for /birthday list
# ══════════════════════════════════════════════════════════════════════════════

class _PaginatorView(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], interaction: discord.Interaction) -> None:
        super().__init__(timeout=120)
        self.pages       = pages
        self.current     = 0
        self.interaction = interaction
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await self.interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Utility / embed builders
# ══════════════════════════════════════════════════════════════════════════════

def _error_embed(message: str) -> discord.Embed:
    embed = discord.Embed(description=f"❌ {message}", colour=COL_ERROR)
    embed.set_footer(text="🐶 PupPet Birthday System")
    return embed


def _build_list_pages(
    rows: list[aiosqlite.Row],
    guild: discord.Guild,
    guild_id: int,
) -> list[discord.Embed]:
    pages: list[discord.Embed] = []
    chunks     = [rows[i : i + PAGE_SIZE] for i in range(0, len(rows), PAGE_SIZE)]
    total_pages = len(chunks)

    for page_num, chunk in enumerate(chunks, start=1):
        embed = discord.Embed(
            title=f"🎂 Birthday List — {guild.name}",
            description=f"Total birthdays: **{len(rows)}**",
            colour=COL_PINK,
        )
        for row in chunk:
            member    = guild.get_member(row["user_id"])
            name      = member.display_name if member else f"User {row['user_id']}"
            date_str  = _format_date(row["month"], row["day"], None)
            days_left = _days_until_next_birthday(row["month"], row["day"])

            countdown = " — 🎂 Today!" if days_left == 0 else f" — in {days_left}d"

            embed.add_field(
                name=f"📅 {date_str}",
                value=f"{name}{countdown}",
                inline=False,
            )
        embed.set_footer(text=f"Page {page_num}/{total_pages} • 🐶 PupPet Birthday System")
        embed.timestamp = discord.utils.utcnow()
        pages.append(embed)

    return pages


# ══════════════════════════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    """Entry point called by discord.py's load_extension."""
    await bot.add_cog(Birthday(bot))