"""
cogs/reaction_roles.py
Reaction Roles System — PupPet
─────────────────────────────────────────────────────────────────────────────
• Button roles     — click to assign/remove, toggle behaviour
• Select menu roles — single or multi-select dropdowns
• Classic reaction roles — emoji ↔ role mapping panels
• Verify panel     — Accept Rules → verify role
• Role groups      — mutual-exclusion groups (PC / Xbox / PS)
• Temporary roles  — time-limited role assignment
• Sticky roles     — restore self-assigned roles on rejoin
• Role requirements — gate roles behind required roles
• Role blacklists  — prevent assignment when blocked role is held
• Role limits      — per-panel and per-user maximums
• Full logging     — configurable log channel

Commands:
  /rolepanel  create | edit | delete | list | info | clone
  /buttonrole add | edit | remove
  /selectrole add | edit | remove
  /reactionrole add | remove | list
  /verify     create | role
  /rolegroup  create | addrole | remove
  /temprole   add | remove
  /stickyroles enable | disable
  /rolelog    set | disable

DB: data/reaction_roles.db
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.migrations import Migration, run_migrations

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("PupPet.reaction_roles")

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH = Path("data/reaction_roles.db")

# ── Colours ───────────────────────────────────────────────────────────────────
COL_BRAND  = 0xFFA500   # PupPet orange
COL_GREEN  = 0x57F287
COL_RED    = 0xED4245
COL_BLUE   = 0x5865F2
COL_GREY   = 0x607D8B
COL_YELLOW = 0xFEE75C

FOOTER = "PupPet • Reaction Roles"


# ─────────────────────────────────────────────────────────────────────────────
# Database bootstrap
# ─────────────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS role_panels (
    panel_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    panel_type  TEXT    NOT NULL DEFAULT 'button',
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS role_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id            INTEGER NOT NULL REFERENCES role_panels(panel_id) ON DELETE CASCADE,
    role_id             INTEGER NOT NULL,
    emoji               TEXT,
    label               TEXT    NOT NULL,
    description         TEXT    NOT NULL DEFAULT '',
    style               INTEGER NOT NULL DEFAULT 1,
    group_name          TEXT,
    required_role_id    INTEGER,
    blacklisted_role_id INTEGER,
    temp_duration       INTEGER,
    position            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sticky_roles (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, role_id)
);

CREATE TABLE IF NOT EXISTS temp_roles (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    role_id    INTEGER NOT NULL,
    expires_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id, role_id)
);

CREATE TABLE IF NOT EXISTS role_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    role_id   INTEGER NOT NULL,
    action    TEXT    NOT NULL,
    timestamp TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS role_settings (
    guild_id       INTEGER PRIMARY KEY,
    log_channel_id INTEGER,
    sticky_enabled INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_role_items_panel ON role_items(panel_id);
CREATE INDEX IF NOT EXISTS idx_sticky_guild     ON sticky_roles(guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_temp_expires     ON temp_roles(expires_at);
CREATE INDEX IF NOT EXISTS idx_logs_guild       ON role_logs(guild_id);
"""


MIGRATIONS = [
    Migration(1, "initial schema — panels, items, sticky/temp roles, logs, settings", _DDL),
]


async def _init_db() -> None:
    os.makedirs("data", exist_ok=True)
    # Migrations run synchronously — this only happens once at cog load,
    # so blocking briefly here is fine and keeps version-tracking logic
    # in one place instead of duplicating it for aiosqlite.
    con = sqlite3.connect(DB_PATH)
    try:
        run_migrations(con, MIGRATIONS, db_name="reaction_roles.db")
    finally:
        con.close()
    log.info("reaction_roles DB ready at %s", DB_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

async def get_panel(panel_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM role_panels WHERE panel_id = ?", (panel_id,)
        ) as cur:
            return await cur.fetchone()


async def get_panel_items(panel_id: int) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM role_items WHERE panel_id = ? ORDER BY position",
            (panel_id,),
        ) as cur:
            return await cur.fetchall()


async def get_role_item(item_id: int) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM role_items WHERE id = ?", (item_id,)
        ) as cur:
            return await cur.fetchone()


async def get_settings(guild_id: int) -> aiosqlite.Row:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO role_settings(guild_id) VALUES(?)", (guild_id,)
        )
        await db.commit()
        async with db.execute(
            "SELECT * FROM role_settings WHERE guild_id = ?", (guild_id,)
        ) as cur:
            return await cur.fetchone()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def validate_role(
    guild: discord.Guild,
    role_id: int,
) -> Optional[discord.Role]:
    """Return the role if it exists and the bot can manage it, else None."""
    role = guild.get_role(role_id)
    if role is None:
        return None
    me = guild.me
    if not me.guild_permissions.manage_roles:
        return None
    if role >= me.top_role:
        return None
    return role


async def assign_role(
    member: discord.Member,
    role: discord.Role,
) -> bool:
    """Add role to member. Returns True on success."""
    try:
        await member.add_roles(role, reason="PupPet Reaction Roles")
        return True
    except discord.Forbidden:
        log.warning("Forbidden: cannot assign %s to %s", role, member)
    except discord.HTTPException as exc:
        log.error("HTTPException assigning role: %s", exc)
    return False


async def remove_role(
    member: discord.Member,
    role: discord.Role,
) -> bool:
    """Remove role from member. Returns True on success."""
    try:
        await member.remove_roles(role, reason="PupPet Reaction Roles")
        return True
    except discord.Forbidden:
        log.warning("Forbidden: cannot remove %s from %s", role, member)
    except discord.HTTPException as exc:
        log.error("HTTPException removing role: %s", exc)
    return False


async def log_action(
    guild_id: int,
    user_id: int,
    role_id: int,
    action: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO role_logs(guild_id,user_id,role_id,action,timestamp) VALUES(?,?,?,?,?)",
            (guild_id, user_id, role_id, action, _now_iso()),
        )
        await db.commit()


async def save_sticky_role(guild_id: int, user_id: int, role_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO sticky_roles(guild_id,user_id,role_id) VALUES(?,?,?)",
            (guild_id, user_id, role_id),
        )
        await db.commit()


async def remove_sticky_role(guild_id: int, user_id: int, role_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM sticky_roles WHERE guild_id=? AND user_id=? AND role_id=?",
            (guild_id, user_id, role_id),
        )
        await db.commit()


async def _send_log_embed(
    bot: commands.Bot,
    guild_id: int,
    embed: discord.Embed,
) -> None:
    try:
        settings = await get_settings(guild_id)
        if not settings or not settings["log_channel_id"]:
            return
        channel = bot.get_channel(settings["log_channel_id"])
        if channel is None:
            return
        await channel.send(embed=embed)
    except Exception as exc:
        log.error("Failed to send log embed: %s", exc)


