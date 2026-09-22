import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import asyncio
import hashlib
import html
import io
import logging
import re
import os
import unicodedata
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional

from utils.migrations import Migration, run_migrations

# ─── Logging ─────────────────────────────────────────────────────────────────
logger = logging.getLogger("puppet.moderation")
logger.setLevel(logging.INFO)

# ─── Constants ────────────────────────────────────────────────────────────────
BACKED_PUPPY_ROLE_ID = 1512042447981510709
ADMIN_ROLE_ID         = 1551515669026177077
SECOND_ADMIN_ROLE_ID  = 1551515670553042995
LOG_CHANNEL_ID        = 1551515726995529778
YOUTUBE_CHANNEL_ID    = 1511774999361491131

SPAM_MSG_LIMIT        = 5
SPAM_SECONDS          = 5
SPAM_MUTE_SECONDS     = 30
MULTI_CHANNEL_LIMIT   = 3
MULTI_CHANNEL_TIMEOUT = timedelta(minutes=5)
YOUTUBE_TIMEOUT       = timedelta(minutes=3)
MAX_EMOJI_COUNT       = 20
MAX_MENTIONS          = 5
CAPS_MIN_LEN          = 10
CAPS_RATIO            = 0.90

# Mod-action cooldown: same moderator cannot apply same action to same user
# within this many seconds (prevents accidental double-punishments).
MOD_COOLDOWN_SECONDS  = 10

# Alt-detection thresholds
ALT_ACCOUNT_AGE_DAYS  = 30   # accounts younger than this are flagged
ALT_JOIN_AGE_DAYS     = 7    # accounts that joined recently are flagged

YOUTUBE_PATTERN = re.compile(
    r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/\S+", re.IGNORECASE
)
URL_PATTERN = re.compile(
    r"(https?://|discord\.gg/|discord\.com/invite/)\S+", re.IGNORECASE
)
INVITE_PATTERN = re.compile(
    r"(discord\.gg/|discord\.com/invite/)\S+", re.IGNORECASE
)

BAD_WORDS: list[str] = [
    "badword1",
    "badword2",
]

from pathlib import Path
DB_PATH = Path(__file__).parent.parent / "data" / "moderation.db"

# ─── Database ─────────────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    os.makedirs(DB_PATH.parent, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


MIGRATIONS = [
    Migration(1, "initial schema — cases, mutes, logs, tickets, etc.", """
        CREATE TABLE IF NOT EXISTS cases (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            moderator_id  INTEGER,
            action        TEXT    NOT NULL,
            reason        TEXT,
            timestamp     TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS active_channel_mutes (
            user_id    INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            expires    TEXT    NOT NULL,
            PRIMARY KEY (user_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS ghost_ping_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            mentioned   TEXT    NOT NULL,
            timestamp   TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staff_audit_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id      INTEGER NOT NULL,
            staff_tag     TEXT    NOT NULL,
            action        TEXT    NOT NULL,
            target_id     INTEGER,
            target_tag    TEXT,
            reason        TEXT,
            guild_id      INTEGER NOT NULL,
            timestamp     TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS timeout_roles (
            user_id   INTEGER NOT NULL,
            guild_id  INTEGER NOT NULL,
            role_ids  TEXT    NOT NULL,
            PRIMARY KEY (user_id, guild_id)
        );

        CREATE TABLE IF NOT EXISTS spam_counter (
            user_id  INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, guild_id)
        );

        CREATE TABLE IF NOT EXISTS alt_flags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            guild_id   INTEGER NOT NULL,
            reason     TEXT    NOT NULL,
            timestamp  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            guild_id     INTEGER NOT NULL,
            channel_id   INTEGER NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'open',
            opened_at    TEXT    NOT NULL,
            closed_at    TEXT,
            case_id      INTEGER
        );
    """),

    # Example of what a FUTURE change looks like — uncomment and adapt
    # whenever you actually need to add a column:
    #
    # Migration(2, "add severity level to cases", """
    #     ALTER TABLE cases ADD COLUMN severity TEXT DEFAULT 'normal';
    # """),
]


def init_db() -> None:
    with get_connection() as con:
        run_migrations(con, MIGRATIONS, db_name="moderation.db")


def add_case(
    user_id: int,
    moderator_id: Optional[int],
    action: str,
    reason: Optional[str],
) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with get_connection() as con:
        cur = con.execute(
            "INSERT INTO cases (user_id, moderator_id, action, reason, timestamp) VALUES (?,?,?,?,?)",
            (user_id, moderator_id, action, reason, ts),
        )
        con.commit()
        return cur.lastrowid  # type: ignore[return-value]


