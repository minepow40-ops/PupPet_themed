"""
cogs/badwords.py
Production-ready Bad Words filter — ticket-panel style public embed.
/badpanel  → sends a persistent panel embed into the channel (like a ticket panel)
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
import sqlite3
from pathlib import Path

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

from utils.migrations import Migration, run_migrations

log = logging.getLogger("BadWords")

DB_PATH = Path(__file__).parent.parent / "data" / "badwords.db"

ALLOWED_ROLE_IDS: set[int] = {1551515669026177077, 1551515670553042995, 1551515673174212638, 1551515673702703147}

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_ZERO_WIDTH = re.compile(
    r"[​‌‍⁠﻿­͏ᅟᅠ"
    r"឴឵᠎ -‏    "
    r"　︀-️]"
)
_PUNCT_SYMBOLS = re.compile(r"[^\w]", re.UNICODE)
_REPEATED = re.compile(r"(.)\1+")


def normalize(text: str) -> str:
    text = text.lower()
    text = _ZERO_WIDTH.sub("", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace(" ", "")
    text = _PUNCT_SYMBOLS.sub("", text)
    text = text.replace("_", "")
    text = _REPEATED.sub(r"\1", text)
    return text


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

MIGRATIONS = [
    Migration(1, "initial schema — badwords, badword_settings", """
        CREATE TABLE IF NOT EXISTS badwords (
            guild_id INTEGER NOT NULL,
            word     TEXT    NOT NULL,
            UNIQUE(guild_id, word)
        );
        CREATE TABLE IF NOT EXISTS badword_settings (
            guild_id       INTEGER PRIMARY KEY,
            enabled        INTEGER DEFAULT 1,
            deleted_count  INTEGER DEFAULT 0
        );
    """),
]


async def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Migrations run synchronously — this only happens once at cog load,
    # so blocking briefly here is fine and keeps version-tracking logic
    # in one place instead of duplicating it for aiosqlite.
    con = sqlite3.connect(DB_PATH)
    try:
        run_migrations(con, MIGRATIONS, db_name="badwords.db")
    finally:
        con.close()
    # aiosqlite continues to be used for all regular query code below —
    # nothing else in this file needs to change.


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

def _embed_success(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=0x2ECC71)

def _embed_error(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=0xE74C3C)

def _embed_info(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=0x5865F2)


def _build_panel_embed(stats: dict, guild_name: str) -> discord.Embed:
    """The public-facing panel embed — shown in the channel like a ticket panel."""
    status_str = "Enabled ✅" if stats["enabled"] else "Disabled ❌"
    embed = discord.Embed(
        title="🐶 Badword Filter Panel",
        description="Use the buttons below to manage the blacklist for this server.",
        color=0x5865F2,
    )
    embed.add_field(name="Status", value=status_str, inline=True)
    embed.add_field(name="Blacklisted Words", value=str(stats["total_words"]), inline=True)
    embed.add_field(name="Messages Deleted", value=str(stats["deleted_count"]), inline=True)
    embed.set_footer(text=guild_name)
    return embed


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class AddWordModal(discord.ui.Modal, title="Add Word to Blacklist"):
    word = discord.ui.TextInput(
        label="Word",
        placeholder="Enter the word to blacklist…",
        min_length=1,
        max_length=100,
    )

    def __init__(self, cog: "BadWords") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        word = self.word.value.strip()
        guild_id: int = interaction.guild_id  # type: ignore[assignment]

        inserted = await self.cog._db_add_word(guild_id, word)
        if not inserted:
            is_dup = await self.cog._db_word_exists(guild_id, word)
            msg = f"`{word}` is already blacklisted." if is_dup else "Could not add the word. Try again."
            await interaction.response.send_message(
                embed=_embed_error("Failed", msg), ephemeral=True
            )
            return

        await self.cog._reload_guild_cache(guild_id)

        # Refresh the panel embed stats
        await self.cog._refresh_panel(interaction.guild)

        await interaction.response.send_message(
            embed=_embed_success("Word Added", f"`{word}` has been added to the blacklist."),
            ephemeral=True,
        )
        log.info("[BadWords] '%s' added in guild %s by %s", word, guild_id, interaction.user.id)


class RemoveWordModal(discord.ui.Modal, title="Remove Word from Blacklist"):
    word = discord.ui.TextInput(
        label="Word",
        placeholder="Enter the word to remove…",
        min_length=1,
        max_length=100,
    )

    def __init__(self, cog: "BadWords") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        word = self.word.value.strip()
        guild_id: int = interaction.guild_id  # type: ignore[assignment]

        removed = await self.cog._db_remove_word(guild_id, word)
        if not removed:
            await interaction.response.send_message(
                embed=_embed_error("Not Found", f"`{word}` is not in the blacklist."),
                ephemeral=True,
            )
            return

        await self.cog._reload_guild_cache(guild_id)
        await self.cog._refresh_panel(interaction.guild)

        await interaction.response.send_message(
            embed=_embed_success("Word Removed", f"`{word}` has been removed from the blacklist."),
            ephemeral=True,
        )
        log.info("[BadWords] '%s' removed in guild %s by %s", word, guild_id, interaction.user.id)


# ---------------------------------------------------------------------------
# Confirm clear
# ---------------------------------------------------------------------------

class ConfirmClear(discord.ui.View):
    def __init__(self, cog: "BadWords", guild_id: int) -> None:
        super().__init__(timeout=30)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Confirm Clear", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog._db_clear_words(self.guild_id)
        self.cog.badword_cache[self.guild_id] = set()
        self.stop()
        await self.cog._refresh_panel(interaction.guild)
        await interaction.response.edit_message(
            embed=_embed_success("Blacklist Cleared", "All blacklisted words have been removed."),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=_embed_info("Cancelled", "No changes were made."),
            view=None,
        )


# ---------------------------------------------------------------------------
# Panel view  (persistent — timeout=None so it survives bot restarts)
# ---------------------------------------------------------------------------

class BadwordPanelView(discord.ui.View):
    """
    Persistent view attached to the public panel message.
    Buttons are visible to everyone; permission is checked inside each callback.
    """

    def __init__(self, cog: "BadWords") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    def _check(self, interaction: discord.Interaction) -> bool:
        return self.cog._has_permission(interaction)

    def _deny(self) -> discord.Embed:
        return _embed_error("Permission Denied", "You don't have the required role to do this.")

    # Row 0 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Add Word",
        style=discord.ButtonStyle.success,
        emoji="➕",
        row=0,
        custom_id="bw:add",
    )
    async def add_word(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._check(interaction):
            await interaction.response.send_message(embed=self._deny(), ephemeral=True)
            return
        await interaction.response.send_modal(AddWordModal(self.cog))

    @discord.ui.button(
        label="Remove Word",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        row=0,
        custom_id="bw:remove",
    )
    async def remove_word(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._check(interaction):
            await interaction.response.send_message(embed=self._deny(), ephemeral=True)
            return
        await interaction.response.send_modal(RemoveWordModal(self.cog))

    @discord.ui.button(
        label="Toggle Filter",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        row=0,
        custom_id="bw:toggle",
    )
    async def toggle_filter(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._check(interaction):
            await interaction.response.send_message(embed=self._deny(), ephemeral=True)
            return
        guild_id: int = interaction.guild_id  # type: ignore[assignment]
        new_state = not self.cog.enabled_cache.get(guild_id, True)
        self.cog.enabled_cache[guild_id] = new_state
        await self.cog._db_set_enabled(guild_id, new_state)
        await self.cog._refresh_panel(interaction.guild)
        status = "**enabled** ✅" if new_state else "**disabled** ❌"
        await interaction.response.send_message(
            embed=_embed_success("Filter Toggled", f"The bad-word filter is now {status}."),
            ephemeral=True,
        )

    # Row 1 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="Clear All",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
        row=1,
        custom_id="bw:clear",
    )
    async def clear_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._check(interaction):
            await interaction.response.send_message(embed=self._deny(), ephemeral=True)
            return
        guild_id: int = interaction.guild_id  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=_embed_info(
                "Confirm Clear",
                "⚠️ This will **permanently** remove **all** blacklisted words.\n\nAre you sure?",
            ),
            view=ConfirmClear(self.cog, guild_id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Stats",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        row=1,
        custom_id="bw:stats",
    )
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._check(interaction):
            await interaction.response.send_message(embed=self._deny(), ephemeral=True)
            return
        guild_id: int = interaction.guild_id  # type: ignore[assignment]
        s = await self.cog._db_get_stats(guild_id)
        status_str = "Enabled ✅" if s["enabled"] else "Disabled ❌"
        embed = _embed_info(f"Bad-Word Filter Stats — {interaction.guild.name}")  # type: ignore[union-attr]
        embed.add_field(name="Status", value=status_str, inline=True)
        embed.add_field(name="Blacklisted Words", value=str(s["total_words"]), inline=True)
        embed.add_field(name="Messages Deleted", value=str(s["deleted_count"]), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class BadWords(commands.Cog, name="BadWords"):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.badword_cache: dict[int, set[str]] = {}
        self.enabled_cache: dict[int, bool] = {}
        # { guild_id: (channel_id, message_id) } — tracks the live panel message
        self.panel_message: dict[int, tuple[int, int]] = {}

    async def cog_load(self) -> None:
        await _init_db()
        await self._load_all_cache()
        # Re-register persistent view so buttons work after a bot restart
        self.bot.add_view(BadwordPanelView(self))
        log.info("[BadWords] Cog loaded.")

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    def _has_permission(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            return False
        return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)

    # ------------------------------------------------------------------
    # Panel refresh helper
    # ------------------------------------------------------------------

    async def _refresh_panel(self, guild: discord.Guild | None) -> None:
        """Edit the live panel message so stats stay up to date."""
        if guild is None:
            return
        entry = self.panel_message.get(guild.id)
        if not entry:
            return
        channel_id, message_id = entry
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            msg = await channel.fetch_message(message_id)
            stats = await self._db_get_stats(guild.id)
            await msg.edit(embed=_build_panel_embed(stats, guild.name))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("[BadWords] Could not refresh panel in guild %s: %s", guild.id, exc)

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    async def _load_all_cache(self) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("SELECT guild_id, word FROM badwords") as cur:
                    async for row in cur:
                        gid = int(row[0])
                        self.badword_cache.setdefault(gid, set()).add(normalize(row[1]))
                async with db.execute("SELECT guild_id, enabled FROM badword_settings") as cur:
                    async for row in cur:
                        self.enabled_cache[int(row[0])] = bool(row[1])
        except aiosqlite.DatabaseError as exc:
            log.exception("[BadWords] Cache load failed: %s", exc)

    async def _reload_guild_cache(self, guild_id: int) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT word FROM badwords WHERE guild_id = ?", (guild_id,)
                ) as cur:
                    rows = await cur.fetchall()
            self.badword_cache[guild_id] = {normalize(r[0]) for r in rows}
        except aiosqlite.DatabaseError as exc:
            log.exception("[BadWords] Cache reload failed for guild %s: %s", guild_id, exc)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _ensure_settings_row(self, db: aiosqlite.Connection, guild_id: int) -> None:
        await db.execute(
            "INSERT OR IGNORE INTO badword_settings (guild_id) VALUES (?)", (guild_id,)
        )

    async def _db_add_word(self, guild_id: int, word: str) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO badwords (guild_id, word) VALUES (?, ?)",
                    (guild_id, word.lower()),
                )
                inserted = cur.rowcount == 1
                await db.commit()
            return inserted
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_add_word")
            return False

    async def _db_word_exists(self, guild_id: int, word: str) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT 1 FROM badwords WHERE guild_id = ? AND word = ? LIMIT 1",
                    (guild_id, word.lower()),
                ) as cur:
                    return await cur.fetchone() is not None
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_word_exists")
            return False

    async def _db_remove_word(self, guild_id: int, word: str) -> bool:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "DELETE FROM badwords WHERE guild_id = ? AND word = ?",
                    (guild_id, word.lower()),
                )
                await db.commit()
            return cur.rowcount > 0
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_remove_word")
            return False

    async def _db_clear_words(self, guild_id: int) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM badwords WHERE guild_id = ?", (guild_id,))
                await db.commit()
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_clear_words")

    async def _db_set_enabled(self, guild_id: int, enabled: bool) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await self._ensure_settings_row(db, guild_id)
                await db.execute(
                    "UPDATE badword_settings SET enabled = ? WHERE guild_id = ?",
                    (int(enabled), guild_id),
                )
                await db.commit()
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_set_enabled")

    async def _db_get_stats(self, guild_id: int) -> dict:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await self._ensure_settings_row(db, guild_id)
                await db.commit()
                async with db.execute(
                    "SELECT enabled, deleted_count FROM badword_settings WHERE guild_id = ?",
                    (guild_id,),
                ) as cur:
                    row = await cur.fetchone()
                async with db.execute(
                    "SELECT COUNT(*) FROM badwords WHERE guild_id = ?", (guild_id,)
                ) as cur2:
                    count_row = await cur2.fetchone()
            return {
                "enabled": bool(row[0]) if row else True,
                "deleted_count": int(row[1]) if row else 0,
                "total_words": int(count_row[0]) if count_row else 0,
            }
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_get_stats")
            return {"enabled": True, "deleted_count": 0, "total_words": 0}

    async def _db_increment_deleted(self, guild_id: int) -> None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await self._ensure_settings_row(db, guild_id)
                await db.execute(
                    "UPDATE badword_settings SET deleted_count = deleted_count + 1 WHERE guild_id = ?",
                    (guild_id,),
                )
                await db.commit()
        except aiosqlite.DatabaseError:
            log.exception("[BadWords] DB ERROR _db_increment_deleted")

    # ------------------------------------------------------------------
    # Automod
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or not message.guild:
            return
        guild_id = message.guild.id
        if not self.enabled_cache.get(guild_id, True):
            return
        words = self.badword_cache.get(guild_id)
        if not words:
            return
        if not any(w in normalize(message.content) for w in words):
            return
        try:
            await message.delete()
            await self._db_increment_deleted(guild_id)
        except discord.NotFound:
            pass
        except discord.Forbidden:
            log.warning("[BadWords] No delete permission in guild %s", guild_id)
        except discord.HTTPException as exc:
            log.exception("[BadWords] HTTPException in guild %s: %s", guild_id, exc)

    # ------------------------------------------------------------------
    # /badpanel — sends a permanent public panel into the current channel
    # ------------------------------------------------------------------

    @app_commands.command(
        name="badpanel",
        description="Send the bad-word filter panel into this channel.",
    )
    async def badpanel(self, interaction: discord.Interaction) -> None:
        if not self._has_permission(interaction):
            await interaction.response.send_message(
                embed=_embed_error("Permission Denied", "You don't have the required role."),
                ephemeral=True,
            )
            return

        guild_id: int = interaction.guild_id  # type: ignore[assignment]
        stats = await self._db_get_stats(guild_id)

        # Acknowledge ephemerally first (avoids the "thinking…" timeout)
        await interaction.response.send_message(
            embed=_embed_success("Panel Sent", "The bad-word filter panel has been posted."),
            ephemeral=True,
        )

        # Send the actual public panel into the channel
        msg = await interaction.channel.send(  # type: ignore[union-attr]
            embed=_build_panel_embed(stats, interaction.guild.name),  # type: ignore[union-attr]
            view=BadwordPanelView(self),
        )

        # Remember this message so we can refresh its embed later
        self.panel_message[guild_id] = (msg.channel.id, msg.id)
        log.info(
            "[BadWords] Panel posted in guild %s channel %s message %s by %s",
            guild_id, msg.channel.id, msg.id, interaction.user.id,
        )


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BadWords(bot))