def _role_assigned_embed(
    member: discord.Member,
    role: discord.Role,
    source: str = "panel",
) -> discord.Embed:
    e = discord.Embed(
        title="✅ Role Assigned",
        color=COL_GREEN,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
    e.add_field(name="Role", value=role.mention, inline=True)
    e.add_field(name="Source", value=source, inline=True)
    e.set_footer(text=FOOTER)
    return e


def _role_removed_embed(
    member: discord.Member,
    role: discord.Role,
    source: str = "panel",
) -> discord.Embed:
    e = discord.Embed(
        title="➖ Role Removed",
        color=COL_RED,
        timestamp=datetime.now(timezone.utc),
    )
    e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=True)
    e.add_field(name="Role", value=role.mention, inline=True)
    e.add_field(name="Source", value=source, inline=True)
    e.set_footer(text=FOOTER)
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Role assignment logic (shared by all panel types)
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_role_toggle(
    interaction: discord.Interaction,
    item: aiosqlite.Row,
    panel: aiosqlite.Row,
    bot: commands.Bot,
) -> None:
    """
    Core toggle logic used by both button and reaction-role panels.
    Handles requirements, blacklists, groups, temp roles, sticky roles, limits.
    """
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message("❌ Server only.", ephemeral=True)
        return

    role = guild.get_role(item["role_id"])
    if role is None:
        await interaction.response.send_message(
            "❌ That role no longer exists.", ephemeral=True
        )
        return

    # ── Validate bot can manage it ─────────────────────────────────────────
    if not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
        await interaction.response.send_message(
            "❌ I don't have permission to manage that role.", ephemeral=True
        )
        return

    # ── Blacklist check ────────────────────────────────────────────────────
    if item["blacklisted_role_id"]:
        blocked = guild.get_role(item["blacklisted_role_id"])
        if blocked and blocked in member.roles:
            await interaction.response.send_message(
                f"❌ You cannot get this role while you have {blocked.mention}.",
                ephemeral=True,
            )
            return

    has_role = role in member.roles

    if not has_role:
        # ── Requirement check ──────────────────────────────────────────────
        if item["required_role_id"]:
            req = guild.get_role(item["required_role_id"])
            if req and req not in member.roles:
                await interaction.response.send_message(
                    f"❌ You need {req.mention} first.", ephemeral=True
                )
                return

        # ── Group mutual exclusion ─────────────────────────────────────────
        if item["group_name"]:
            await _strip_group_roles(guild, member, item["panel_id"], item["group_name"], bot)

        # ── Per-panel limit ────────────────────────────────────────────────
        panel_items = await get_panel_items(item["panel_id"])
        panel_role_ids = {r["role_id"] for r in panel_items}
        member_panel_roles = [r for r in member.roles if r.id in panel_role_ids]

        max_per_panel = 10  # default — can be extended per feature
        if len(member_panel_roles) >= max_per_panel and not item["group_name"]:
            await interaction.response.send_message(
                f"❌ You already have the maximum roles for this panel ({max_per_panel}).",
                ephemeral=True,
            )
            return

        # ── Assign ────────────────────────────────────────────────────────
        ok = await assign_role(member, role)
        if ok:
            await log_action(guild.id, member.id, role.id, "assigned")
            await _send_log_embed(
                bot, guild.id, _role_assigned_embed(member, role, "reaction_roles")
            )
            # Sticky store
            settings = await get_settings(guild.id)
            if settings and settings["sticky_enabled"]:
                await save_sticky_role(guild.id, member.id, role.id)
            # Temp role
            if item["temp_duration"]:
                expires = (
                    datetime.now(timezone.utc) + timedelta(minutes=item["temp_duration"])
                ).isoformat()
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT OR REPLACE INTO temp_roles(guild_id,user_id,role_id,expires_at) VALUES(?,?,?,?)",
                        (guild.id, member.id, role.id, expires),
                    )
                    await db.commit()
            await interaction.response.send_message(
                f"✅ You now have {role.mention}!", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Failed to assign role. Check my permissions.", ephemeral=True
            )
    else:
        # ── Remove ────────────────────────────────────────────────────────
        ok = await remove_role(member, role)
        if ok:
            await log_action(guild.id, member.id, role.id, "removed")
            await _send_log_embed(
                bot, guild.id, _role_removed_embed(member, role, "reaction_roles")
            )
            await remove_sticky_role(guild.id, member.id, role.id)
            # Remove temp record too
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "DELETE FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=?",
                    (guild.id, member.id, role.id),
                )
                await db.commit()
            await interaction.response.send_message(
                f"➖ Removed {role.mention}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Failed to remove role.", ephemeral=True
            )


async def _strip_group_roles(
    guild: discord.Guild,
    member: discord.Member,
    panel_id: int,
    group_name: str,
    bot: commands.Bot,
) -> None:
    """Remove all roles in the same group from the member."""
    items = await get_panel_items(panel_id)
    for item in items:
        if item["group_name"] == group_name:
            r = guild.get_role(item["role_id"])
            if r and r in member.roles:
                await remove_role(member, r)
                await log_action(guild.id, member.id, r.id, "group_swap_removed")
                await _send_log_embed(
                    bot, guild.id, _role_removed_embed(member, r, "group_swap")
                )
                await remove_sticky_role(guild.id, member.id, r.id)


# ─────────────────────────────────────────────────────────────────────────────
# Persistent Views
# ─────────────────────────────────────────────────────────────────────────────

class RolePanelButtonView(discord.ui.View):
    """Persistent button-role view for a panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @classmethod
    async def build(cls, panel_id: int) -> "RolePanelButtonView":
        view = cls()
        items = await get_panel_items(panel_id)
        for item in items:
            style = discord.ButtonStyle(item["style"]) if item["style"] in (1, 2, 3, 4) else discord.ButtonStyle.secondary
            btn = RoleButton(
                item_id=item["id"],
                panel_id=panel_id,
                label=item["label"],
                emoji=item["emoji"] or None,
                style=style,
            )
            view.add_item(btn)
        return view


class RoleButton(discord.ui.Button):
    def __init__(
        self,
        item_id: int,
        panel_id: int,
        label: str,
        emoji: Optional[str],
        style: discord.ButtonStyle,
    ) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"rr:btn:{panel_id}:{item_id}",
        )
        self.item_id = item_id
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        item = await get_role_item(self.item_id)
        if item is None:
            await interaction.response.send_message("❌ Role item not found.", ephemeral=True)
            return
        panel = await get_panel(self.panel_id)
        if panel is None:
            await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
            return
        await _handle_role_toggle(interaction, item, panel, interaction.client)


class RolePanelSelectView(discord.ui.View):
    """Persistent select-menu role view."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @classmethod
    async def build(cls, panel_id: int, multi: bool = False) -> "RolePanelSelectView":
        view = cls()
        items = await get_panel_items(panel_id)
        if not items:
            return view
        options = [
            discord.SelectOption(
                label=it["label"][:100],
                value=str(it["id"]),
                emoji=it["emoji"] or None,
                description=it["description"][:100] if it["description"] else None,
            )
            for it in items
        ]
        max_v = len(options) if multi else 1
        select = RoleSelect(
            panel_id=panel_id,
            options=options,
            min_values=1,
            max_values=max_v,
        )
        view.add_item(select)
        return view