def get_history(user_id: int, limit: int = 25) -> list[sqlite3.Row]:
    with get_connection() as con:
        return con.execute(
            "SELECT * FROM cases WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def add_staff_audit(
    staff: discord.Member,
    action: str,
    target: Optional[discord.Member | discord.User],
    reason: Optional[str],
    guild_id: int,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as con:
            con.execute(
                """INSERT INTO staff_audit_log
                   (staff_id, staff_tag, action, target_id, target_tag, reason, guild_id, timestamp)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    staff.id,
                    str(staff),
                    action,
                    target.id if target else None,
                    str(target) if target else None,
                    reason,
                    guild_id,
                    ts,
                ),
            )
            con.commit()
    except Exception as e:
        logger.error("Staff audit log error: %s", e)


def increment_spam_counter(user_id: int, guild_id: int) -> int:
    with get_connection() as con:
        con.execute(
            """INSERT INTO spam_counter (user_id, guild_id, count) VALUES (?,?,1)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET count = count + 1""",
            (user_id, guild_id),
        )
        con.commit()
        row = con.execute(
            "SELECT count FROM spam_counter WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ).fetchone()
        return row["count"] if row else 1


def get_spam_count(user_id: int, guild_id: int) -> int:
    with get_connection() as con:
        row = con.execute(
            "SELECT count FROM spam_counter WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ).fetchone()
        return row["count"] if row else 0


def save_timeout_roles(user_id: int, guild_id: int, role_ids: list[int]) -> None:
    with get_connection() as con:
        con.execute(
            """INSERT INTO timeout_roles (user_id, guild_id, role_ids) VALUES (?,?,?)
               ON CONFLICT(user_id, guild_id) DO UPDATE SET role_ids=excluded.role_ids""",
            (user_id, guild_id, ",".join(str(r) for r in role_ids)),
        )
        con.commit()


def pop_timeout_roles(user_id: int, guild_id: int) -> list[int]:
    with get_connection() as con:
        row = con.execute(
            "SELECT role_ids FROM timeout_roles WHERE user_id=? AND guild_id=?",
            (user_id, guild_id),
        ).fetchone()
        if row:
            con.execute(
                "DELETE FROM timeout_roles WHERE user_id=? AND guild_id=?",
                (user_id, guild_id),
            )
            con.commit()
            return [int(r) for r in row["role_ids"].split(",") if r]
        return []


def flag_alt(user_id: int, guild_id: int, reason: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with get_connection() as con:
        con.execute(
            "INSERT INTO alt_flags (user_id, guild_id, reason, timestamp) VALUES (?,?,?,?)",
            (user_id, guild_id, reason, ts),
        )
        con.commit()


def get_alt_flags(user_id: int, guild_id: int) -> list[sqlite3.Row]:
    with get_connection() as con:
        return con.execute(
            "SELECT * FROM alt_flags WHERE user_id=? AND guild_id=? ORDER BY id DESC",
            (user_id, guild_id),
        ).fetchall()


# ─── Permission helpers ───────────────────────────────────────────────────────

def is_admin(member: discord.Member) -> bool:
    if member.id == 1371080029265465406:
        return True
    return any(r.id in (ADMIN_ROLE_ID, SECOND_ADMIN_ROLE_ID) for r in member.roles)


def admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if is_admin(interaction.user):
            return True
        embed = discord.Embed(
            title="🐶 Access Denied — Raw Puppy",
            description=(
                "ඔයාට මේ command use කරන්න permission නෑ.\n\n"
                "This command is restricted to:\n"
                f"<@&{ADMIN_ROLE_ID}> and <@&{SECOND_ADMIN_ROLE_ID}> only."
            ),
            color=0xFF4444,
        )
        embed.set_footer(text="🐶 PupPet Moderation — Admins Only")
        embed.timestamp = datetime.now(timezone.utc)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return app_commands.check(predicate)


# ─── Embed helpers ────────────────────────────────────────────────────────────

PUPPY_COLOR = 0xD4A017


def base_embed(title: str, description: str = "", color: int = PUPPY_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="🐶 PupPet Moderation")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def user_display(user: discord.User | discord.Member) -> str:
    return f"{user.mention} (`{user.id}` — **{user}**)"


# ─── Modals ───────────────────────────────────────────────────────────────────

class ReasonModal(discord.ui.Modal):
    reason_input = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Enter the reason for this action…",
        required=False,
        max_length=512,
    )

    def __init__(self, title: str, callback):
        super().__init__(title=title)
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._callback(interaction, self.reason_input.value or "No reason provided.")


class FreezeModal(discord.ui.Modal, title="🐶 Puppy Freeze — Duration & Reason"):
    duration_input = discord.ui.TextInput(
        label="Duration (minutes)",
        placeholder="e.g. 60",
        required=True,
        max_length=6,
    )
    reason_input = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Enter the reason…",
        required=False,
        max_length=512,
    )

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            minutes = int(self.duration_input.value.strip())
            if minutes <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "🐶 Invalid duration. Enter a positive number of minutes.", ephemeral=True
            )
            return
        reason = self.reason_input.value or "No reason provided."
        await self._callback(interaction, minutes, reason)


# ─── Confirmation View (prevents accidental punishments) ─────────────────────

class ConfirmView(discord.ui.View):
    """Two-button confirm / cancel gate for destructive actions."""

    def __init__(self, action_label: str, on_confirm, on_cancel=None):
        super().__init__(timeout=30)
        self._on_confirm = on_confirm
        self._on_cancel  = on_cancel
        self._action_label = action_label

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await self._on_confirm(interaction)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self._on_cancel:
            await self._on_cancel(interaction)
        else:
            await interaction.response.edit_message(
                content="🐶 Action cancelled.", embed=None, view=self
            )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        # Note: ephemeral messages can't be edited on timeout without an active
        # interaction — buttons will appear clickable but fire an "interaction failed"
        # error on Discord's side, which is acceptable UX for a 30s window.


# ─── Mod Panel View ───────────────────────────────────────────────────────────

class ModPanelView(discord.ui.View):
    def __init__(self, cog: "PuppyModeration", target: discord.Member):
        super().__init__(timeout=300)
        self.cog    = cog
        self.target = target

    # ── admin gate ──────────────────────────────────────────────────────────

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not is_admin(interaction.user):
            await interaction.response.send_message("🐶 Admins only!", ephemeral=True)
            return False
        return True

    # ── cooldown gate ────────────────────────────────────────────────────────

    async def _check_cooldown(
        self,
        interaction: discord.Interaction,
        action_key: str,
    ) -> bool:
        """Returns True if action may proceed, False (+ reply) if on cooldown."""
        key = (interaction.user.id, self.target.id, action_key)
        blocked = self.cog._mod_cooldowns.get(key)
        if blocked and blocked > datetime.now(timezone.utc):
            remaining = (blocked - datetime.now(timezone.utc)).seconds
            await interaction.response.send_message(
                f"🐶 Cooldown active. Wait **{remaining}s** before repeating this action.",
                ephemeral=True,
            )
            return False
        self.cog._mod_cooldowns[key] = datetime.now(timezone.utc) + timedelta(seconds=MOD_COOLDOWN_SECONDS)
        return True

    # ── Ban ──────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Puppy Exile (Ban)", style=discord.ButtonStyle.danger, row=0)
    async def btn_ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        if not await self._check_cooldown(interaction, "ban"): return

        async def after_reason(reason_inter: discord.Interaction, reason: str):
            confirm_embed = base_embed(
                "⚠️ Confirm Puppy Exile",
                f"You are about to **permanently ban** {user_display(self.target)}.\n**Reason:** {reason}\n\nAre you sure?",
                color=0xFF4444,
            )
            async def do_ban(conf_inter: discord.Interaction):
                try:
                    await self.cog.apply_backed_puppy(conf_inter.guild, self.target)
                    await self.target.ban(reason=reason)
                    case_id = await asyncio.to_thread(add_case, self.target.id, conf_inter.user.id, "Ban", reason)
                    add_staff_audit(conf_inter.user, "Ban", self.target, reason, conf_inter.guild.id)
                    embed = base_embed(
                        "🐶 Puppy Exile Executed",
                        f"{user_display(self.target)} has been exiled.\n**Reason:** {reason}\n**Case:** #{case_id}",
                        color=0xFF4444,
                    )
                    await conf_inter.response.edit_message(content=None, embed=embed, view=None)
                    await self.cog.send_log(embed)
                except discord.Forbidden:
                    await conf_inter.response.edit_message(content="🐶 Missing permissions.", embed=None, view=None)
                except Exception as e:
                    logger.error("Ban error: %s", e)
            view = ConfirmView("Ban", do_ban)
            await reason_inter.response.send_message(embed=confirm_embed, view=view, ephemeral=True)

        await interaction.response.send_modal(ReasonModal("🐶 Puppy Exile Reason", after_reason))

    # ── Kick ─────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Puppy Yeet (Kick)", style=discord.ButtonStyle.danger, row=0)
    async def btn_kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        if not await self._check_cooldown(interaction, "kick"): return

        async def after_reason(reason_inter: discord.Interaction, reason: str):
            confirm_embed = base_embed(
                "⚠️ Confirm Puppy Yeet",
                f"You are about to **kick** {user_display(self.target)}.\n**Reason:** {reason}\n\nAre you sure?",
                color=0xFF8800,
            )
            async def do_kick(conf_inter: discord.Interaction):
                try:
                    await self.cog.apply_backed_puppy(conf_inter.guild, self.target)
                    await self.target.kick(reason=reason)
                    case_id = await asyncio.to_thread(add_case, self.target.id, conf_inter.user.id, "Kick", reason)
                    add_staff_audit(conf_inter.user, "Kick", self.target, reason, conf_inter.guild.id)
                    embed = base_embed(
                        "🐶 Puppy Yeet Executed",
                        f"{user_display(self.target)} has been yeeted.\n**Reason:** {reason}\n**Case:** #{case_id}",
                        color=0xFF8800,
                    )
                    await conf_inter.response.edit_message(content=None, embed=embed, view=None)
                    await self.cog.send_log(embed)
                except discord.Forbidden:
                    await conf_inter.response.edit_message(content="🐶 Missing permissions.", embed=None, view=None)
                except Exception as e:
                    logger.error("Kick error: %s", e)
            view = ConfirmView("Kick", do_kick)
            await reason_inter.response.send_message(embed=confirm_embed, view=view, ephemeral=True)

        await interaction.response.send_modal(ReasonModal("🐶 Puppy Yeet Reason", after_reason))

    # ── Timeout ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Puppy Freeze (Mute)", style=discord.ButtonStyle.primary, row=0)
    async def btn_freeze(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        if not await self._check_cooldown(interaction, "freeze"): return

        async def do_freeze(modal_inter: discord.Interaction, minutes: int, reason: str):
            try:
                # Save roles before timeout for restore later
                role_ids = [r.id for r in self.target.roles if not r.is_default() and not r.managed]
                save_timeout_roles(self.target.id, modal_inter.guild.id, role_ids)

                until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                await self.cog.apply_backed_puppy(modal_inter.guild, self.target)
                await self.target.timeout(until, reason=reason)
                case_id = await asyncio.to_thread(add_case, self.target.id, modal_inter.user.id, f"Timeout ({minutes}m)", reason)
                add_staff_audit(modal_inter.user, f"Timeout ({minutes}m)", self.target, reason, modal_inter.guild.id)

                embed = base_embed(
                    "🐶 Puppy Freeze Executed",
                    f"{user_display(self.target)} frozen for **{minutes}m**.\n**Reason:** {reason}\n**Case:** #{case_id}\n**Roles saved for restore:** {len(role_ids)}",
                    color=0x44AAFF,
                )
                await modal_inter.response.send_message(embed=embed, ephemeral=True)
                await self.cog.send_log(embed)
            except discord.Forbidden:
                await modal_inter.response.send_message("🐶 Missing permissions.", ephemeral=True)
            except Exception as e:
                logger.error("Timeout error: %s", e)

        await interaction.response.send_modal(FreezeModal(do_freeze))

    # ── Unfreeze ──────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Puppy Unfreeze (Unmute)", style=discord.ButtonStyle.secondary, row=0)
    async def btn_unfreeze(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        try:
            await self.target.timeout(None)
            restored = await self.cog.restore_roles_after_timeout(self.target, interaction.guild)
            await self.cog.remove_backed_puppy(interaction.guild, self.target)
            case_id = await asyncio.to_thread(add_case, self.target.id, interaction.user.id, "Unfreeze", "Manually unfrozen")
            add_staff_audit(interaction.user, "Unfreeze", self.target, "Manually unfrozen", interaction.guild.id)
            embed = base_embed(
                "🐶 Puppy Unfreeze Executed",
                f"{user_display(self.target)} has been thawed.\n**Roles restored:** {restored}\n**Backed Puppy role removed.**\n**Case:** #{case_id}",
                color=0x44FF99,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await self.cog.send_log(embed)
        except discord.Forbidden:
            await interaction.response.send_message("🐶 Missing permissions.", ephemeral=True)
        except Exception as e:
            logger.error("Unfreeze error: %s", e)

    # ── Unban ─────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Puppy Return (Unban)", style=discord.ButtonStyle.success, row=1)
    async def btn_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        if not await self._check_cooldown(interaction, "unban"): return

        async def after_reason(reason_inter: discord.Interaction, reason: str):
            async def do_unban(conf_inter: discord.Interaction):
                try:
                    await conf_inter.guild.unban(self.target, reason=reason)
                    case_id = await asyncio.to_thread(add_case, self.target.id, conf_inter.user.id, "Unban", reason)
                    add_staff_audit(conf_inter.user, "Unban", self.target, reason, conf_inter.guild.id)
                    # Try to remove Backed Puppy role if the user is still in the guild
                    member_in_guild = conf_inter.guild.get_member(self.target.id)
                    if member_in_guild:
                        await self.cog.remove_backed_puppy(conf_inter.guild, member_in_guild)
                    embed = base_embed(
                        "🐶 Puppy Return Executed",
                        f"{user_display(self.target)} has been allowed back.\n**Reason:** {reason}\n**Backed Puppy role removed (if present).**\n**Case:** #{case_id}",
                        color=0x44FF44,
                    )
                    await conf_inter.response.edit_message(content=None, embed=embed, view=None)
                    await self.cog.send_log(embed)
                except discord.NotFound:
                    await conf_inter.response.edit_message(content="🐶 User is not banned.", embed=None, view=None)
                except discord.Forbidden:
                    await conf_inter.response.edit_message(content="🐶 Missing permissions.", embed=None, view=None)
                except Exception as e:
                    logger.error("Unban error: %s", e)
            confirm_embed = base_embed(
                "⚠️ Confirm Puppy Return",
                f"Unban {user_display(self.target)}?\n**Reason:** {reason}",
                color=0x44FF44,
            )
            await reason_inter.response.send_message(
                embed=confirm_embed, view=ConfirmView("Unban", do_unban), ephemeral=True
            )

        await interaction.response.send_modal(ReasonModal("🐶 Puppy Return Reason", after_reason))

    # ── Add Backed Puppy ──────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Add Backed Puppy (Role)", style=discord.ButtonStyle.secondary, row=1)
    async def btn_backed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        await self.cog.apply_backed_puppy(interaction.guild, self.target)
        add_staff_audit(interaction.user, "Add Backed Puppy Role", self.target, None, interaction.guild.id)
        embed = base_embed("🐶 Backed Puppy Role Added", f"{user_display(self.target)} is now a Backed Puppy.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.cog.send_log(embed)

    # ── Remove Backed Puppy ──────────────────────────────────────────────────

    @discord.ui.button(label="🐶 Remove Backed Puppy (Role)", style=discord.ButtonStyle.danger, row=2)
    async def btn_remove_backed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        await self.cog.remove_backed_puppy(interaction.guild, self.target)
        add_staff_audit(interaction.user, "Remove Backed Puppy Role", self.target, None, interaction.guild.id)
        embed = base_embed("🐶 Backed Puppy Role Removed", f"{user_display(self.target)} — Backed Puppy role removed.", color=0x44FF99)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await self.cog.send_log(embed)

    # ── User Info ──────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 User Info (Profile)", style=discord.ButtonStyle.secondary, row=2)
    async def btn_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        m = self.target
        created_at = discord.utils.format_dt(m.created_at, "F")
        joined_at  = discord.utils.format_dt(m.joined_at, "F") if m.joined_at else "Unknown"
        timeout_status = (
            f"Frozen until {discord.utils.format_dt(m.timed_out_until, 'R')}"
            if m.timed_out_until and m.timed_out_until > datetime.now(timezone.utc)
            else "Not frozen"
        )
        roles  = [r.mention for r in m.roles if not r.is_default()]
        highest = m.top_role.mention if not m.top_role.is_default() else "None"
        alt_flags = await asyncio.to_thread(get_alt_flags, m.id, interaction.guild.id)
        spam_cnt  = await asyncio.to_thread(get_spam_count, m.id, interaction.guild.id)

        embed = base_embed(
            "🐶 Puppy File",
            f"**User:** {m.mention} (`{m.id}`)\n"
            f"**Username:** {m}\n"
            f"**Account Created:** {created_at}\n"
            f"**Joined Server:** {joined_at}\n"
            f"**Roles ({len(roles)}):** {', '.join(roles) or 'None'}\n"
            f"**Highest Role:** {highest}\n"
            f"**Timeout Status:** {timeout_status}\n"
            f"**Spam Triggers:** {spam_cnt}\n"
            f"**Alt Flags:** {len(alt_flags)}",
        )
        embed.set_thumbnail(url=m.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── History ────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🐶 History (Cases)", style=discord.ButtonStyle.secondary, row=2)
    async def btn_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = await asyncio.to_thread(get_history, self.target.id, 10)
        if not rows:
            embed = base_embed("🐶 Puppy History", "No moderation cases found for this user.")
        else:
            lines = []
            for r in rows:
                ts = r["timestamp"][:10]
                lines.append(
                    f"**#{r['id']}** `{r['action']}` — {r['reason'] or 'No reason'} "
                    f"*(by <@{r['moderator_id']}> on {ts})*"
                )
            embed = base_embed(
                "🐶 Puppy History",
                f"Last **{len(rows)}** cases for {self.target.mention}\n\n" + "\n".join(lines),
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Ticket ─────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🎫 Open Ticket (Support)", style=discord.ButtonStyle.secondary, row=3)
    async def btn_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._require_admin(interaction): return
        cog: "PuppyModeration" = self.cog
        await cog.open_ticket(interaction, self.target)


# ─── Help pages ──────────────────────────────────────────────────────────────

_HELP_PAGES: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "🐶 Mod Panel & Core Commands",
        "Main moderation commands — admin only.",
        [
            ("/mod @user", "Opens the full **Puppy Justice Panel** for a member.\nButtons: Exile, Yeet, Freeze, Unfreeze, Return, Backed Puppy, User Info, History, Open Ticket."),
            ("/cases @user", "Shows the full moderation case history for a user (up to 25 cases, paginated)."),
            ("/clearcases @user", "Permanently deletes all moderation cases for a user (requires confirmation button)."),
            ("/puppycheck @user", "Generates a **risk score** (0–100+) based on account age, bans, kicks, timeouts, spam triggers, and alt flags."),
            ("/staffaudit [limit]", "Shows recent staff action audit log entries from the database. Default: 20, max: 50."),
            ("/exporthistory @user [txt|html]", "Exports a user's full case history as a downloadable `.txt` plain file or a styled `.html` transcript."),
            ("/modhelp", "Shows this help panel."),
        ],
    ),
    (
        "🐶 Mod Panel Buttons",
        "Buttons available inside `/mod` — all require Admin or Second Admin role.",
        [
            ("🐶 Puppy Exile", "**Ban** the user. Opens a reason modal → confirmation buttons. Automatically adds Backed Puppy role."),
            ("🐶 Puppy Yeet", "**Kick** the user. Opens a reason modal → confirmation buttons. Automatically adds Backed Puppy role."),
            ("🐶 Puppy Freeze", "**Timeout** the user. Opens a modal asking for duration (minutes) and reason. Saves current roles for automatic restore."),
            ("🐶 Puppy Unfreeze", "Removes timeout and **restores saved roles** immediately."),
            ("🐶 Puppy Return", "**Unban** the user. Opens a reason modal → confirmation buttons."),
            ("🐶 Add Backed Puppy", "Manually adds the Backed Puppy role to the user."),
            ("🐶 User Info", "Shows account age, join date, roles, timeout status, spam count, and alt flag count."),
            ("🐶 History", "Shows the last 10 moderation cases for the user."),
            ("🎫 Open Ticket", "Creates a **private channel** visible to the target user + all admins. Saved to the tickets database table."),
        ],
    ),
    (
        "🐶 Auto-Moderation — Messages",
        "Automatic protections that run on every message.",
        [
            ("Bad word filter", "Deletes messages containing words from the configured `BAD_WORDS` list. No punishment — silent delete only."),
            ("Anti-link", "Non-admins cannot send `http://`, `https://`, or any URL. Message is deleted and logged."),
            ("Invite blocker", "Blocks `discord.gg/` and `discord.com/invite/` links for non-admins. Deleted and logged."),
            ("Anti @everyone / @here", "Only admins may use @everyone or @here. Message deleted and logged."),
            ("Caps spam", "Messages over 10 characters with 90%+ uppercase are silently deleted. No punishment."),
            ("Emoji spam", "Messages with 20+ emojis are silently deleted. No punishment."),
            ("Mass mention", "Messages mentioning more than 5 users are deleted and logged."),
            ("YouTube-only channel", f"In <#{YOUTUBE_CHANNEL_ID}> only YouTube links are allowed. Anything else → message deleted + **3 minute timeout** + Backed Puppy role added."),
        ],
    ),
    (
        "🐶 Auto-Moderation — Spam",
        "Spam detection and progressive punishment.",
        [
            ("Message spam", f"If a user sends **{SPAM_MSG_LIMIT}+ messages within {SPAM_SECONDS}s** in a channel → channel-specific mute for **{SPAM_MUTE_SECONDS}s** (can still talk elsewhere)."),
            ("Duplicate message spam", f"Sending the same message {SPAM_MSG_LIMIT}+ times in {SPAM_SECONDS}s is treated identically to message spam."),
            ("Multi-channel spam", f"If the same user gets muted in **{MULTI_CHANNEL_LIMIT}+ channels** → server-wide **5 minute timeout** + case logged."),
            ("Spam counter", "Every spam trigger increments a persistent counter per user (visible in `/puppycheck` and User Info button)."),
        ],
    ),
    (
        "🐶 Auto-Moderation — Tracking",
        "Background systems that monitor and log user behaviour.",
        [
            ("Ghost ping detection", "Tracks all messages with mentions. If the message is deleted after being sent, a log is sent to the log channel with the pinger, mentioned users, channel, and timestamp."),
            ("Alt account detection", "On every member join, checks: account age < 30 days, default avatar (no custom avatar), and matching avatar hash with existing members. Any match → logged to the log channel and saved to the `alt_flags` database table."),
            ("Role restore after timeout", "When a Freeze (timeout) is applied via the mod panel, all current roles are saved to the database. When the timeout expires (background check every 30s) or Unfreeze is pressed, roles are automatically restored."),
            ("Mod action cooldown", f"The same moderator cannot apply the same action to the same user within **{MOD_COOLDOWN_SECONDS} seconds**. Prevents accidental duplicate punishments."),
        ],
    ),
    (
        "🐶 Logging & Database",
        "Everything is logged and stored.",
        [
            ("Log channel", f"All events are sent to <#{LOG_CHANNEL_ID}>: bans, kicks, timeouts, unbans, link/invite deletions, bad word deletions, spam mutes, ghost pings, mass mentions, YouTube violations, @everyone abuse, alt detections, role restores."),
            ("cases table", "Every moderation action creates a case: `id`, `user_id`, `moderator_id`, `action`, `reason`, `timestamp`."),
            ("staff_audit_log table", "Every staff action (including exports and case clears) is recorded with staff ID, tag, action, target, reason, and timestamp."),
            ("spam_counter table", "Persistent spam trigger count per user per guild."),
            ("alt_flags table", "Stores all alt account detection flags with reason and timestamp."),
            ("timeout_roles table", "Stores role IDs saved before a timeout so they can be restored automatically."),
            ("ghost_ping_log table", "Stores all ghost ping events."),
            ("tickets table", "Stores all opened mod tickets with status, channel ID, and timestamps."),
        ],
    ),
    (
        "🐶 Roles & Channels",
        "Configured role and channel IDs for this server.",
        [
            ("Admin role", f"<@&{ADMIN_ROLE_ID}> — full access to all mod commands, exempt from auto-mod."),
            ("Second Admin role", f"<@&{SECOND_ADMIN_ROLE_ID}> — same permissions as Admin role."),
            ("Backed Puppy role", f"<@&{BACKED_PUPPY_ROLE_ID}> — automatically added to any user who receives a ban, kick, or timeout."),
            ("Log channel", f"<#{LOG_CHANNEL_ID}> — all moderation events are sent here."),
            ("YouTube channel", f"<#{YOUTUBE_CHANNEL_ID}> — only YouTube links allowed; all other content triggers a 3-minute timeout."),
            ("Database path", "`data/moderation.db` — SQLite, auto-created on bot start."),
        ],
    ),
]


class ModHelpView(discord.ui.View):
    """Paginated /modhelp embed navigator."""

    TOTAL_PAGES = len(_HELP_PAGES)

    def __init__(self, page: int = 0) -> None:
        super().__init__(timeout=120)
        self._page = page
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.btn_prev.disabled = self._page == 0
        self.btn_next.disabled = self._page == self.TOTAL_PAGES - 1
        self.btn_page.label    = f"{self._page + 1} / {self.TOTAL_PAGES}"

    @staticmethod
    def build_page(index: int) -> discord.Embed:
        title, subtitle, entries = _HELP_PAGES[index]
        lines: list[str] = [f"*{subtitle}*\n"]
        for name, value in entries:
            lines.append(f"**`{name}`**\n{value}\n")
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=PUPPY_COLOR,
        )
        embed.set_footer(text=f"🐶 PupPet Moderation Help  •  Page {index + 1}/{len(_HELP_PAGES)}")
        embed.timestamp = datetime.now(timezone.utc)
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._page = max(0, self._page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_page(self._page), view=self)

    @discord.ui.button(label="1 / 7", style=discord.ButtonStyle.secondary, disabled=True)
    async def btn_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pass  # display-only

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._page = min(self.TOTAL_PAGES - 1, self._page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_page(self._page), view=self)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="🐶 Help panel closed.", embed=None, view=None
        )


# ─── Main Cog ─────────────────────────────────────────────────────────────────

class PuppyModeration(commands.Cog, name="PuppyModeration"):
    """🐶 PupPet moderation system — full feature edition."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        init_db()

        # spam tracking: {guild_id: {user_id: [(channel_id, timestamp, content)]}}
        self._msg_timestamps: dict[int, dict[int, list[tuple[int, float, str]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        # channel mute expiries: {(guild_id, user_id): {channel_id: expiry}}
        self._channel_mutes: dict[tuple[int, int], dict[int, datetime]] = defaultdict(dict)
        # ghost ping tracking: {message_id: (author_id, channel_id, [mentioned_ids])}
        self._pending_pings: dict[int, tuple[int, int, list[int]]] = {}
        # mod action cooldowns: {(mod_id, target_id, action): expiry datetime}
        self._mod_cooldowns: dict[tuple[int, int, str], datetime] = {}

        self._cleanup_mutes.start()
        self._check_timeout_restores.start()

    def cog_unload(self) -> None:
        self._cleanup_mutes.cancel()
        self._check_timeout_restores.cancel()

    # ── Log helper ────────────────────────────────────────────────────────────

    async def send_log(self, embed: discord.Embed) -> None:
        try:
            channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if channel and isinstance(channel, discord.TextChannel):
                await channel.send(embed=embed)
        except Exception as e:
            logger.error("Failed to send log: %s", e)

    # ── Backed Puppy helper ──────────────────────────────────────────────────

    async def apply_backed_puppy(self, guild: discord.Guild, member: discord.Member) -> None:
        try:
            role = guild.get_role(BACKED_PUPPY_ROLE_ID)
            if role and role not in member.roles:
                await member.add_roles(role, reason="Auto Backed Puppy")
        except Exception as e:
            logger.error("Failed to add Backed Puppy role: %s", e)

    async def remove_backed_puppy(self, guild: discord.Guild, member: discord.Member) -> None:
        try:
            role = guild.get_role(BACKED_PUPPY_ROLE_ID)
            if role and role in member.roles:
                await member.remove_roles(role, reason="Punishment reversed — Backed Puppy removed")
        except Exception as e:
            logger.error("Failed to remove Backed Puppy role: %s", e)

    # ── Role restore after timeout ─────────────────────────────────────────────

    async def restore_roles_after_timeout(
        self, member: discord.Member, guild: discord.Guild
    ) -> int:
        """Restore saved roles to a member. Returns number of roles restored."""
        role_ids = pop_timeout_roles(member.id, guild.id)
        if not role_ids:
            return 0
        roles_to_add: list[discord.Role] = []
        for rid in role_ids:
            role = guild.get_role(rid)
            if role and role not in member.roles:
                roles_to_add.append(role)
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="Role restore after timeout expired")
            except Exception as e:
                logger.error("Role restore error for %s: %s", member.id, e)
        return len(roles_to_add)

    # ── Background: check for expired timeouts and restore roles ──────────────

    @tasks.loop(seconds=30)
    async def _check_timeout_restores(self) -> None:
        try:
            rows = await asyncio.to_thread(
                lambda: get_connection().execute("SELECT DISTINCT user_id, guild_id FROM timeout_roles").fetchall()
            )
            for row in rows:
                guild = self.bot.get_guild(row["guild_id"])
                if not guild:
                    continue
                member = guild.get_member(row["user_id"])
                if not member:
                    continue
                timed_out = (
                    member.timed_out_until
                    and member.timed_out_until > datetime.now(timezone.utc)
                )
                if not timed_out:
                    restored = await self.restore_roles_after_timeout(member, guild)
                    await self.remove_backed_puppy(guild, member)
                    if restored > 0:
                        embed = base_embed(
                            "🐶 Roles Restored After Timeout",
                            f"{user_display(member)} — **{restored}** role(s) restored automatically.\n**Backed Puppy role removed.**",
                            color=0x44FF99,
                        )
                        await self.send_log(embed)
        except Exception as e:
            logger.error("Timeout restore check error: %s", e)

    @_check_timeout_restores.before_loop
    async def _before_restore(self) -> None:
        await self.bot.wait_until_ready()

    # ── Background: mute cleanup ──────────────────────────────────────────────

    @tasks.loop(seconds=10)
    async def _cleanup_mutes(self) -> None:
        now = datetime.now(timezone.utc)
        for key in list(self._channel_mutes.keys()):
            expired = [ch for ch, exp in self._channel_mutes[key].items() if exp <= now]
            for ch in expired:
                del self._channel_mutes[key][ch]
            if not self._channel_mutes[key]:
                del self._channel_mutes[key]

    @_cleanup_mutes.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ── Emoji counter ─────────────────────────────────────────────────────────

    @staticmethod
    def count_emojis(text: str) -> int:
        count = 0
        for char in text:
            cp = ord(char)
            cat = unicodedata.category(char)
            if cat in ("So", "Sm") or 0x1F000 <= cp <= 0x1FFFF or 0x2600 <= cp <= 0x27BF:
                count += 1
        count += len(re.findall(r"<a?:[^:]+:\d+>", text))
        return count

    # ── Channel-specific mute ─────────────────────────────────────────────────

    async def apply_channel_mute(
        self,
        member: discord.Member,
        channel: discord.TextChannel,
        seconds: int,
        reason: str,
    ) -> None:
        try:
            overwrite = channel.overwrites_for(member)
            overwrite.send_messages = False
            await channel.set_permissions(member, overwrite=overwrite, reason=reason)
            key    = (member.guild.id, member.id)
            expiry = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            self._channel_mutes[key][channel.id] = expiry

            spam_total = await asyncio.to_thread(increment_spam_counter, member.id, member.guild.id)
            await self.apply_backed_puppy(member.guild, member)

            async def _unmute():
                await asyncio.sleep(seconds)
                try:
                    ow = channel.overwrites_for(member)
                    ow.send_messages = None
                    await channel.set_permissions(member, overwrite=ow, reason="Spam mute expired")
                    self._channel_mutes[key].pop(channel.id, None)
                    # If no more active channel mutes for this user, remove Backed Puppy role
                    if not self._channel_mutes.get(key):
                        await self.remove_backed_puppy(member.guild, member)
                except Exception:
                    pass
            asyncio.ensure_future(_unmute())

            embed = base_embed(
                "🐶 Spam Mute Applied",
                f"{user_display(member)} muted in {channel.mention} for **{seconds}s**.\n"
                f"**Reason:** {reason}\n**Total spam triggers:** {spam_total}",
                color=0xFF8800,
            )
            await self.send_log(embed)

            muted_channels = len(self._channel_mutes.get(key, {}))
            if muted_channels >= MULTI_CHANNEL_LIMIT:
                until = datetime.now(timezone.utc) + MULTI_CHANNEL_TIMEOUT
                await member.timeout(until, reason="Multi-channel spam detected.")
                await self.apply_backed_puppy(member.guild, member)
                case_id = await asyncio.to_thread(add_case, 
                    member.id,
                    self.bot.user.id if self.bot.user else None,
                    "Auto Timeout (Multi-Spam)",
                    "Multi-channel spam detected.",
                )
                embed2 = base_embed(
                    "🐶 Multi-Channel Spam Timeout",
                    f"{user_display(member)} frozen for **5 minutes** (multi-channel spam).\n**Case:** #{case_id}",
                    color=0xFF0000,
                )
                await self.send_log(embed2)
        except Exception as e:
            logger.error("Channel mute error: %s", e)

    # ── Alt detection on join ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        flags: list[str] = []
        now = datetime.now(timezone.utc)
        account_age = (now - member.created_at).days

        if account_age < ALT_ACCOUNT_AGE_DAYS:
            flags.append(f"New account ({account_age}d old)")

        # Default avatar check
        if member.avatar is None:
            flags.append("Default avatar (no custom avatar set)")

        # Check for matching avatar hash with existing members
        if member.avatar:
            av_hash = member.avatar.key
            similar = [
                m for m in member.guild.members
                if m.id != member.id and m.avatar and m.avatar.key == av_hash
            ]
            if similar:
                flags.append(
                    f"Matching avatar with: {', '.join(str(m) for m in similar[:3])}"
                )

        if flags:
            await asyncio.to_thread(flag_alt, member.id, member.guild.id, " | ".join(flags))
            embed = base_embed(
                "🐶 Potential Alt Account Detected",
                f"**User:** {user_display(member)}\n"
                f"**Account Age:** {account_age} days\n"
                f"**Flags:**\n" + "\n".join(f"• {f}" for f in flags),
                color=0xFFAA00,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            await self.send_log(embed)

    # ── Ticket system ─────────────────────────────────────────────────────────

    async def open_ticket(
        self, interaction: discord.Interaction, target: discord.Member
    ) -> None:
        guild = interaction.guild
        try:
            overwrites = {
                guild.default_role:                 discord.PermissionOverwrite(view_channel=False),
                target:                             discord.PermissionOverwrite(view_channel=True, send_messages=True),
                interaction.user:                   discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            admin_role = guild.get_role(ADMIN_ROLE_ID)
            second_admin_role = guild.get_role(SECOND_ADMIN_ROLE_ID)
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            if second_admin_role:
                overwrites[second_admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

            channel = await guild.create_text_channel(
                name=f"ticket-{target.name}",
                overwrites=overwrites,
                reason=f"Mod ticket opened by {interaction.user}",
                topic=f"Ticket for {target} ({target.id}) | Opened by {interaction.user}",
            )
            # ── Persist ticket data ──────────────────────────────────────
            now = datetime.now(timezone.utc)

            # Calculate next ticket number to maintain consistency with cogs/tickets.py
            all_tickets = await self.bot.data.get_tickets()
            existing_numbers = [t.get("number", 0) for t in all_tickets.values()]
            ticket_number = (max(existing_numbers) + 1) if existing_numbers else 1

            ticket_data = {
                "channel_id": channel.id,
                "guild_id":   guild.id,
                "owner_id":   target.id,
                "category":   "moderation",
                "number":     ticket_number,
                "priority":   "none",
                "claimed_by": None,
                "status":     "open",
                "created_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "created_ts": now.timestamp(),
                "last_message_ts": now.timestamp(),
                "first_response_ts": None,
                "extra_users": [],
                "timer_message_id": None,
            }
            await self.bot.data.save_ticket(channel.id, ticket_data)


            embed = base_embed(
                "🎫 Mod Ticket Opened",
                f"**User:** {user_display(target)}\n"
                f"**Opened by:** {interaction.user.mention}\n"
                f"**Channel:** {channel.mention}",
            )
            await channel.send(
                content=f"{target.mention} {interaction.user.mention}",
                embed=embed,
            )
            await interaction.response.send_message(
                f"🎫 Ticket opened: {channel.mention}", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message("🐶 Missing permissions to create ticket channel.", ephemeral=True)
        except Exception as e:
            logger.error("Ticket open error: %s", e)
            await interaction.response.send_message("🐶 Failed to open ticket.", ephemeral=True)

    # ── /mod ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="mod", description="🐶 Open the Puppy Justice Panel for a member.")
    @app_commands.describe(user="The member to moderate.")
    @admin_check()
    async def mod_command(self, interaction: discord.Interaction, user: discord.Member) -> None:
        embed = base_embed(
            "🐶 Puppy Justice Panel",
            f"**Target:** {user_display(user)}\n"
            f"**Account Created:** {discord.utils.format_dt(user.created_at, 'R')}\n"
            f"**Joined Server:** {discord.utils.format_dt(user.joined_at, 'R') if user.joined_at else 'Unknown'}\n"
            f"**Roles:** {len(user.roles) - 1}\n"
            f"**Timeout:** {'Yes' if user.timed_out_until and user.timed_out_until > datetime.now(timezone.utc) else 'No'}",
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(
            embed=embed, view=ModPanelView(self, user), ephemeral=True
        )

    # ── /cases ────────────────────────────────────────────────────────────────

    @app_commands.command(name="cases", description="🐶 View the full case history of a user.")
    @app_commands.describe(user="The member to look up.")
    @admin_check()
    async def cases_command(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        rows = await asyncio.to_thread(get_history, user.id, 25)
        if not rows:
            embed = base_embed("🐶 Case History", f"No cases found for {user.mention}.")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        pages: list[str] = []
        for r in rows:
            ts = r["timestamp"][:16].replace("T", " ")
            pages.append(
                f"**#{r['id']}** `{r['action']}`\n"
                f"  Mod: <@{r['moderator_id']}> | {ts}\n"
                f"  Reason: {r['reason'] or 'None'}"
            )

        # Split into chunks of 8 per embed
        chunks = [pages[i:i+8] for i in range(0, len(pages), 8)]
        embed = base_embed(
            f"🐶 Case History — {user} ({len(rows)} cases)",
            "\n\n".join(chunks[0]),
        )
        embed.set_footer(text=f"🐶 PupPet Moderation | Page 1/{len(chunks)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        for i, chunk in enumerate(chunks[1:], 2):
            e = base_embed("", "\n\n".join(chunk))
            e.set_footer(text=f"🐶 PupPet Moderation | Page {i}/{len(chunks)}")
            await interaction.followup.send(embed=e, ephemeral=True)

    # ── /clearcases ───────────────────────────────────────────────────────────

    @app_commands.command(name="clearcases", description="🐶 Clear all moderation cases for a user.")
    @app_commands.describe(user="The member whose history to clear.")
    @admin_check()
    async def clearcases_command(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        async def do_clear(conf_inter: discord.Interaction):
            try:
                with get_connection() as con:
                    con.execute("DELETE FROM cases WHERE user_id=?", (user.id,))
                    con.commit()
                add_staff_audit(
                    conf_inter.user, "Clear Cases", user,
                    "All cases deleted", conf_inter.guild.id
                )
                embed = base_embed(
                    "🐶 Cases Cleared",
                    f"All moderation cases for {user_display(user)} have been deleted.",
                    color=0x44FF99,
                )
                await conf_inter.response.edit_message(content=None, embed=embed, view=None)
                await self.send_log(embed)
            except Exception as e:
                logger.error("Clear cases error: %s", e)
                await conf_inter.response.edit_message(content="🐶 Failed to clear cases.", embed=None, view=None)

        confirm_embed = base_embed(
            "⚠️ Confirm Clear Cases",
            f"This will **permanently delete** all moderation cases for {user_display(user)}.\nAre you sure?",
            color=0xFF4444,
        )
        await interaction.response.send_message(
            embed=confirm_embed, view=ConfirmView("Clear Cases", do_clear), ephemeral=True
        )

    # ── /puppycheck ──────────────────────────────────────────────────────────

    @app_commands.command(name="puppycheck", description="🐶 Risk score analysis for a user.")
    @app_commands.describe(user="The member to analyse.")
    @admin_check()
    async def puppycheck_command(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        now        = datetime.now(timezone.utc)
        cases      = await asyncio.to_thread(get_history, user.id, 100)
        alt_flags  = await asyncio.to_thread(get_alt_flags, user.id, interaction.guild.id)
        spam_count = await asyncio.to_thread(get_spam_count, user.id, interaction.guild.id)
        acct_age   = (now - user.created_at).days
        join_age   = (now - user.joined_at).days if user.joined_at else 999

        score = 0
        breakdown: list[str] = []

        # Account age
        if acct_age < 7:
            score += 30
            breakdown.append(f"🔴 Account only **{acct_age}d** old (+30)")
        elif acct_age < 30:
            score += 15
            breakdown.append(f"🟠 Account only **{acct_age}d** old (+15)")
        else:
            breakdown.append(f"🟢 Account age **{acct_age}d** (no risk)")

        # Server join age
        if join_age < 3:
            score += 20
            breakdown.append(f"🔴 Joined **{join_age}d** ago (+20)")
        elif join_age < 7:
            score += 10
            breakdown.append(f"🟠 Joined **{join_age}d** ago (+10)")
        else:
            breakdown.append(f"🟢 Joined **{join_age}d** ago (no risk)")

        # Default avatar
        if user.avatar is None:
            score += 10
            breakdown.append("🟠 Default avatar (+10)")

        # Cases
        bans   = sum(1 for r in cases if "Ban" in r["action"])
        kicks  = sum(1 for r in cases if "Kick" in r["action"])
        tos    = sum(1 for r in cases if "Timeout" in r["action"])
        warns  = sum(1 for r in cases if "Warn" in r["action"])

        if bans:
            score += bans * 20
            breakdown.append(f"🔴 **{bans}** ban(s) (+{bans*20})")
        if kicks:
            score += kicks * 10
            breakdown.append(f"🟠 **{kicks}** kick(s) (+{kicks*10})")
        if tos:
            score += tos * 5
            breakdown.append(f"🟡 **{tos}** timeout(s) (+{tos*5})")
        if warns:
            score += warns * 3
            breakdown.append(f"🟡 **{warns}** warning(s) (+{warns*3})")

        # Spam
        if spam_count > 0:
            pts = min(spam_count * 5, 30)
            score += pts
            breakdown.append(f"🟠 **{spam_count}** spam trigger(s) (+{pts})")

        # Alt flags
        if alt_flags:
            pts = min(len(alt_flags) * 15, 30)
            score += pts
            breakdown.append(f"🔴 **{len(alt_flags)}** alt flag(s) (+{pts})")

        # Risk level
        if score >= 80:
            level, color = "🔴 EXTREME RISK", 0xFF0000
        elif score >= 50:
            level, color = "🟠 HIGH RISK", 0xFF8800
        elif score >= 25:
            level, color = "🟡 MODERATE RISK", 0xFFCC00
        else:
            level, color = "🟢 LOW RISK", 0x44FF44

        embed = base_embed(
            f"🐶 Puppy Risk Check — {user}",
            f"**Risk Score:** {score}/100+\n**Level:** {level}\n\n**Breakdown:**\n" + "\n".join(breakdown),
            color=color,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(
            name="Summary",
            value=f"Cases: {len(cases)} | Bans: {bans} | Kicks: {kicks} | Timeouts: {tos} | Spam: {spam_count} | Alt Flags: {len(alt_flags)}",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /exporthistory ────────────────────────────────────────────────────────

    @app_commands.command(
        name="exporthistory",
        description="🐶 Export a user's moderation history as a .txt or .html file.",
    )
    @app_commands.describe(
        user="The member to export.",
        format="Export format: txt or html",
    )
    @app_commands.choices(
        format=[
            app_commands.Choice(name="Text (.txt)", value="txt"),
            app_commands.Choice(name="HTML transcript (.html)", value="html"),
        ]
    )
    @admin_check()
    async def exporthistory_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        format: str = "txt",
    ) -> None:
        rows = await asyncio.to_thread(get_history, user.id, 500)
        if not rows:
            await interaction.response.send_message(
                f"🐶 No cases found for {user.mention}.", ephemeral=True
            )
            return

        if format == "txt":
            lines = [
                f"PupPet Moderation History Export",
                f"User: {user} ({user.id})",
                f"Exported: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                f"Total cases: {len(rows)}",
                "=" * 60,
                "",
            ]
            for r in rows:
                lines.append(f"Case #{r['id']}")
                lines.append(f"  Action    : {r['action']}")
                lines.append(f"  Moderator : {r['moderator_id']}")
                lines.append(f"  Reason    : {r['reason'] or 'None'}")
                lines.append(f"  Timestamp : {r['timestamp']}")
                lines.append("")
            content_bytes = "\n".join(lines).encode("utf-8")
            filename = f"history_{user.id}.txt"
            mime = "text/plain"

        else:  # html
            rows_html = ""
            for r in rows:
                rows_html += (
                    f"<tr>"
                    f"<td>#{r['id']}</td>"
                    f"<td>{html.escape(r['action'])}</td>"
                    f"<td>{r['moderator_id']}</td>"
                    f"<td>{html.escape(r['reason'] or '')}</td>"
                    f"<td>{r['timestamp'][:16]}</td>"
                    f"</tr>"
                )
            doc = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8">
<title>PupPet Mod History — {html.escape(str(user))}</title>
<style>
  body{{font-family:monospace;background:#1e1e2e;color:#cdd6f4;padding:2rem}}
  h1{{color:#f5c2e7}} table{{border-collapse:collapse;width:100%}}
  th{{background:#313244;padding:.5rem 1rem;text-align:left}}
  td{{padding:.4rem 1rem;border-bottom:1px solid #45475a}}
  tr:hover td{{background:#313244}}
</style></head>
<body>
<h1>🐶 PupPet Moderation History</h1>
<p><b>User:</b> {html.escape(str(user))} ({user.id})<br>
<b>Exported:</b> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}<br>
<b>Total cases:</b> {len(rows)}</p>
<table>
<tr><th>Case</th><th>Action</th><th>Moderator ID</th><th>Reason</th><th>Timestamp</th></tr>
{rows_html}
</table></body></html>"""
            content_bytes = doc.encode("utf-8")
            filename = f"history_{user.id}.html"
            mime = "text/html"

        file = discord.File(io.BytesIO(content_bytes), filename=filename)
        add_staff_audit(
            interaction.user, f"Export History ({format})", user, None, interaction.guild.id
        )
        await interaction.response.send_message(
            f"🐶 Export for {user.mention} ({len(rows)} cases):", file=file, ephemeral=True
        )

    # ── /staffaudit ───────────────────────────────────────────────────────────

    @app_commands.command(name="staffaudit", description="🐶 View recent staff audit log entries.")
    @app_commands.describe(limit="How many entries to show (default 20, max 50)")
    @admin_check()
    async def staffaudit_command(
        self, interaction: discord.Interaction, limit: int = 20
    ) -> None:
        limit = max(1, min(limit, 50))
        with get_connection() as con:
            rows = con.execute(
                "SELECT * FROM staff_audit_log WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                (interaction.guild.id, limit),
            ).fetchall()
        if not rows:
            await interaction.response.send_message("🐶 No staff audit entries found.", ephemeral=True)
            return
        lines = []
        for r in rows:
            ts = r["timestamp"][:16].replace("T", " ")
            target_str = f"→ {r['target_tag']} ({r['target_id']})" if r["target_id"] else ""
            lines.append(
                f"**{r['staff_tag']}** `{r['action']}` {target_str} | {ts}\n"
                f"  Reason: {r['reason'] or 'None'}"
            )
        # Build description, respecting Discord's 4096-char embed limit
        description = "\n\n".join(lines)
        if len(description) > 4000:
            description = description[:4000] + "\n…*(truncated — export full log from DB)*"
        embed = base_embed(
            f"🐶 Staff Audit Log (last {len(rows)})",
            description,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── on_message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return

        admin   = is_admin(member)
        content = message.content

        # ── YouTube-only channel ──────────────────────────────────────────────
        if message.channel.id == YOUTUBE_CHANNEL_ID:
            if not admin and not YOUTUBE_PATTERN.search(content):
                try:
                    await message.delete()
                    until = datetime.now(timezone.utc) + YOUTUBE_TIMEOUT
                    await member.timeout(until, reason="Only YouTube videos are allowed in this channel.")
                    await self.apply_backed_puppy(message.guild, member)
                    case_id = await asyncio.to_thread(add_case, 
                        member.id,
                        self.bot.user.id if self.bot.user else None,
                        "Timeout (YouTube Violation)",
                        "Only YouTube videos are allowed in this channel.",
                    )
                    embed = base_embed(
                        "🐶 YouTube Channel Violation",
                        f"{user_display(member)} frozen for **3 minutes** (non-YouTube content in {message.channel.mention}).\n**Case:** #{case_id}",
                        color=0xFF0000,
                    )
                    await self.send_log(embed)
                except Exception as e:
                    logger.error("YouTube channel enforcement error: %s", e)
            return

        # ── Caps spam ─────────────────────────────────────────────────────────
        if len(content) > CAPS_MIN_LEN:
            alpha = [c for c in content if c.isalpha()]
            if alpha and sum(1 for c in alpha if c.isupper()) / len(alpha) >= CAPS_RATIO:
                try:
                    await message.delete()
                except Exception:
                    pass
                return

        # ── Emoji spam ────────────────────────────────────────────────────────
        if self.count_emojis(content) >= MAX_EMOJI_COUNT:
            try:
                await message.delete()
            except Exception:
                pass
            return

        # ── Mass mention ──────────────────────────────────────────────────────
        if len(message.mentions) > MAX_MENTIONS:
            try:
                await message.delete()
                embed = base_embed(
                    "🐶 Mass Mention Blocked",
                    f"{user_display(member)} tried to mention **{len(message.mentions)}** users in {message.channel.mention}.",
                    color=0xFF4444,
                )
                await self.send_log(embed)
            except Exception as e:
                logger.error("Mass mention error: %s", e)
            return

        # ── Anti-everyone ─────────────────────────────────────────────────────
        if not admin and ("@everyone" in content or "@here" in content):
            try:
                await message.delete()
                embed = base_embed(
                    "🐶 @everyone/@here Abuse",
                    f"{user_display(member)} tried to use @everyone/@here in {message.channel.mention}.",
                    color=0xFF4444,
                )
                await self.send_log(embed)
            except Exception as e:
                logger.error("Anti-everyone error: %s", e)
            return

        # ── Bad word filter ───────────────────────────────────────────────────
        lower_content = content.lower()
        if any(bw in lower_content for bw in BAD_WORDS):
            try:
                await message.delete()
            except Exception:
                pass
            return

        # ── Invite blocker ────────────────────────────────────────────────────
        if not admin and INVITE_PATTERN.search(content):
            try:
                await message.delete()
                embed = base_embed(
                    "🐶 Invite Blocked",
                    f"{user_display(member)} sent a Discord invite in {message.channel.mention}.\n**Content:** ||{content[:200]}||",
                    color=0xFF8800,
                )
                await self.send_log(embed)
            except Exception as e:
                logger.error("Invite blocker error: %s", e)
            return

        # ── Anti-link ─────────────────────────────────────────────────────────
        if not admin and URL_PATTERN.search(content):
            try:
                await message.delete()
                embed = base_embed(
                    "🐶 Link Blocked",
                    f"**User:** {user_display(member)}\n"
                    f"**Channel:** {message.channel.mention}\n"
                    f"**Content:** ||{content[:200]}||\n"
                    f"**Link Detected:** Yes",
                    color=0xFF8800,
                )
                await self.send_log(embed)
            except Exception as e:
                logger.error("Anti-link error: %s", e)
            return

        # ── Spam detection ────────────────────────────────────────────────────
        now_ts   = message.created_at.timestamp()
        guild_id = message.guild.id
        timestamps = self._msg_timestamps[guild_id][member.id]
        timestamps.append((message.channel.id, now_ts, content))
        timestamps[:] = [(c, t, txt) for c, t, txt in timestamps if now_ts - t <= 60]

        recent = [(c, t, txt) for c, t, txt in timestamps if now_ts - t <= SPAM_SECONDS]

        if len(recent) >= SPAM_MSG_LIMIT:
            try:
                history_items = [
                    m async for m in message.channel.history(
                        limit=20, after=datetime.now(timezone.utc) - timedelta(seconds=SPAM_SECONDS)
                    )
                ]
                for sm in [m for m in history_items if m.author.id == member.id][:10]:
                    try:
                        await sm.delete()
                    except Exception:
                        pass
            except Exception:
                pass
            await self.apply_channel_mute(member, message.channel, SPAM_MUTE_SECONDS, "Spam detected.")
            self._msg_timestamps[guild_id][member.id] = []
            return

        # Duplicate message spam
        recent_texts = [txt for _, _, txt in recent]
        if len(recent_texts) >= SPAM_MSG_LIMIT and len(set(recent_texts)) == 1:
            await self.apply_channel_mute(member, message.channel, SPAM_MUTE_SECONDS, "Duplicate message spam.")
            self._msg_timestamps[guild_id][member.id] = []
            return

        # ── Ghost ping tracking ───────────────────────────────────────────────
        if message.mentions:
            self._pending_pings[message.id] = (
                member.id,
                message.channel.id,
                [u.id for u in message.mentions],
            )

    # ── /modhelp ──────────────────────────────────────────────────────────────

    @app_commands.command(name="modhelp", description="🐶 PupPet moderation system — all commands and auto-mod info.")
    @admin_check()
    async def modhelp_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=ModHelpView.build_page(0),
            view=ModHelpView(),
            ephemeral=True,
        )

    # ── Ghost ping ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.id in self._pending_pings:
            author_id, channel_id, mentioned_ids = self._pending_pings.pop(message.id)
            mentioned_str = ", ".join(f"<@{uid}>" for uid in mentioned_ids)
            embed = base_embed(
                "🐶 Ghost Ping Detected",
                f"**Pinger:** <@{author_id}> (`{author_id}`)\n"
                f"**Channel:** <#{channel_id}>\n"
                f"**Mentioned:** {mentioned_str}\n"
                f"**Timestamp:** {discord.utils.format_dt(message.created_at, 'F')}",
                color=0xAA44FF,
            )
            await self.send_log(embed)
            try:
                with get_connection() as con:
                    con.execute(
                        "INSERT INTO ghost_ping_log (user_id, channel_id, mentioned, timestamp) VALUES (?,?,?,?)",
                        (author_id, channel_id, mentioned_str, datetime.now(timezone.utc).isoformat()),
                    )
                    con.commit()
            except Exception as e:
                logger.error("Ghost ping DB error: %s", e)


# ─── Setup ────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PuppyModeration(bot))
    logger.info("🐶 PuppyModeration cog loaded (full feature edition).")