class RoleSelect(discord.ui.Select):
    def __init__(
        self,
        panel_id: int,
        options: list[discord.SelectOption],
        min_values: int,
        max_values: int,
    ) -> None:
        super().__init__(
            custom_id=f"rr:sel:{panel_id}",
            placeholder="Select role(s)…",
            min_values=min_values,
            max_values=max_values,
            options=options,
        )
        self.panel_id = panel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        panel = await get_panel(self.panel_id)
        if panel is None:
            await interaction.response.send_message("❌ Panel not found.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        results: list[str] = []
        for item_id_str in self.values:
            item = await get_role_item(int(item_id_str))
            if item is None:
                continue
            guild = interaction.guild
            member = interaction.user
            if guild is None or not isinstance(member, discord.Member):
                continue
            role = guild.get_role(item["role_id"])
            if role is None:
                results.append(f"❌ Role not found for {item['label']}")
                continue
            if role in member.roles:
                ok = await remove_role(member, role)
                if ok:
                    await log_action(guild.id, member.id, role.id, "removed")
                    await _send_log_embed(
                        interaction.client, guild.id,
                        _role_removed_embed(member, role, "select_menu"),
                    )
                    await remove_sticky_role(guild.id, member.id, role.id)
                    results.append(f"➖ Removed {role.name}")
            else:
                # blacklist / requirement checks
                if item["blacklisted_role_id"]:
                    blocked = guild.get_role(item["blacklisted_role_id"])
                    if blocked and blocked in member.roles:
                        results.append(f"❌ Blocked ({blocked.name}) for {role.name}")
                        continue
                if item["required_role_id"]:
                    req = guild.get_role(item["required_role_id"])
                    if req and req not in member.roles:
                        results.append(f"❌ Need {req.name} first for {role.name}")
                        continue
                if item["group_name"]:
                    await _strip_group_roles(guild, member, item["panel_id"], item["group_name"], interaction.client)
                ok = await assign_role(member, role)
                if ok:
                    await log_action(guild.id, member.id, role.id, "assigned")
                    await _send_log_embed(
                        interaction.client, guild.id,
                        _role_assigned_embed(member, role, "select_menu"),
                    )
                    settings = await get_settings(guild.id)
                    if settings and settings["sticky_enabled"]:
                        await save_sticky_role(guild.id, member.id, role.id)
                    results.append(f"✅ Got {role.name}")
                else:
                    results.append(f"❌ Failed for {role.name}")

        msg = "\n".join(results) if results else "No changes."
        await interaction.followup.send(msg, ephemeral=True)


class VerifyView(discord.ui.View):
    """Persistent verification panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅ Accept Rules",
        style=discord.ButtonStyle.success,
        custom_id="rr:verify:accept",
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        # Fetch configured verify role
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM role_panels WHERE guild_id=? AND panel_type='verify' ORDER BY panel_id DESC LIMIT 1",
                (guild.id,),
            ) as cur:
                panel = await cur.fetchone()

        if panel is None:
            await interaction.response.send_message(
                "❌ Verify panel not configured.", ephemeral=True
            )
            return

        items = await get_panel_items(panel["panel_id"])
        if not items:
            await interaction.response.send_message(
                "❌ No verify role configured.", ephemeral=True
            )
            return

        role = guild.get_role(items[0]["role_id"])
        if role is None:
            await interaction.response.send_message(
                "❌ Verify role not found.", ephemeral=True
            )
            return

        if role in member.roles:
            await interaction.response.send_message(
                "✅ You are already verified!", ephemeral=True
            )
            return

        ok = await assign_role(member, role)
        if ok:
            await log_action(guild.id, member.id, role.id, "verify_accepted")
            e = discord.Embed(
                title="✅ Verified",
                description=f"{member.mention} accepted the rules.",
                color=COL_GREEN,
                timestamp=datetime.now(timezone.utc),
            )
            e.add_field(name="Role", value=role.mention)
            e.set_footer(text=FOOTER)
            await _send_log_embed(interaction.client, guild.id, e)
            await interaction.response.send_message(
                f"✅ You have been verified and given {role.mention}!", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Could not assign verify role. Contact an admin.", ephemeral=True
            )


# ─────────────────────────────────────────────────────────────────────────────
# View restore helper
# ─────────────────────────────────────────────────────────────────────────────

async def restore_views(bot: commands.Bot) -> None:
    """Re-register all persistent panel views after restart."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM role_panels WHERE message_id IS NOT NULL"
            ) as cur:
                panels = await cur.fetchall()

        for panel in panels:
            try:
                ptype = panel["panel_type"]
                pid = panel["panel_id"]
                if ptype == "button":
                    view = await RolePanelButtonView.build(pid)
                    bot.add_view(view, message_id=panel["message_id"])
                elif ptype in ("select", "select_multi"):
                    view = await RolePanelSelectView.build(pid, multi=(ptype == "select_multi"))
                    bot.add_view(view, message_id=panel["message_id"])
                elif ptype == "verify":
                    view = VerifyView()
                    bot.add_view(view, message_id=panel["message_id"])
            except Exception as exc:
                log.error("Failed to restore view for panel %s: %s", panel["panel_id"], exc)

        log.info("Restored persistent views for %d panels.", len(panels))
    except Exception as exc:
        log.error("restore_views failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Embeds
# ─────────────────────────────────────────────────────────────────────────────

def _panel_embed(panel: aiosqlite.Row, items: list[aiosqlite.Row]) -> discord.Embed:
    e = discord.Embed(
        title=panel["title"],
        description=panel["description"] or "",
        color=COL_BRAND,
        timestamp=datetime.now(timezone.utc),
    )
    if items:
        lines = []
        for it in items:
            prefix = it["emoji"] + " " if it["emoji"] else ""
            desc = f" — {it['description']}" if it["description"] else ""
            lines.append(f"{prefix}**{it['label']}**{desc}")
        e.add_field(name="Roles", value="\n".join(lines), inline=False)
    e.set_footer(text=FOOTER)
    return e


def _verify_embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(
        title=title,
        description=description,
        color=COL_BLUE,
        timestamp=datetime.now(timezone.utc),
    )
    e.set_footer(text=FOOTER)
    return e


def _err(title: str, desc: str = "") -> discord.Embed:
    e = discord.Embed(title=f"❌ {title}", description=desc, color=COL_RED)
    e.set_footer(text=FOOTER)
    return e


def _ok(title: str, desc: str = "") -> discord.Embed:
    e = discord.Embed(title=f"✅ {title}", description=desc, color=COL_GREEN)
    e.set_footer(text=FOOTER)
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Main Cog
# ─────────────────────────────────────────────────────────────────────────────

class ReactionRoles(commands.Cog, name="ReactionRoles"):

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        await _init_db()
        await restore_views(self.bot)
        self._temp_role_checker.start()
        log.info("ReactionRoles cog loaded.")

    def cog_unload(self) -> None:
        self._temp_role_checker.cancel()
        log.info("ReactionRoles cog unloaded.")

    # ── Background: temp role expiration ─────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _temp_role_checker(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM temp_roles WHERE expires_at <= ?", (now,)
                ) as cur:
                    expired = await cur.fetchall()

            for row in expired:
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    continue
                member = guild.get_member(row["user_id"])
                role = guild.get_role(row["role_id"])
                if member and role and role in member.roles:
                    ok = await remove_role(member, role)
                    if ok:
                        await log_action(guild.id, member.id, role.id, "temp_expired")
                        e = discord.Embed(
                            title="⏰ Temp Role Expired",
                            color=COL_YELLOW,
                            timestamp=datetime.now(timezone.utc),
                        )
                        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)")
                        e.add_field(name="Role", value=role.mention)
                        e.set_footer(text=FOOTER)
                        await _send_log_embed(self.bot, guild.id, e)

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "DELETE FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=?",
                        (row["guild_id"], row["user_id"], row["role_id"]),
                    )
                    await db.commit()
        except Exception as exc:
            log.error("Temp role checker error: %s", exc)

    @_temp_role_checker.before_loop
    async def _before_temp(self) -> None:
        await self.bot.wait_until_ready()

    # ── on_member_remove — save sticky roles ──────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            settings = await get_settings(member.guild.id)
            if not settings or not settings["sticky_enabled"]:
                return
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT role_id FROM sticky_roles WHERE guild_id=? AND user_id=?",
                    (member.guild.id, member.id),
                ) as cur:
                    existing = {r["role_id"] for r in await cur.fetchall()}

            # Get all self-assignable role IDs from panels
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT ri.role_id FROM role_items ri
                       JOIN role_panels rp ON ri.panel_id = rp.panel_id
                       WHERE rp.guild_id = ?""",
                    (member.guild.id,),
                ) as cur:
                    panel_role_ids = {r["role_id"] for r in await cur.fetchall()}

            for role in member.roles:
                if role.id in panel_role_ids and role.id not in existing:
                    await save_sticky_role(member.guild.id, member.id, role.id)
        except Exception as exc:
            log.error("on_member_remove sticky error: %s", exc)

    # ── on_member_join — restore sticky roles ─────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        try:
            settings = await get_settings(member.guild.id)
            if not settings or not settings["sticky_enabled"]:
                return
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT role_id FROM sticky_roles WHERE guild_id=? AND user_id=?",
                    (member.guild.id, member.id),
                ) as cur:
                    rows = await cur.fetchall()

            restored: list[str] = []
            for row in rows:
                role = member.guild.get_role(row["role_id"])
                if role and await validate_role(member.guild, role.id):
                    ok = await assign_role(member, role)
                    if ok:
                        restored.append(role.name)
                        await log_action(member.guild.id, member.id, role.id, "sticky_restored")

            if restored:
                e = discord.Embed(
                    title="🔁 Sticky Roles Restored",
                    description=f"{member.mention} rejoined. Restored: {', '.join(restored)}",
                    color=COL_BLUE,
                    timestamp=datetime.now(timezone.utc),
                )
                e.set_footer(text=FOOTER)
                await _send_log_embed(self.bot, member.guild.id, e)
        except Exception as exc:
            log.error("on_member_join sticky error: %s", exc)

    # ── on_raw_reaction_add / remove — classic reaction roles ─────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, adding=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, adding=False)

    async def _handle_reaction(
        self, payload: discord.RawReactionActionEvent, adding: bool
    ) -> None:
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        try:
            emoji_str = str(payload.emoji)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """SELECT ri.* FROM role_items ri
                       JOIN role_panels rp ON ri.panel_id = rp.panel_id
                       WHERE rp.guild_id = ? AND rp.message_id = ? AND ri.emoji = ? AND rp.panel_type='reaction'""",
                    (payload.guild_id, payload.message_id, emoji_str),
                ) as cur:
                    item = await cur.fetchone()

            if item is None:
                return

            guild = self.bot.get_guild(payload.guild_id)
            if guild is None:
                return
            member = guild.get_member(payload.user_id)
            if member is None:
                return
            role = guild.get_role(item["role_id"])
            if role is None:
                return

            if not await validate_role(guild, role.id):
                return

            if adding:
                ok = await assign_role(member, role)
                if ok:
                    await log_action(guild.id, member.id, role.id, "reaction_assigned")
                    await _send_log_embed(
                        self.bot, guild.id,
                        _role_assigned_embed(member, role, "reaction"),
                    )
                    settings = await get_settings(guild.id)
                    if settings and settings["sticky_enabled"]:
                        await save_sticky_role(guild.id, member.id, role.id)
            else:
                ok = await remove_role(member, role)
                if ok:
                    await log_action(guild.id, member.id, role.id, "reaction_removed")
                    await _send_log_embed(
                        self.bot, guild.id,
                        _role_removed_embed(member, role, "reaction"),
                    )
                    await remove_sticky_role(guild.id, member.id, role.id)
        except Exception as exc:
            log.error("reaction handler error: %s", exc)

    # ═════════════════════════════════════════════════════════════════════════
    # /rolepanel
    # ═════════════════════════════════════════════════════════════════════════

    rolepanel = app_commands.Group(
        name="rolepanel",
        description="Manage role panels.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @rolepanel.command(name="create", description="Create a new role panel.")
    @app_commands.describe(
        title="Panel title",
        description="Panel description",
        panel_type="Panel type: button, select, select_multi, reaction, verify",
        channel="Channel to post the panel in",
    )
    @app_commands.choices(panel_type=[
        app_commands.Choice(name="Button roles",      value="button"),
        app_commands.Choice(name="Select (single)",   value="select"),
        app_commands.Choice(name="Select (multi)",    value="select_multi"),
        app_commands.Choice(name="Reaction roles",    value="reaction"),
        app_commands.Choice(name="Verify panel",      value="verify"),
    ])
    async def rolepanel_create(
        self,
        interaction: discord.Interaction,
        title: str,
        panel_type: str,
        channel: discord.TextChannel,
        description: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO role_panels(guild_id,channel_id,title,description,panel_type,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    interaction.guild.id,
                    channel.id,
                    title[:256],
                    description[:1024],
                    panel_type,
                    interaction.user.id,
                    _now_iso(),
                ),
            )
            panel_id = cur.lastrowid
            await db.commit()

        e = _ok(
            "Panel Created",
            f"Panel **#{panel_id}** created (type: `{panel_type}`).\n"
            f"Add roles with `/buttonrole add`, `/selectrole add`, or `/reactionrole add`,\n"
            f"then use `/rolepanel post {panel_id}` to post it (or re-run after adding roles).",
        )
        e.add_field(name="Panel ID", value=str(panel_id), inline=True)
        e.add_field(name="Channel", value=channel.mention, inline=True)
        await interaction.followup.send(embed=e)

        e2 = discord.Embed(
            title="📋 Panel Created",
            description=f"Panel **#{panel_id}** (`{panel_type}`) created by {interaction.user.mention}.",
            color=COL_BRAND,
            timestamp=datetime.now(timezone.utc),
        )
        e2.set_footer(text=FOOTER)
        await _send_log_embed(self.bot, interaction.guild.id, e2)

    @rolepanel.command(name="post", description="Post (or re-post) a panel to its channel.")
    @app_commands.describe(panel_id="Panel ID to post")
    async def rolepanel_post(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return

        channel = self.bot.get_channel(panel["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(embed=_err("Channel not found or not a text channel."))
            return

        items = await get_panel_items(panel_id)
        ptype = panel["panel_type"]

        if ptype == "button":
            view = await RolePanelButtonView.build(panel_id)
            embed = _panel_embed(panel, items)
            msg = await channel.send(embed=embed, view=view)
        elif ptype in ("select", "select_multi"):
            view = await RolePanelSelectView.build(panel_id, multi=(ptype == "select_multi"))
            embed = _panel_embed(panel, items)
            msg = await channel.send(embed=embed, view=view)
        elif ptype == "verify":
            embed = _verify_embed(panel["title"], panel["description"] or "Click below to verify.")
            view = VerifyView()
            msg = await channel.send(embed=embed, view=view)
        elif ptype == "reaction":
            embed = _panel_embed(panel, items)
            msg = await channel.send(embed=embed)
            for it in items:
                if it["emoji"]:
                    try:
                        await msg.add_reaction(it["emoji"])
                    except Exception as exc:
                        log.warning("Could not add reaction %s: %s", it["emoji"], exc)
        else:
            await interaction.followup.send(embed=_err(f"Unknown panel type `{ptype}`."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE role_panels SET message_id=?, channel_id=? WHERE panel_id=?",
                (msg.id, channel.id, panel_id),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Panel Posted", f"Panel **#{panel_id}** posted in {channel.mention}.")
        )

    @rolepanel.command(name="edit", description="Edit panel title/description.")
    @app_commands.describe(
        panel_id="Panel ID",
        title="New title (leave blank to keep)",
        description="New description (leave blank to keep)",
    )
    async def rolepanel_edit(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        title: str = "",
        description: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return

        new_title = title or panel["title"]
        new_desc = description if description != "" else panel["description"]

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE role_panels SET title=?, description=? WHERE panel_id=?",
                (new_title[:256], new_desc[:1024], panel_id),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Panel Updated", f"Panel **#{panel_id}** updated. Re-post with `/rolepanel post {panel_id}`.")
        )

    @rolepanel.command(name="delete", description="Delete a role panel.")
    @app_commands.describe(panel_id="Panel ID to delete")
    async def rolepanel_delete(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return

        # Try to delete the message
        try:
            if panel["message_id"] and panel["channel_id"]:
                ch = self.bot.get_channel(panel["channel_id"])
                if isinstance(ch, discord.TextChannel):
                    msg = await ch.fetch_message(panel["message_id"])
                    await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM role_items WHERE panel_id=?", (panel_id,))
            await db.execute("DELETE FROM role_panels WHERE panel_id=?", (panel_id,))
            await db.commit()

        e2 = discord.Embed(
            title="🗑️ Panel Deleted",
            description=f"Panel **#{panel_id}** deleted by {interaction.user.mention}.",
            color=COL_RED,
            timestamp=datetime.now(timezone.utc),
        )
        e2.set_footer(text=FOOTER)
        await _send_log_embed(self.bot, interaction.guild.id, e2)
        await interaction.followup.send(embed=_ok("Panel Deleted", f"Panel **#{panel_id}** deleted."))

    @rolepanel.command(name="list", description="List all role panels in this server.")
    async def rolepanel_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM role_panels WHERE guild_id=? ORDER BY panel_id",
                (interaction.guild.id,),
            ) as cur:
                panels = await cur.fetchall()

        if not panels:
            await interaction.followup.send(embed=_err("No panels found."))
            return

        e = discord.Embed(
            title="📋 Role Panels",
            color=COL_BRAND,
            timestamp=datetime.now(timezone.utc),
        )
        for p in panels:
            ch = f"<#{p['channel_id']}>" if p["channel_id"] else "?"
            e.add_field(
                name=f"#{p['panel_id']} — {p['title']}",
                value=f"Type: `{p['panel_type']}` | Channel: {ch}",
                inline=False,
            )
        e.set_footer(text=FOOTER)
        await interaction.followup.send(embed=e)

    @rolepanel.command(name="info", description="Detailed info about a panel.")
    @app_commands.describe(panel_id="Panel ID")
    async def rolepanel_info(self, interaction: discord.Interaction, panel_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        items = await get_panel_items(panel_id)
        e = discord.Embed(
            title=f"Panel #{panel_id} — {panel['title']}",
            description=panel["description"] or "",
            color=COL_BRAND,
            timestamp=datetime.now(timezone.utc),
        )
        e.add_field(name="Type",    value=panel["panel_type"], inline=True)
        e.add_field(name="Channel", value=f"<#{panel['channel_id']}>", inline=True)
        e.add_field(name="Created", value=panel["created_at"][:10], inline=True)
        if items:
            lines = []
            for it in items:
                req = f" [req: <@&{it['required_role_id']}>]" if it["required_role_id"] else ""
                grp = f" [group: {it['group_name']}]" if it["group_name"] else ""
                tmp = f" [{it['temp_duration']}m]" if it["temp_duration"] else ""
                lines.append(f"`{it['id']}` {it['emoji'] or ''} **{it['label']}** → <@&{it['role_id']}>{req}{grp}{tmp}")
            e.add_field(name="Items", value="\n".join(lines) or "None", inline=False)
        e.set_footer(text=FOOTER)
        await interaction.followup.send(embed=e)

    @rolepanel.command(name="clone", description="Clone an existing panel.")
    @app_commands.describe(panel_id="Panel ID to clone", channel="Channel for the clone")
    async def rolepanel_clone(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        items = await get_panel_items(panel_id)

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO role_panels(guild_id,channel_id,title,description,panel_type,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    interaction.guild.id,
                    channel.id,
                    panel["title"] + " (clone)",
                    panel["description"],
                    panel["panel_type"],
                    interaction.user.id,
                    _now_iso(),
                ),
            )
            new_id = cur.lastrowid
            for it in items:
                await db.execute(
                    """INSERT INTO role_items(panel_id,role_id,emoji,label,description,style,group_name,
                       required_role_id,blacklisted_role_id,temp_duration,position)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        new_id, it["role_id"], it["emoji"], it["label"],
                        it["description"], it["style"], it["group_name"],
                        it["required_role_id"], it["blacklisted_role_id"],
                        it["temp_duration"], it["position"],
                    ),
                )
            await db.commit()

        await interaction.followup.send(
            embed=_ok(
                "Panel Cloned",
                f"Panel **#{panel_id}** cloned as **#{new_id}**. Use `/rolepanel post {new_id}` to post it.",
            )
        )

    # ═════════════════════════════════════════════════════════════════════════
    # /buttonrole
    # ═════════════════════════════════════════════════════════════════════════

    buttonrole = app_commands.Group(
        name="buttonrole",
        description="Manage button roles on a panel.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @buttonrole.command(name="add", description="Add a button role to a panel.")
    @app_commands.describe(
        panel_id="Panel ID",
        role="Role to assign",
        label="Button label",
        style="Button colour: 1=Primary 2=Secondary 3=Success 4=Danger",
        emoji="Optional emoji",
        description="Optional description",
        required_role="Role required before getting this role",
        blacklisted_role="Role that blocks this assignment",
        group_name="Mutual-exclusion group name",
        temp_duration="Temp duration in minutes (0 = permanent)",
    )
    @app_commands.choices(style=[
        app_commands.Choice(name="Primary (blue)",   value=1),
        app_commands.Choice(name="Secondary (grey)", value=2),
        app_commands.Choice(name="Success (green)",  value=3),
        app_commands.Choice(name="Danger (red)",     value=4),
    ])
    async def buttonrole_add(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        role: discord.Role,
        label: str,
        style: int = 2,
        emoji: str = "",
        description: str = "",
        required_role: Optional[discord.Role] = None,
        blacklisted_role: Optional[discord.Role] = None,
        group_name: str = "",
        temp_duration: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        if panel["panel_type"] != "button":
            await interaction.followup.send(embed=_err("That panel is not a button panel."))
            return
        if not await validate_role(interaction.guild, role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role (hierarchy or permissions)."))
            return

        # Count existing items for position
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM role_items WHERE panel_id=?", (panel_id,)
            ) as cur:
                count = (await cur.fetchone())[0]

        if count >= 25:
            await interaction.followup.send(embed=_err("Max 25 items per panel."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO role_items(panel_id,role_id,emoji,label,description,style,group_name,
                   required_role_id,blacklisted_role_id,temp_duration,position)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    panel_id, role.id,
                    emoji[:64] if emoji else None,
                    label[:80],
                    description[:200],
                    style,
                    group_name[:64] if group_name else None,
                    required_role.id if required_role else None,
                    blacklisted_role.id if blacklisted_role else None,
                    temp_duration or None,
                    count,
                ),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok(
                "Button Role Added",
                f"**{label}** → {role.mention} added to panel **#{panel_id}**.\n"
                f"Re-post with `/rolepanel post {panel_id}`.",
            )
        )

    @buttonrole.command(name="edit", description="Edit a button role item.")
    @app_commands.describe(
        item_id="Item ID (see /rolepanel info)",
        label="New label",
        emoji="New emoji",
        style="New style",
    )
    @app_commands.choices(style=[
        app_commands.Choice(name="Primary (blue)",   value=1),
        app_commands.Choice(name="Secondary (grey)", value=2),
        app_commands.Choice(name="Success (green)",  value=3),
        app_commands.Choice(name="Danger (red)",     value=4),
    ])
    async def buttonrole_edit(
        self,
        interaction: discord.Interaction,
        item_id: int,
        label: str = "",
        emoji: str = "",
        style: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        item = await get_role_item(item_id)
        if item is None:
            await interaction.followup.send(embed=_err("Item not found."))
            return
        panel = await get_panel(item["panel_id"])
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return

        new_label = label or item["label"]
        new_emoji = emoji or item["emoji"]
        new_style = style if style in (1, 2, 3, 4) else item["style"]

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE role_items SET label=?, emoji=?, style=? WHERE id=?",
                (new_label[:80], new_emoji, new_style, item_id),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Item Updated", f"Item `{item_id}` updated. Re-post the panel to apply.")
        )

    @buttonrole.command(name="remove", description="Remove a role item from a panel.")
    @app_commands.describe(item_id="Item ID (see /rolepanel info)")
    async def buttonrole_remove(self, interaction: discord.Interaction, item_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        item = await get_role_item(item_id)
        if item is None:
            await interaction.followup.send(embed=_err("Item not found."))
            return
        panel = await get_panel(item["panel_id"])
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM role_items WHERE id=?", (item_id,))
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Item Removed", f"Item `{item_id}` removed. Re-post the panel to apply.")
        )

    # ═════════════════════════════════════════════════════════════════════════
    # /selectrole
    # ═════════════════════════════════════════════════════════════════════════

    selectrole = app_commands.Group(
        name="selectrole",
        description="Manage select menu roles on a panel.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @selectrole.command(name="add", description="Add a role option to a select panel.")
    @app_commands.describe(
        panel_id="Panel ID",
        role="Role to assign",
        label="Option label",
        description="Option description",
        emoji="Optional emoji",
        required_role="Role required before getting this",
        blacklisted_role="Role that blocks this",
        group_name="Mutual-exclusion group",
        temp_duration="Duration in minutes (0 = permanent)",
    )
    async def selectrole_add(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        role: discord.Role,
        label: str,
        description: str = "",
        emoji: str = "",
        required_role: Optional[discord.Role] = None,
        blacklisted_role: Optional[discord.Role] = None,
        group_name: str = "",
        temp_duration: int = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        if panel["panel_type"] not in ("select", "select_multi"):
            await interaction.followup.send(embed=_err("That panel is not a select panel."))
            return
        if not await validate_role(interaction.guild, role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM role_items WHERE panel_id=?", (panel_id,)
            ) as cur:
                count = (await cur.fetchone())[0]
        if count >= 25:
            await interaction.followup.send(embed=_err("Max 25 options per select."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO role_items(panel_id,role_id,emoji,label,description,style,group_name,
                   required_role_id,blacklisted_role_id,temp_duration,position)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    panel_id, role.id,
                    emoji[:64] if emoji else None,
                    label[:100],
                    description[:100],
                    2,
                    group_name[:64] if group_name else None,
                    required_role.id if required_role else None,
                    blacklisted_role.id if blacklisted_role else None,
                    temp_duration or None,
                    count,
                ),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Option Added", f"**{label}** → {role.mention} added. Re-post panel to apply.")
        )

    @selectrole.command(name="edit", description="Edit a select option label/description.")
    @app_commands.describe(item_id="Item ID", label="New label", description="New description")
    async def selectrole_edit(
        self,
        interaction: discord.Interaction,
        item_id: int,
        label: str = "",
        description: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        item = await get_role_item(item_id)
        if item is None:
            await interaction.followup.send(embed=_err("Item not found."))
            return
        panel = await get_panel(item["panel_id"])
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE role_items SET label=?, description=? WHERE id=?",
                (label or item["label"], description or item["description"], item_id),
            )
            await db.commit()
        await interaction.followup.send(embed=_ok("Option Updated", "Re-post panel to apply."))

    @selectrole.command(name="remove", description="Remove a select option.")
    @app_commands.describe(item_id="Item ID")
    async def selectrole_remove(self, interaction: discord.Interaction, item_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        item = await get_role_item(item_id)
        if item is None:
            await interaction.followup.send(embed=_err("Item not found."))
            return
        panel = await get_panel(item["panel_id"])
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM role_items WHERE id=?", (item_id,))
            await db.commit()
        await interaction.followup.send(embed=_ok("Option Removed", "Re-post panel to apply."))

    # ═════════════════════════════════════════════════════════════════════════
    # /reactionrole
    # ═════════════════════════════════════════════════════════════════════════

    reactionrole = app_commands.Group(
        name="reactionrole",
        description="Manage classic reaction roles.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @reactionrole.command(name="add", description="Add a reaction → role mapping to a reaction panel.")
    @app_commands.describe(
        panel_id="Panel ID (must be type 'reaction')",
        role="Role to assign",
        emoji="Emoji to use",
        label="Label for the embed",
    )
    async def reactionrole_add(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        role: discord.Role,
        emoji: str,
        label: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        if panel["panel_type"] != "reaction":
            await interaction.followup.send(embed=_err("That panel is not a reaction panel."))
            return
        if not await validate_role(interaction.guild, role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM role_items WHERE panel_id=?", (panel_id,)
            ) as cur:
                count = (await cur.fetchone())[0]
            await db.execute(
                """INSERT INTO role_items(panel_id,role_id,emoji,label,description,style,position)
                   VALUES(?,?,?,?,?,?,?)""",
                (panel_id, role.id, emoji[:64], label[:80] or str(role.name), "", 2, count),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Reaction Role Added", f"{emoji} → {role.mention} added. Re-post panel to apply reactions.")
        )

    @reactionrole.command(name="remove", description="Remove a reaction → role mapping.")
    @app_commands.describe(item_id="Item ID (see /rolepanel info)")
    async def reactionrole_remove(self, interaction: discord.Interaction, item_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        item = await get_role_item(item_id)
        if item is None:
            await interaction.followup.send(embed=_err("Item not found."))
            return
        panel = await get_panel(item["panel_id"])
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM role_items WHERE id=?", (item_id,))
            await db.commit()
        await interaction.followup.send(embed=_ok("Mapping Removed"))

    @reactionrole.command(name="list", description="List all reaction role mappings for a panel.")
    @app_commands.describe(panel_id="Panel ID")
    async def reactionrole_list(self, interaction: discord.Interaction, panel_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        items = await get_panel_items(panel_id)
        if not items:
            await interaction.followup.send(embed=_err("No items."))
            return
        e = discord.Embed(title=f"Reaction Roles — Panel #{panel_id}", color=COL_BRAND)
        for it in items:
            e.add_field(
                name=f"{it['emoji'] or '?'} {it['label']}",
                value=f"<@&{it['role_id']}> (item `{it['id']}`)",
                inline=False,
            )
        e.set_footer(text=FOOTER)
        await interaction.followup.send(embed=e)

    # ═════════════════════════════════════════════════════════════════════════
    # /verify
    # ═════════════════════════════════════════════════════════════════════════

    verify_group = app_commands.Group(
        name="verify",
        description="Manage the verification panel.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @verify_group.command(name="create", description="Post a verification panel.")
    @app_commands.describe(
        channel="Channel to post the panel in",
        title="Embed title",
        description="Embed description",
        verify_role="Role to assign on verify",
    )
    async def verify_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        verify_role: discord.Role,
        title: str = "✅ Server Verification",
        description: str = "Click the button below to accept our rules and gain access to the server.",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        if not await validate_role(interaction.guild, verify_role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO role_panels(guild_id,channel_id,title,description,panel_type,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    interaction.guild.id, channel.id,
                    title[:256], description[:1024],
                    "verify", interaction.user.id, _now_iso(),
                ),
            )
            panel_id = cur.lastrowid
            await db.execute(
                "INSERT INTO role_items(panel_id,role_id,emoji,label,description,style,position) VALUES(?,?,?,?,?,?,?)",
                (panel_id, verify_role.id, None, "Verify", "", 3, 0),
            )
            await db.commit()

        view = VerifyView()
        embed = _verify_embed(title, description)
        msg = await channel.send(embed=embed, view=view)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE role_panels SET message_id=? WHERE panel_id=?",
                (msg.id, panel_id),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Verify Panel Created", f"Posted in {channel.mention}. Panel ID: **#{panel_id}**.")
        )

    @verify_group.command(name="role", description="Change the verify role for the active verify panel.")
    @app_commands.describe(panel_id="Panel ID", new_role="New verify role")
    async def verify_role(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        new_role: discord.Role,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        if panel["panel_type"] != "verify":
            await interaction.followup.send(embed=_err("Not a verify panel."))
            return
        if not await validate_role(interaction.guild, new_role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE role_items SET role_id=? WHERE panel_id=?",
                (new_role.id, panel_id),
            )
            await db.commit()
        await interaction.followup.send(embed=_ok("Verify Role Updated", f"Now assigns {new_role.mention}."))

    # ═════════════════════════════════════════════════════════════════════════
    # /rolegroup
    # ═════════════════════════════════════════════════════════════════════════

    rolegroup = app_commands.Group(
        name="rolegroup",
        description="Manage mutual-exclusion role groups.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @rolegroup.command(name="create", description="Create a role group panel.")
    @app_commands.describe(
        channel="Channel to post",
        title="Panel title",
        group_name="Internal group name (e.g. Platform)",
        description="Panel description",
    )
    async def rolegroup_create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        group_name: str,
        description: str = "Pick one role from this group.",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """INSERT INTO role_panels(guild_id,channel_id,title,description,panel_type,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    interaction.guild.id, channel.id,
                    title[:256], description[:1024],
                    "button", interaction.user.id, _now_iso(),
                ),
            )
            panel_id = cur.lastrowid
            await db.commit()

        await interaction.followup.send(
            embed=_ok(
                "Role Group Created",
                f"Panel **#{panel_id}** created for group `{group_name}`.\n"
                f"Add roles with `/rolegroup addrole {panel_id} @Role <label> {group_name}`\n"
                f"then post with `/rolepanel post {panel_id}`.",
            )
        )

    @rolegroup.command(name="addrole", description="Add a role to a group panel.")
    @app_commands.describe(
        panel_id="Panel ID",
        role="Role to add",
        label="Button label",
        group_name="Group name (must match the group)",
        emoji="Optional emoji",
    )
    async def rolegroup_addrole(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        role: discord.Role,
        label: str,
        group_name: str,
        emoji: str = "",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.followup.send(embed=_err("Panel not found."))
            return
        if not await validate_role(interaction.guild, role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role."))
            return

        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM role_items WHERE panel_id=?", (panel_id,)
            ) as cur:
                count = (await cur.fetchone())[0]
            await db.execute(
                """INSERT INTO role_items(panel_id,role_id,emoji,label,description,style,group_name,position)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    panel_id, role.id,
                    emoji[:64] if emoji else None,
                    label[:80], "", 2, group_name[:64], count,
                ),
            )
            await db.commit()

        await interaction.followup.send(
            embed=_ok("Role Added to Group", f"{role.mention} added to `{group_name}`. Re-post panel to apply.")
        )

    @rolegroup.command(name="remove", description="Remove a group panel.")
    @app_commands.describe(panel_id="Panel ID to remove")
    async def rolegroup_remove(self, interaction: discord.Interaction, panel_id: int) -> None:
        # Delegate to rolepanel_delete logic
        panel = await get_panel(panel_id)
        if panel is None or (interaction.guild and panel["guild_id"] != interaction.guild.id):
            await interaction.response.send_message(embed=_err("Panel not found."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM role_items WHERE panel_id=?", (panel_id,))
            await db.execute("DELETE FROM role_panels WHERE panel_id=?", (panel_id,))
            await db.commit()
        await interaction.followup.send(embed=_ok("Group Panel Removed"))

    # ═════════════════════════════════════════════════════════════════════════
    # /temprole
    # ═════════════════════════════════════════════════════════════════════════

    temprole = app_commands.Group(
        name="temprole",
        description="Assign temporary roles.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @temprole.command(name="add", description="Assign a role temporarily to a member.")
    @app_commands.describe(
        member="Member to assign the role to",
        role="Role to assign",
        duration="Duration in minutes",
    )
    async def temprole_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        duration: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        if not await validate_role(interaction.guild, role.id):
            await interaction.followup.send(embed=_err("I cannot manage that role."))
            return
        if duration <= 0:
            await interaction.followup.send(embed=_err("Duration must be > 0 minutes."))
            return

        expires = (datetime.now(timezone.utc) + timedelta(minutes=duration)).isoformat()
        ok = await assign_role(member, role)
        if ok:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO temp_roles(guild_id,user_id,role_id,expires_at) VALUES(?,?,?,?)",
                    (interaction.guild.id, member.id, role.id, expires),
                )
                await db.commit()
            await log_action(interaction.guild.id, member.id, role.id, "temp_assigned")
            e = discord.Embed(
                title="⏱️ Temp Role Assigned",
                color=COL_YELLOW,
                timestamp=datetime.now(timezone.utc),
            )
            e.add_field(name="Member", value=member.mention)
            e.add_field(name="Role", value=role.mention)
            e.add_field(name="Duration", value=f"{duration} minutes")
            e.add_field(name="Expires", value=f"<t:{int((datetime.now(timezone.utc) + timedelta(minutes=duration)).timestamp())}:R>")
            e.set_footer(text=FOOTER)
            await _send_log_embed(self.bot, interaction.guild.id, e)
            await interaction.followup.send(embed=e)
        else:
            await interaction.followup.send(embed=_err("Failed to assign role."))

    @temprole.command(name="remove", description="Remove a temporary role immediately.")
    @app_commands.describe(member="Member", role="Role to remove")
    async def temprole_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        ok = await remove_role(member, role)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM temp_roles WHERE guild_id=? AND user_id=? AND role_id=?",
                (interaction.guild.id, member.id, role.id),
            )
            await db.commit()
        if ok:
            await log_action(interaction.guild.id, member.id, role.id, "temp_removed_manual")
            await interaction.followup.send(embed=_ok("Temp Role Removed", f"Removed {role.mention} from {member.mention}."))
        else:
            await interaction.followup.send(embed=_err("Failed to remove role."))

    # ═════════════════════════════════════════════════════════════════════════
    # /stickyroles
    # ═════════════════════════════════════════════════════════════════════════

    stickyroles = app_commands.Group(
        name="stickyroles",
        description="Configure sticky roles for this server.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @stickyroles.command(name="enable", description="Enable sticky roles — restores self-assigned roles on rejoin.")
    async def stickyroles_enable(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO role_settings(guild_id,sticky_enabled) VALUES(?,1) ON CONFLICT(guild_id) DO UPDATE SET sticky_enabled=1",
                (interaction.guild.id,),
            )
            await db.commit()
        await interaction.followup.send(embed=_ok("Sticky Roles Enabled", "Self-assigned roles will be restored when members rejoin."))

    @stickyroles.command(name="disable", description="Disable sticky roles.")
    async def stickyroles_disable(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO role_settings(guild_id,sticky_enabled) VALUES(?,0) ON CONFLICT(guild_id) DO UPDATE SET sticky_enabled=0",
                (interaction.guild.id,),
            )
            await db.commit()
        await interaction.followup.send(embed=_ok("Sticky Roles Disabled"))

    # ═════════════════════════════════════════════════════════════════════════
    # /rolelog
    # ═════════════════════════════════════════════════════════════════════════

    rolelog = app_commands.Group(
        name="rolelog",
        description="Configure the role-action log channel.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @rolelog.command(name="set", description="Set the channel for role action logs.")
    @app_commands.describe(channel="Log channel")
    async def rolelog_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO role_settings(guild_id,log_channel_id) VALUES(?,?)
                   ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=?""",
                (interaction.guild.id, channel.id, channel.id),
            )
            await db.commit()
        await interaction.followup.send(embed=_ok("Log Channel Set", f"Role actions will be logged to {channel.mention}."))

    @rolelog.command(name="disable", description="Disable role action logging.")
    async def rolelog_disable(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send(embed=_err("Server only."))
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """INSERT INTO role_settings(guild_id,log_channel_id) VALUES(?,NULL)
                   ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=NULL""",
                (interaction.guild.id,),
            )
            await db.commit()
        await interaction.followup.send(embed=_ok("Logging Disabled"))


# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))