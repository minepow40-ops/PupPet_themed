"""
cogs/backup.py — Puppy Backup System™ — PupPet
─────────────────────────────────────────────────────────────────────────────
• /backup create <name>   — Create a named backup of the server structure
• /backup load   <name>   — Restore server from a backup (2-step confirmation)
• /backup delete <name>   — Delete a backup (with confirmation)
• /backup list            — List all backups (paginated)
• /backup info   <name>   — Detailed info about a backup
• /backuppanel            — Post the persistent backup management panel

Background task (every 24 h):
  • Creates AUTO_YYYY_MM_DD backup, deleting any previous auto backup.

Access: ONLY role ID 1510908996339634257 may use any backup feature.

Backed up: roles, categories, text/voice/stage/forum/media channels, emojis,
           stickers, automod rules, welcome screen, scheduled events, guild settings.
NOT backed up: messages, attachments, DMs, audit logs, member data, voice state.

DB  : data/backups.db
Dir : data/backups/{guild_id}/{name}.json
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.migrations import Migration, run_migrations

# ── Logger ────────────────────────────────────────────────────────────────────
logger = logging.getLogger("PupPet.Backup")

# ── Constants ─────────────────────────────────────────────────────────────────
OWNER_ROLE_ID: int = 1551515669026177077

DB_PATH    = Path("data/backups.db")
BACKUP_DIR = Path("data/backups")

PAGE_SIZE  = 8

# ── Colour palette ─────────────────────────────────────────────────────────────
COL_SUCCESS = 0x2ECC71
COL_ERROR   = 0xE74C3C
COL_WARN    = 0xF39C12
COL_INFO    = 0x5865F2

# ── Guilds currently mid-restore ───────────────────────────────────────────────
guild_restore_locks: dict[int, bool] = {}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def has_backup_access(member: discord.Member) -> bool:
    return any(r.id == OWNER_ROLE_ID for r in member.roles)


def _guild_backup_dir(guild_id: int) -> Path:
    d = BACKUP_DIR / str(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_path(guild_id: int, name: str) -> Path:
    return _guild_backup_dir(guild_id) / f"{name}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _footer_embed(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text="🐶 Puppy Backup System")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _error_embed(msg: str) -> discord.Embed:
    return _footer_embed(discord.Embed(description=f"❌  {msg}", colour=COL_ERROR))


def _success_embed(title: str, desc: str = "") -> discord.Embed:
    return _footer_embed(discord.Embed(title=title, description=desc, colour=COL_SUCCESS))


def _warn_embed(title: str, desc: str = "") -> discord.Embed:
    return _footer_embed(discord.Embed(title=title, description=desc, colour=COL_WARN))


def _info_embed(title: str, desc: str = "") -> discord.Embed:
    return _footer_embed(discord.Embed(title=title, description=desc, colour=COL_INFO))


# ══════════════════════════════════════════════════════════════════════════════
# Database
# ══════════════════════════════════════════════════════════════════════════════

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS backup_metadata (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id       INTEGER NOT NULL,
    backup_name    TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    file_size      INTEGER NOT NULL DEFAULT 0,
    role_count     INTEGER NOT NULL DEFAULT 0,
    category_count INTEGER NOT NULL DEFAULT 0,
    channel_count  INTEGER NOT NULL DEFAULT 0,
    emoji_count    INTEGER NOT NULL DEFAULT 0,
    sticker_count  INTEGER NOT NULL DEFAULT 0,
    automod_count  INTEGER NOT NULL DEFAULT 0,
    is_auto        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(guild_id, backup_name)
);
"""

CREATE_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS backup_settings (
    guild_id     INTEGER PRIMARY KEY,
    auto_enabled INTEGER NOT NULL DEFAULT 1
);
"""

# Every schema change for this database gets ONE new entry appended here.
# NEVER edit an existing entry once it has shipped — add a new one instead,
# even for something as small as a typo in a column name.
MIGRATIONS = [
    Migration(1, "initial schema — backup_metadata + backup_settings", (
        CREATE_TABLE_SQL + CREATE_SETTINGS_SQL
    )),
]


async def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Migrations run synchronously — this only happens once at cog load,
    # so blocking briefly here is fine and keeps version-tracking logic
    # in one place instead of duplicating it for aiosqlite.
    con = sqlite3.connect(DB_PATH)
    try:
        run_migrations(con, MIGRATIONS, db_name="backups.db")
    finally:
        con.close()
    logger.info("[DB] backups.db initialised.")


async def _db_get_auto_enabled(guild_id: int) -> bool:
    """Return True if auto-backup is enabled for this guild (default: True)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT auto_enabled FROM backup_settings WHERE guild_id=?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    return bool(row["auto_enabled"]) if row else True


async def _db_set_auto_enabled(guild_id: int, enabled: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO backup_settings (guild_id, auto_enabled)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET auto_enabled = excluded.auto_enabled
            """,
            (guild_id, int(enabled)),
        )
        await db.commit()


async def _db_upsert(
    guild_id: int,
    name: str,
    file_size: int,
    role_count: int,
    category_count: int,
    channel_count: int,
    emoji_count: int = 0,
    sticker_count: int = 0,
    automod_count: int = 0,
    is_auto: bool = False,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO backup_metadata
                (guild_id, backup_name, created_at, file_size,
                 role_count, category_count, channel_count,
                 emoji_count, sticker_count, automod_count, is_auto)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, backup_name) DO UPDATE SET
                created_at     = excluded.created_at,
                file_size      = excluded.file_size,
                role_count     = excluded.role_count,
                category_count = excluded.category_count,
                channel_count  = excluded.channel_count,
                emoji_count    = excluded.emoji_count,
                sticker_count  = excluded.sticker_count,
                automod_count  = excluded.automod_count,
                is_auto        = excluded.is_auto
            """,
            (
                guild_id, name, _now_iso(), file_size,
                role_count, category_count, channel_count,
                emoji_count, sticker_count, automod_count, int(is_auto),
            ),
        )
        await db.commit()


async def _db_delete(guild_id: int, name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM backup_metadata WHERE guild_id=? AND backup_name=?",
            (guild_id, name),
        )
        await db.commit()


async def _db_list(guild_id: int) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM backup_metadata WHERE guild_id=? ORDER BY created_at DESC",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()


async def _db_get(guild_id: int, name: str) -> Optional[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM backup_metadata WHERE guild_id=? AND backup_name=?",
            (guild_id, name),
        ) as cur:
            return await cur.fetchone()


async def _db_find_auto(guild_id: int) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM backup_metadata WHERE guild_id=? AND is_auto=1",
            (guild_id,),
        ) as cur:
            return await cur.fetchall()


# ══════════════════════════════════════════════════════════════════════════════
# Backup creation
# ══════════════════════════════════════════════════════════════════════════════

def _serialize_overwrites(overwrites: dict) -> list[dict]:
    result = []
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "target_id":   target.id,
            "target_type": "role" if isinstance(target, discord.Role) else "member",
            "allow":       allow.value,
            "deny":        deny.value,
        })
    return result


async def _fetch_asset_b64(url: str) -> Optional[str]:
    """Download an asset URL and return base64-encoded string."""
    try:
        import aiohttp, base64
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    raw = await resp.read()
                    ct  = resp.content_type or "image/png"
                    b64 = base64.b64encode(raw).decode()
                    return f"data:{ct};base64,{b64}"
    except Exception:
        pass
    return None


async def create_backup(guild: discord.Guild, name: str) -> dict:
    """
    Capture the full guild structure and return the backup dict.
    """
    data: dict = {
        "version":    3,
        "created_at": _now_iso(),
        "name":       name,
        "guild":      {},
        "roles":      [],
        "categories": [],
        "channels":   [],
        "emojis":     [],
        "stickers":   [],
        "automod":    [],
        "welcome_screen": None,
        "scheduled_events": [],
    }

    # ── Guild Settings ────────────────────────────────────────────────────────
    print("[BACKUP] Started")
    afk_ch_id = guild.afk_channel.id if guild.afk_channel else None
    sys_ch_id = guild.system_channel.id if guild.system_channel else None
    rules_ch_id = guild.rules_channel.id if guild.rules_channel else None
    pub_updates_ch_id = guild.public_updates_channel.id if guild.public_updates_channel else None

    data["guild"] = {
        "name":                      guild.name,
        "description":               guild.description,
        "verification_level":        guild.verification_level.value,
        "explicit_content_filter":   guild.explicit_content_filter.value,
        "default_notifications":     guild.default_notifications.value,
        "preferred_locale":          str(guild.preferred_locale),
        "afk_timeout":               guild.afk_timeout,
        "afk_channel_id":            afk_ch_id,
        "system_channel_id":         sys_ch_id,
        "rules_channel_id":          rules_ch_id,
        "public_updates_channel_id": pub_updates_ch_id,
        "nsfw_level":                guild.nsfw_level.value,
    }

    # ── Roles ─────────────────────────────────────────────────────────────────
    print("[BACKUP] Saving Roles")
    for role in guild.roles:
        if role.is_default():
            continue
        role_data = {
            "id":           role.id,
            "name":         role.name,
            "color":        role.color.value,
            "hoist":        role.hoist,
            "mentionable":  role.mentionable,
            "permissions":  role.permissions.value,
            "position":     role.position,
            "unicode_emoji": role.unicode_emoji,
        }
        # Role icon
        if role.icon:
            role_data["icon_b64"] = await _fetch_asset_b64(str(role.icon.url))
        data["roles"].append(role_data)

    # ── Categories ────────────────────────────────────────────────────────────
    print("[BACKUP] Saving Categories")
    for cat in guild.categories:
        data["categories"].append({
            "id":         cat.id,
            "name":       cat.name,
            "position":   cat.position,
            "overwrites": _serialize_overwrites(cat.overwrites),
        })

    # ── Channels ──────────────────────────────────────────────────────────────
    print("[BACKUP] Saving Channels")
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue

        base = {
            "id":          ch.id,
            "name":        ch.name,
            "position":    ch.position,
            "category_id": ch.category_id,
            "overwrites":  _serialize_overwrites(ch.overwrites),
        }

        if isinstance(ch, discord.TextChannel):
            base.update({
                "type":                       "text",
                "topic":                      ch.topic,
                "nsfw":                       ch.nsfw,
                "slowmode":                   ch.slowmode_delay,
                "default_auto_archive_duration": ch.default_auto_archive_duration,
            })
        elif isinstance(ch, discord.VoiceChannel):
            base.update({
                "type":             "voice",
                "bitrate":          ch.bitrate,
                "user_limit":       ch.user_limit,
                "rtc_region":       str(ch.rtc_region) if ch.rtc_region else None,
                "video_quality_mode": ch.video_quality_mode.value if ch.video_quality_mode else None,
            })
        elif isinstance(ch, discord.StageChannel):
            base.update({
                "type":  "stage",
                "topic": ch.topic,
            })
        elif isinstance(ch, discord.ForumChannel):
            tags = [
                {
                    "name":       t.name,
                    "emoji_name": t.emoji.name if t.emoji else None,
                    "moderated":  t.moderated,
                }
                for t in ch.available_tags
            ]
            base.update({
                "type":     "forum",
                "topic":    ch.topic,
                "nsfw":     ch.nsfw,
                "slowmode": ch.slowmode_delay,
                "tags":     tags,
            })
        elif hasattr(discord, "MediaChannel") and isinstance(ch, discord.MediaChannel):
            base.update({
                "type":     "media",
                "topic":    ch.topic,
                "nsfw":     getattr(ch, "nsfw", False),
                "slowmode": getattr(ch, "slowmode_delay", 0),
            })
        else:
            base["type"] = "other"

        data["channels"].append(base)

    # ── Emojis ────────────────────────────────────────────────────────────────
    print("[BACKUP] Saving Emojis")
    for emoji in guild.emojis:
        emoji_b64 = await _fetch_asset_b64(str(emoji.url))
        data["emojis"].append({
            "name":      emoji.name,
            "animated":  emoji.animated,
            "image_b64": emoji_b64,
        })

    # ── Stickers ──────────────────────────────────────────────────────────────
    print("[BACKUP] Saving Stickers")
    for sticker in guild.stickers:
        sticker_b64 = await _fetch_asset_b64(str(sticker.url))
        data["stickers"].append({
            "name":        sticker.name,
            "description": sticker.description,
            "emoji":       sticker.emoji,
            "image_b64":   sticker_b64,
            "format":      sticker.format.name,
        })

    # ── AutoMod Rules ─────────────────────────────────────────────────────────
    print("[BACKUP] Saving AutoMod")
    try:
        automod_rules = await guild.fetch_automod_rules()
        for rule in automod_rules:
            rule_data = {
                "name":        rule.name,
                "enabled":     rule.enabled,
                "event_type":  rule.event_type.value,
                "trigger_type": rule.trigger.type.value,
            }
            # Trigger metadata
            try:
                tm = rule.trigger
                rule_data["trigger_metadata"] = {
                    "keyword_filter":        list(tm.keyword_filter) if hasattr(tm, "keyword_filter") and tm.keyword_filter else [],
                    "regex_patterns":        list(tm.regex_patterns) if hasattr(tm, "regex_patterns") and tm.regex_patterns else [],
                    "allow_list":            list(tm.allow_list) if hasattr(tm, "allow_list") and tm.allow_list else [],
                    "mention_total_limit":   tm.mention_total_limit if hasattr(tm, "mention_total_limit") else None,
                    "presets":               [p.value for p in tm.presets] if hasattr(tm, "presets") and tm.presets else [],
                }
            except Exception:
                rule_data["trigger_metadata"] = {}
            # Actions
            actions_list = []
            for action in rule.actions:
                a = {"type": action.type.value}
                if action.channel_id:
                    a["channel_id"] = action.channel_id
                if action.duration:
                    a["duration_seconds"] = int(action.duration.total_seconds())
                if action.custom_message:
                    a["custom_message"] = action.custom_message
                actions_list.append(a)
            rule_data["actions"] = actions_list
            rule_data["exempt_role_ids"]    = [r.id for r in rule.exempt_roles]
            rule_data["exempt_channel_ids"] = [c.id for c in rule.exempt_channels]
            data["automod"].append(rule_data)
    except Exception:
        logger.warning("[BACKUP] Could not fetch automod rules: %s", traceback.format_exc(limit=2))

    # ── Welcome Screen ────────────────────────────────────────────────────────
    try:
        if "COMMUNITY" in [str(f) for f in guild.features]:
            ws = await guild.fetch_welcome_screen()
            ws_channels = [
                {
                    "channel_id":  wc.channel.id if wc.channel else None,
                    "description": wc.description,
                    "emoji_name":  wc.emoji.name if wc.emoji else None,
                    "emoji_id":    wc.emoji.id if wc.emoji and wc.emoji.id else None,
                }
                for wc in ws.welcome_channels
            ]
            data["welcome_screen"] = {
                "description": ws.description,
                "channels":    ws_channels,
            }
    except Exception:
        logger.debug("[BACKUP] Welcome screen not available: %s", traceback.format_exc(limit=1))

    # ── Scheduled Events ──────────────────────────────────────────────────────
    try:
        for ev in guild.scheduled_events:
            ev_data = {
                "name":        ev.name,
                "description": ev.description,
                "start_time":  ev.start_time.isoformat() if ev.start_time else None,
                "end_time":    ev.end_time.isoformat() if ev.end_time else None,
                "location":    ev.location.value if ev.location else None,
                "entity_type": ev.entity_type.value,
            }
            data["scheduled_events"].append(ev_data)
    except Exception:
        logger.debug("[BACKUP] Scheduled events error: %s", traceback.format_exc(limit=1))

    print("[BACKUP] Completed")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# Restore logic
# ══════════════════════════════════════════════════════════════════════════════

async def _rebuild_overwrites(
    guild: discord.Guild,
    raw: list[dict],
    id_map: dict[int, int],
) -> dict:
    result: dict = {}
    for entry in raw:
        allow = discord.Permissions(entry["allow"])
        deny  = discord.Permissions(entry["deny"])
        ow    = discord.PermissionOverwrite.from_pair(allow, deny)
        if entry["target_type"] == "role":
            new_id = id_map.get(entry["target_id"])
            if new_id:
                role = guild.get_role(new_id)
                if role:
                    result[role] = ow
            elif entry["target_id"] == guild.default_role.id:
                result[guild.default_role] = ow
        else:
            member = guild.get_member(entry["target_id"])
            if member:
                result[member] = ow
    return result


async def restore_backup(guild: discord.Guild, data: dict) -> dict:
    """
    Dangerous: deletes current structure, recreates from backup data.
    Restore order: roles → categories → channels → emojis → stickers →
                   automod → welcome screen → scheduled events → guild settings
    """
    t_start = time.monotonic()
    bot_member  = guild.me
    bot_top     = bot_member.top_role
    protected_ids = {OWNER_ROLE_ID, guild.default_role.id, bot_top.id}

    print("[RESTORE] Started")

    # ─── Delete channels ──────────────────────────────────────────────────────
    print("[RESTORE] Deleting Channels")
    for ch in list(guild.channels):
        try:
            await ch.delete(reason="PupPet restore: clearing structure")
        except Exception:
            logger.warning("[RESTORE] Could not delete channel %s", ch.name)

    # ─── Delete roles ─────────────────────────────────────────────────────────
    print("[RESTORE] Deleting Roles")
    for role in list(guild.roles):
        if role.id in protected_ids:
            continue
        if role >= bot_top:
            continue
        try:
            await role.delete(reason="PupPet restore: clearing roles")
        except Exception:
            logger.warning("[RESTORE] Could not delete role %s", role.name)

    # ─── Create roles ─────────────────────────────────────────────────────────
    print("[RESTORE] Creating Roles")
    old_to_new_role: dict[int, int] = {}
    sorted_roles = sorted(data.get("roles", []), key=lambda r: r["position"])

    for r in sorted_roles:
        if r["name"] == "@everyone":
            old_to_new_role[r["id"]] = guild.default_role.id
            continue
        try:
            kwargs: dict = {
                "name":        r["name"],
                "color":       discord.Color(r["color"]),
                "hoist":       r["hoist"],
                "mentionable": r["mentionable"],
                "permissions": discord.Permissions(r["permissions"]),
                "reason":      "PupPet restore",
            }
            if r.get("unicode_emoji"):
                kwargs["unicode_emoji"] = r["unicode_emoji"]
            new_role = await guild.create_role(**kwargs)
            old_to_new_role[r["id"]] = new_role.id
        except Exception:
            logger.error("[RESTORE] Failed to create role %s: %s", r["name"], traceback.format_exc())

    # Restore role positions
    try:
        position_map = {}
        for r in sorted_roles:
            new_id = old_to_new_role.get(r["id"])
            if not new_id:
                continue
            role_obj = guild.get_role(new_id)
            if role_obj and role_obj < bot_top:
                position_map[role_obj] = r["position"]
        if position_map:
            await guild.edit_role_positions(position_map, reason="PupPet restore: positions")
    except Exception:
        logger.error("[RESTORE] Could not restore role positions: %s", traceback.format_exc())

    # ─── Create categories ────────────────────────────────────────────────────
    print("[RESTORE] Creating Categories")
    old_to_new_cat: dict[int, int] = {}
    for cat in sorted(data.get("categories", []), key=lambda c: c["position"]):
        try:
            overwrites = await _rebuild_overwrites(guild, cat.get("overwrites", []), old_to_new_role)
            new_cat = await guild.create_category(
                name=cat["name"],
                overwrites=overwrites,
                position=cat["position"],
                reason="PupPet restore",
            )
            old_to_new_cat[cat["id"]] = new_cat.id
        except Exception:
            logger.error("[RESTORE] Failed to create category %s: %s", cat["name"], traceback.format_exc())

    # ─── Create channels ──────────────────────────────────────────────────────
    print("[RESTORE] Creating Channels")
    channels_restored = 0
    for ch in sorted(data.get("channels", []), key=lambda c: c["position"]):
        try:
            overwrites = await _rebuild_overwrites(guild, ch.get("overwrites", []), old_to_new_role)
            category_obj = None
            if ch.get("category_id"):
                new_cat_id = old_to_new_cat.get(ch["category_id"])
                if new_cat_id:
                    category_obj = guild.get_channel(new_cat_id)

            ch_type = ch.get("type", "text")

            if ch_type == "text":
                await guild.create_text_channel(
                    name=ch["name"],
                    category=category_obj,
                    position=ch["position"],
                    topic=ch.get("topic"),
                    nsfw=ch.get("nsfw", False),
                    slowmode_delay=ch.get("slowmode", 0),
                    overwrites=overwrites,
                    reason="PupPet restore",
                )
            elif ch_type == "voice":
                region = None
                if ch.get("rtc_region"):
                    try:
                        region = discord.VoiceRegion(ch["rtc_region"])
                    except Exception:
                        pass
                await guild.create_voice_channel(
                    name=ch["name"],
                    category=category_obj,
                    position=ch["position"],
                    bitrate=min(ch.get("bitrate", 64000), guild.bitrate_limit),
                    user_limit=ch.get("user_limit", 0),
                    rtc_region=region,
                    overwrites=overwrites,
                    reason="PupPet restore",
                )
            elif ch_type == "stage":
                await guild.create_stage_channel(
                    name=ch["name"],
                    category=category_obj,
                    position=ch["position"],
                    topic=ch.get("topic"),
                    overwrites=overwrites,
                    reason="PupPet restore",
                )
            elif ch_type == "forum":
                tags = []
                for t in ch.get("tags", []):
                    try:
                        em = None
                        if t.get("emoji_name"):
                            em = discord.PartialEmoji(name=t["emoji_name"])
                        tags.append(discord.ForumTag(name=t["name"], emoji=em, moderated=t.get("moderated", False)))
                    except Exception:
                        pass
                await guild.create_forum(
                    name=ch["name"],
                    category=category_obj,
                    position=ch["position"],
                    topic=ch.get("topic"),
                    nsfw=ch.get("nsfw", False),
                    slowmode_delay=ch.get("slowmode", 0),
                    available_tags=tags,
                    overwrites=overwrites,
                    reason="PupPet restore",
                )
            elif ch_type == "media" and hasattr(guild, "create_media_channel"):
                await guild.create_media_channel(
                    name=ch["name"],
                    category=category_obj,
                    position=ch["position"],
                    topic=ch.get("topic"),
                    nsfw=ch.get("nsfw", False),
                    slowmode_delay=ch.get("slowmode", 0),
                    overwrites=overwrites,
                    reason="PupPet restore",
                )
            else:
                continue

            channels_restored += 1
        except Exception:
            logger.error("[RESTORE] Failed to create channel %s: %s", ch["name"], traceback.format_exc())

    # ─── Emojis ───────────────────────────────────────────────────────────────
    print("[RESTORE] Creating Emojis")
    emojis_restored = 0
    for em in data.get("emojis", []):
        try:
            if not em.get("image_b64"):
                continue
            import base64
            header, b64data = em["image_b64"].split(",", 1)
            raw = base64.b64decode(b64data)
            await guild.create_custom_emoji(
                name=em["name"],
                image=raw,
                reason="PupPet restore",
            )
            emojis_restored += 1
        except Exception:
            logger.error("[RESTORE] Failed to restore emoji %s: %s", em.get("name"), traceback.format_exc())

    # ─── Stickers ─────────────────────────────────────────────────────────────
    print("[RESTORE] Creating Stickers")
    stickers_restored = 0
    for st in data.get("stickers", []):
        try:
            if not st.get("image_b64"):
                continue
            import base64
            header, b64data = st["image_b64"].split(",", 1)
            raw = base64.b64decode(b64data)
            ext = "png" if "png" in header else "apng"
            fp = discord.File(io.BytesIO(raw), filename=f"{st['name']}.{ext}")
            await guild.create_sticker(
                name=st["name"],
                description=st.get("description") or st["name"],
                emoji=st.get("emoji") or "🐶",
                file=fp,
                reason="PupPet restore",
            )
            stickers_restored += 1
        except Exception:
            logger.error("[RESTORE] Failed to restore sticker %s: %s", st.get("name"), traceback.format_exc())

    # ─── AutoMod ──────────────────────────────────────────────────────────────
    print("[RESTORE] Creating AutoMod")
    automod_restored = 0
    for rule in data.get("automod", []):
        try:
            # Build trigger
            tt = discord.AutoModRuleTriggerType(rule["trigger_type"])
            tm_raw = rule.get("trigger_metadata", {})
            trigger = discord.AutoModTrigger(
                type=tt,
                keyword_filter=tm_raw.get("keyword_filter") or None,
                regex_patterns=tm_raw.get("regex_patterns") or None,
                allow_list=tm_raw.get("allow_list") or None,
                mention_total_limit=tm_raw.get("mention_total_limit"),
            )
            # Build actions
            actions = []
            for a in rule.get("actions", []):
                at = discord.AutoModRuleActionType(a["type"])
                duration = None
                if a.get("duration_seconds"):
                    from datetime import timedelta
                    duration = timedelta(seconds=a["duration_seconds"])
                actions.append(discord.AutoModRuleAction(
                    type=at,
                    channel_id=a.get("channel_id"),
                    duration=duration,
                    custom_message=a.get("custom_message"),
                ))
            await guild.create_automod_rule(
                name=rule["name"],
                event_type=discord.AutoModRuleEventType(rule["event_type"]),
                trigger=trigger,
                actions=actions,
                enabled=rule.get("enabled", True),
                reason="PupPet restore",
            )
            automod_restored += 1
        except Exception:
            logger.error("[RESTORE] Failed to restore automod rule %s: %s", rule.get("name"), traceback.format_exc())

    # ─── Welcome Screen ───────────────────────────────────────────────────────
    ws_data = data.get("welcome_screen")
    if ws_data:
        try:
            wc_objs = []
            for wc in ws_data.get("channels", []):
                ch_obj = guild.get_channel(wc.get("channel_id"))
                if ch_obj:
                    em = None
                    if wc.get("emoji_name"):
                        em = discord.PartialEmoji(name=wc["emoji_name"], id=wc.get("emoji_id"))
                    wc_objs.append(discord.WelcomeScreenChannel(
                        channel=ch_obj,
                        description=wc.get("description", ""),
                        emoji=em,
                    ))
            await guild.edit_welcome_screen(
                description=ws_data.get("description"),
                welcome_channels=wc_objs,
                enabled=True,
            )
        except Exception:
            logger.error("[RESTORE] Failed to restore welcome screen: %s", traceback.format_exc())

    # ─── Scheduled Events ─────────────────────────────────────────────────────
    print("[RESTORE] Creating Scheduled Events (skipped — past events ignored)")
    for ev in data.get("scheduled_events", []):
        try:
            start = datetime.fromisoformat(ev["start_time"]) if ev.get("start_time") else None
            end   = datetime.fromisoformat(ev["end_time"])   if ev.get("end_time")   else None
            if not start or start < datetime.now(timezone.utc):
                continue
            et = discord.EntityType(ev.get("entity_type", 3))
            if et == discord.EntityType.external:
                await guild.create_scheduled_event(
                    name=ev["name"],
                    description=ev.get("description") or "",
                    start_time=start,
                    end_time=end,
                    entity_type=et,
                    location=ev.get("location") or "TBD",
                    reason="PupPet restore",
                )
        except Exception:
            logger.error("[RESTORE] Failed to restore event %s: %s", ev.get("name"), traceback.format_exc())

    # ─── Guild Settings ───────────────────────────────────────────────────────
    print("[RESTORE] Applying Guild Settings")
    try:
        g = data.get("guild", {})
        edit_kwargs: dict = {}
        if g.get("name"):
            edit_kwargs["name"] = g["name"]
        if g.get("description") is not None:
            edit_kwargs["description"] = g["description"]
        if g.get("verification_level") is not None:
            edit_kwargs["verification_level"] = discord.VerificationLevel(g["verification_level"])
        if g.get("explicit_content_filter") is not None:
            edit_kwargs["explicit_content_filter"] = discord.ContentFilter(g["explicit_content_filter"])
        if g.get("default_notifications") is not None:
            edit_kwargs["default_notifications"] = discord.NotificationLevel(g["default_notifications"])
        if g.get("afk_timeout") is not None:
            edit_kwargs["afk_timeout"] = g["afk_timeout"]
        if g.get("preferred_locale"):
            try:
                edit_kwargs["preferred_locale"] = discord.Locale(g["preferred_locale"])
            except Exception:
                pass
        await guild.edit(reason="PupPet restore: guild settings", **edit_kwargs)
    except Exception:
        logger.error("[RESTORE] Failed to apply guild settings: %s", traceback.format_exc())

    duration = time.monotonic() - t_start
    print("[RESTORE] Finished")
    logger.info(
        "[RESTORE] Complete — guild: %s | roles: %d | cats: %d | channels: %d | emojis: %d | stickers: %d | automod: %d | %.2fs",
        guild.name, len(old_to_new_role), len(old_to_new_cat), channels_restored,
        emojis_restored, stickers_restored, automod_restored, duration,
    )

    return {
        "roles_restored":      len(old_to_new_role),
        "categories_restored": len(old_to_new_cat),
        "channels_restored":   channels_restored,
        "emojis_restored":     emojis_restored,
        "stickers_restored":   stickers_restored,
        "automod_restored":    automod_restored,
        "duration":            round(duration, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Modals
# ══════════════════════════════════════════════════════════════════════════════

class CreateBackupModal(discord.ui.Modal, title="💾 Create Backup"):
    backup_name = discord.ui.TextInput(
        label="Backup Name",
        placeholder="e.g. before-restructure",
        min_length=1,
        max_length=64,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.backup_name.value.strip().replace(" ", "_")
        cog: Optional[BackupSystem] = interaction.client.get_cog("BackupSystem")  # type: ignore
        if cog:
            await cog._do_create_backup(interaction, name)
        else:
            await interaction.response.send_message(
                embed=_error_embed("Backup cog not available."), ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("[ERROR] CreateBackupModal error", exc_info=error)
        await interaction.response.send_message(
            embed=_error_embed("An unexpected error occurred."), ephemeral=True
        )


class RestoreConfirmModal(discord.ui.Modal, title="⚠️ Confirm Restore"):
    """Step-2 modal — user must type RESTORE exactly."""
    confirm_text = discord.ui.TextInput(
        label='Type "RESTORE" to confirm',
        placeholder="RESTORE",
        min_length=7,
        max_length=7,
        required=True,
    )

    def __init__(self, guild_id: int, backup_name: str) -> None:
        super().__init__()
        self.guild_id    = guild_id
        self.backup_name = backup_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.confirm_text.value.strip() != "RESTORE":
            await interaction.response.send_message(
                embed=_error_embed("Incorrect confirmation text. Restore cancelled."),
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if not guild:
            await interaction.response.send_message(embed=_error_embed("Guild not found."), ephemeral=True)
            return

        if guild_restore_locks.get(guild.id):
            await interaction.response.send_message(
                embed=_error_embed("A restore is already running for this server. Please wait."),
                ephemeral=True,
            )
            return

        path = _backup_path(self.guild_id, self.backup_name)
        if not path.exists():
            await interaction.response.send_message(
                embed=_error_embed(f"Backup file **{self.backup_name}** not found."), ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=_footer_embed(discord.Embed(
                title="⚙️  Restoring...",
                description="Restore in progress. Do **NOT** modify the server until complete.",
                colour=COL_WARN,
            )),
            ephemeral=True,
        )

        guild_restore_locks[guild.id] = True
        try:
            with open(path, "r", encoding="utf-8") as f:
                bdata = json.load(f)

            stats = await restore_backup(guild, bdata)

            embed = discord.Embed(
                title="✅  Restore Complete",
                description=f"Server restored from **{self.backup_name}**.",
                colour=COL_SUCCESS,
            )
            embed.add_field(name="🎭 Roles",      value=str(stats["roles_restored"]),      inline=True)
            embed.add_field(name="📁 Categories", value=str(stats["categories_restored"]), inline=True)
            embed.add_field(name="💬 Channels",   value=str(stats["channels_restored"]),   inline=True)
            embed.add_field(name="😀 Emojis",     value=str(stats["emojis_restored"]),     inline=True)
            embed.add_field(name="🎨 Stickers",   value=str(stats["stickers_restored"]),   inline=True)
            embed.add_field(name="🛡️ AutoMod",    value=str(stats["automod_restored"]),    inline=True)
            embed.add_field(name="⏱️ Duration",   value=f"{stats['duration']}s",           inline=True)
            embed.set_footer(text="🐶 Puppy Backup System")
            embed.timestamp = discord.utils.utcnow()

            try:
                await interaction.edit_original_response(embed=embed)
            except Exception:
                pass

        except Exception:
            logger.exception("[ERROR] Restore failed for guild %d backup %s", guild.id, self.backup_name)
            try:
                await interaction.edit_original_response(
                    embed=_error_embed("Restore failed. Check bot logs for details.")
                )
            except Exception:
                pass
        finally:
            guild_restore_locks[guild.id] = False

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("[ERROR] RestoreConfirmModal error", exc_info=error)


# ══════════════════════════════════════════════════════════════════════════════
# Step-1 Restore Confirm View
# ══════════════════════════════════════════════════════════════════════════════

class RestoreStep1View(discord.ui.View):
    def __init__(self, guild_id: int, backup_name: str, requester_id: int) -> None:
        super().__init__(timeout=120)
        self.guild_id     = guild_id
        self.backup_name  = backup_name
        self.requester_id = requester_id

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=_error_embed("Only the command requester can confirm this."),
                ephemeral=True,
            )
            return False
        if not isinstance(interaction.user, discord.Member) or not has_backup_access(interaction.user):
            await interaction.response.send_message(
                embed=_error_embed("You do not have permission to use the Backup System."),
                ephemeral=True,
            )
            logger.warning("[SECURITY] Unauthorized restore step-1 by %s (%d)", interaction.user, interaction.user.id)
            return False
        return True

    @discord.ui.button(label="⚠️ Confirm Restore", style=discord.ButtonStyle.danger, custom_id="restore:step1_confirm")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction):
            return
        await interaction.response.send_modal(
            RestoreConfirmModal(self.guild_id, self.backup_name)
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, custom_id="restore:step1_cancel")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=_error_embed("Only the command requester can cancel."), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=discord.Embed(description="❌  Restore cancelled.", colour=COL_INFO),
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# Delete Confirm View
# ══════════════════════════════════════════════════════════════════════════════

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, guild_id: int, backup_name: str, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.guild_id     = guild_id
        self.backup_name  = backup_name
        self.requester_id = requester_id

    async def _check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=_error_embed("Only the command requester can confirm this."),
                ephemeral=True,
            )
            return False
        if not isinstance(interaction.user, discord.Member) or not has_backup_access(interaction.user):
            await interaction.response.send_message(
                embed=_error_embed("You do not have permission to use the Backup System."),
                ephemeral=True,
            )
            logger.warning("[SECURITY] Unauthorized confirm-delete by %s (%d)", interaction.user, interaction.user.id)
            return False
        return True

    @discord.ui.button(label="✅ Confirm Delete", style=discord.ButtonStyle.danger, custom_id="backup:confirm_delete")
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._check(interaction):
            return
        path = _backup_path(self.guild_id, self.backup_name)
        try:
            if path.exists():
                path.unlink()
            await _db_delete(self.guild_id, self.backup_name)
            logger.info("[BACKUP] Deleted — guild: %d | name: %s | user: %s (%d)",
                        self.guild_id, self.backup_name, interaction.user, interaction.user.id)
            await interaction.response.edit_message(
                embed=_success_embed("🗑️ Backup Deleted", f"Backup **{self.backup_name}** has been deleted."),
                view=None,
            )
        except Exception:
            logger.exception("[ERROR] Failed to delete backup %s", self.backup_name)
            await interaction.response.edit_message(
                embed=_error_embed("Failed to delete backup file."), view=None
            )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary, custom_id="backup:cancel_delete")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=_error_embed("Only the command requester can cancel."), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=discord.Embed(description="❌  Deletion cancelled.", colour=COL_INFO),
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# Panel Dropdowns
# ══════════════════════════════════════════════════════════════════════════════

class LoadBackupSelect(discord.ui.Select):
    def __init__(self, rows: list) -> None:
        options = [
            discord.SelectOption(
                label=r["backup_name"][:100],
                description=f"Created: {r['created_at'][:10]}",
                value=r["backup_name"],
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="Select a backup to restore...", options=options, custom_id="backup:load_select")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_backup_access(interaction.user):
            await interaction.response.send_message(
                embed=_error_embed("You do not have permission to use the Backup System."), ephemeral=True
            )
            logger.warning("[SECURITY] Unauthorized load-select by %s (%d)", interaction.user, interaction.user.id)
            return

        name  = self.values[0]
        guild = interaction.guild
        embed = _warn_embed(
            "⚠️  WARNING — Server Restore",
            (
                f"You are about to restore **{guild.name}** from backup **`{name}`**.\n\n"
                "This will **completely replace** the current server structure:\n"
                "• All roles (except @everyone, bot role, Owner role) will be deleted\n"
                "• All categories and channels will be deleted and recreated\n"
                "• Emojis, stickers, automod rules will be recreated\n\n"
                "**This operation cannot be undone. Proceed only if you are certain.**"
            ),
        )
        view = RestoreStep1View(guild.id, name, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class DeleteBackupSelect(discord.ui.Select):
    def __init__(self, rows: list) -> None:
        options = [
            discord.SelectOption(
                label=r["backup_name"][:100],
                description=f"Created: {r['created_at'][:10]}",
                value=r["backup_name"],
            )
            for r in rows[:25]
        ]
        super().__init__(placeholder="Select a backup to delete...", options=options, custom_id="backup:delete_select")

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_backup_access(interaction.user):
            await interaction.response.send_message(
                embed=_error_embed("You do not have permission to use the Backup System."), ephemeral=True
            )
            logger.warning("[SECURITY] Unauthorized delete-select by %s (%d)", interaction.user, interaction.user.id)
            return

        name = self.values[0]
        embed = _warn_embed(
            "🗑️  Delete Backup",
            f"Are you sure you want to permanently delete backup **`{name}`**?\n\nThis cannot be undone.",
        )
        view = ConfirmDeleteView(interaction.guild_id, name, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# Persistent Panel View
# ══════════════════════════════════════════════════════════════════════════════

class BackupPanelView(discord.ui.View):
    """
    Persistent 5-button panel:
      Row 0: 💾 Create Backup | 📥 Load Backup
      Row 1: 🟢 Auto Backup ON | 🔴 Auto Backup OFF | 📋 Backup List
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _deny(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not has_backup_access(interaction.user):
            await interaction.response.send_message(
                embed=_error_embed("You do not have permission to use the Backup System."),
                ephemeral=True,
            )
            logger.warning("[SECURITY] Unauthorized panel access by %s (%d)", interaction.user, interaction.user.id)
            return True
        return False

    # ── Row 0 ─────────────────────────────────────────────────────────────────

    @discord.ui.button(label="💾 Create Backup", style=discord.ButtonStyle.success,
                       custom_id="backuppanel:create", row=0)
    async def btn_create(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._deny(interaction):
            return
        await interaction.response.send_modal(CreateBackupModal())

    @discord.ui.button(label="📥 Load Backup", style=discord.ButtonStyle.primary,
                       custom_id="backuppanel:load", row=0)
    async def btn_load(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._deny(interaction):
            return
        rows = await _db_list(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                embed=_error_embed("No backups found for this server."), ephemeral=True
            )
            return
        view = discord.ui.View(timeout=120)
        view.add_item(LoadBackupSelect(rows))
        await interaction.response.send_message(
            embed=_info_embed("📥  Load Backup", "Select a backup from the dropdown below to restore it."),
            view=view, ephemeral=True,
        )

    # ── Row 1 ─────────────────────────────────────────────────────────────────

    @discord.ui.button(label="🟢 Auto Backup ON", style=discord.ButtonStyle.success,
                       custom_id="backuppanel:auto_on", row=1)
    async def btn_auto_on(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._deny(interaction):
            return
        guild_id = interaction.guild_id
        already  = await _db_get_auto_enabled(guild_id)
        if already:
            await interaction.response.send_message(
                embed=_info_embed("🟢 Auto Backup", "Auto backup is **already enabled** for this server."),
                ephemeral=True,
            )
            return
        await _db_set_auto_enabled(guild_id, True)
        logger.info("[AUTO-BACKUP] Enabled by %s (%d) for guild %d", interaction.user, interaction.user.id, guild_id)
        embed = _success_embed(
            "🟢 Auto Backup Enabled",
            "Automatic daily backups are now **ON**.\nA backup named `AUTO_YYYY_MM_DD` will be created every 24 hours.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔴 Auto Backup OFF", style=discord.ButtonStyle.danger,
                       custom_id="backuppanel:auto_off", row=1)
    async def btn_auto_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._deny(interaction):
            return
        guild_id = interaction.guild_id
        already  = await _db_get_auto_enabled(guild_id)
        if not already:
            await interaction.response.send_message(
                embed=_info_embed("🔴 Auto Backup", "Auto backup is **already disabled** for this server."),
                ephemeral=True,
            )
            return
        await _db_set_auto_enabled(guild_id, False)
        logger.info("[AUTO-BACKUP] Disabled by %s (%d) for guild %d", interaction.user, interaction.user.id, guild_id)
        embed = _warn_embed(
            "🔴 Auto Backup Disabled",
            "Automatic daily backups are now **OFF**.\nNo new auto backups will be created until you turn this back on.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📋 Backup List", style=discord.ButtonStyle.secondary,
                       custom_id="backuppanel:list", row=1)
    async def btn_list(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._deny(interaction):
            return
        cog: Optional[BackupSystem] = interaction.client.get_cog("BackupSystem")  # type: ignore
        if cog:
            await cog._do_list(interaction)
        else:
            await interaction.response.send_message(embed=_error_embed("Backup cog unavailable."), ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# List Paginator
# ══════════════════════════════════════════════════════════════════════════════

class _BackupListPaginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], original_interaction: discord.Interaction) -> None:
        super().__init__(timeout=120)
        self.pages   = pages
        self.current = 0
        self._orig   = original_interaction
        self._update_btns()

    def _update_btns(self) -> None:
        self.prev_btn.disabled = self.current == 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current -= 1
        self._update_btns()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current += 1
        self._update_btns()
        await interaction.response.edit_message(embed=self.pages[self.current], view=self)

    async def on_timeout(self) -> None:
        for c in self.children:
            if isinstance(c, discord.ui.Button):
                c.disabled = True
        try:
            await self._orig.edit_original_response(view=self)
        except discord.HTTPException:
            pass


def _build_list_pages(rows: list, guild_name: str) -> list[discord.Embed]:
    pages: list[discord.Embed] = []
    chunks = [rows[i:i + PAGE_SIZE] for i in range(0, len(rows), PAGE_SIZE)]
    total_pages = len(chunks)

    for idx, chunk in enumerate(chunks, 1):
        embed = discord.Embed(
            title=f"💾  Backups — {guild_name}",
            description=f"Total: **{len(rows)}** backup(s)",
            colour=COL_INFO,
        )
        for r in chunk:
            auto_tag = " *(auto)*" if r["is_auto"] else ""
            embed.add_field(
                name=f"📦  {r['backup_name']}{auto_tag}",
                value=(
                    f"📅 `{r['created_at'][:19].replace('T', ' ')} UTC`\n"
                    f"🎭 Roles: {r['role_count']}  📁 Cats: {r['category_count']}  💬 Chs: {r['channel_count']}\n"
                    f"😀 Emojis: {r['emoji_count']}  🎨 Stickers: {r['sticker_count']}  🛡️ AutoMod: {r['automod_count']}\n"
                    f"📄 Size: {r['file_size'] // 1024} KB"
                ),
                inline=False,
            )
        embed.set_footer(text=f"Page {idx}/{total_pages} • 🐶 Puppy Backup System")
        embed.timestamp = discord.utils.utcnow()
        pages.append(embed)

    return pages


# ══════════════════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════════════════

class BackupSystem(commands.Cog):
    """
    Full server backup/restore system for PupPet.
    Restricted to users holding role ID 1510908996339634257.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        await _init_db()
        self.bot.add_view(BackupPanelView())
        self.auto_backup_task.start()
        logger.info("[INFO] Puppy Backup System loaded.")

    async def cog_unload(self) -> None:
        self.auto_backup_task.cancel()
        logger.info("[INFO] Puppy Backup System unloaded.")

    # ── Shared internal helpers ───────────────────────────────────────────────

    async def _do_create_backup(
        self,
        interaction: discord.Interaction,
        name: str,
        *,
        is_auto: bool = False,
        defer_done: bool = False,
    ) -> None:
        guild = interaction.guild if interaction else None
        if not guild:
            return

        logger.info(
            "[BACKUP] Create requested — guild: %s | guild_id: %d | user: %s | backup: %s",
            guild.name, guild.id,
            str(interaction.user) if interaction else "AUTO",
            name,
        )

        if not defer_done and not is_auto:
            await interaction.response.defer(ephemeral=True)

        t = time.monotonic()
        try:
            data = await create_backup(guild, name)
        except Exception:
            logger.exception("[ERROR] Backup capture failed for guild %d", guild.id)
            if not is_auto:
                await interaction.followup.send(
                    embed=_error_embed("Failed to capture server data. Check bot logs."), ephemeral=True
                )
            return

        path = _backup_path(guild.id, name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            logger.exception("[ERROR] Failed to write backup file %s", path)
            if not is_auto:
                await interaction.followup.send(
                    embed=_error_embed("Failed to save backup file."), ephemeral=True
                )
            return

        fsize      = path.stat().st_size
        r_count    = len(data["roles"])
        cat_count  = len(data["categories"])
        ch_count   = len(data["channels"])
        em_count   = len(data["emojis"])
        st_count   = len(data["stickers"])
        am_count   = len(data["automod"])
        duration   = time.monotonic() - t

        await _db_upsert(
            guild.id, name, fsize, r_count, cat_count, ch_count,
            em_count, st_count, am_count, is_auto,
        )

        logger.info(
            "[BACKUP] Completed — guild: %s | name: %s | roles: %d | cats: %d | chs: %d | emojis: %d | stickers: %d | automod: %d | %.2fs",
            guild.name, name, r_count, cat_count, ch_count, em_count, st_count, am_count, duration,
        )

        if not is_auto:
            embed = _success_embed("✅  Backup Created", f"Backup **`{name}`** created successfully.")
            embed.add_field(name="🎭 Roles",       value=str(r_count),          inline=True)
            embed.add_field(name="📁 Categories",  value=str(cat_count),        inline=True)
            embed.add_field(name="💬 Channels",    value=str(ch_count),         inline=True)
            embed.add_field(name="😀 Emojis",      value=str(em_count),         inline=True)
            embed.add_field(name="🎨 Stickers",    value=str(st_count),         inline=True)
            embed.add_field(name="🛡️ AutoMod",     value=str(am_count),         inline=True)
            embed.add_field(name="📄 File Size",   value=f"{fsize // 1024} KB", inline=True)
            embed.add_field(name="⏱️ Duration",    value=f"{duration:.2f}s",    inline=True)
            await interaction.followup.send(embed=embed, ephemeral=True)

    async def _do_list(self, interaction: discord.Interaction) -> None:
        rows = await _db_list(interaction.guild_id)
        if not rows:
            await interaction.response.send_message(
                embed=_error_embed("No backups found for this server."), ephemeral=True
            )
            return
        pages = _build_list_pages(rows, interaction.guild.name)
        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
        else:
            view = _BackupListPaginator(pages, interaction)
            await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)

    # ── Auto-backup task ──────────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def auto_backup_task(self) -> None:
        print("[AUTO-BACKUP] Started")
        today_str = datetime.now(timezone.utc).strftime("AUTO_%Y_%m_%d")

        for guild in self.bot.guilds:
            try:
                # Skip if auto-backup is disabled for this guild
                if not await _db_get_auto_enabled(guild.id):
                    logger.info("[AUTO-BACKUP] Skipped guild %s (%d) — auto backup is OFF", guild.name, guild.id)
                    continue

                logger.info("[AUTO-BACKUP] Processing guild: %s (%d)", guild.name, guild.id)

                # Remove all previous auto backups
                existing_autos = await _db_find_auto(guild.id)
                for ea in existing_autos:
                    old_path = _backup_path(guild.id, ea["backup_name"])
                    if old_path.exists():
                        old_path.unlink()
                    await _db_delete(guild.id, ea["backup_name"])
                    print(f"[AUTO-BACKUP] Removed Previous Auto Backup: {ea['backup_name']}")

                # Create new auto backup
                data = await create_backup(guild, today_str)
                bk_path = _backup_path(guild.id, today_str)
                with open(bk_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)

                fsize    = bk_path.stat().st_size
                em_count = len(data["emojis"])
                st_count = len(data["stickers"])
                am_count = len(data["automod"])

                await _db_upsert(
                    guild.id, today_str, fsize,
                    len(data["roles"]),
                    len(data["categories"]),
                    len(data["channels"]),
                    em_count, st_count, am_count,
                    is_auto=True,
                )
                print(f"[AUTO-BACKUP] Created {today_str}")
                logger.info("[AUTO-BACKUP] Completed for guild: %s — %s", guild.name, today_str)

            except Exception:
                logger.exception("[ERROR] Auto-backup failed for guild %d", guild.id)

        print("[AUTO-BACKUP] Finished")

    @auto_backup_task.before_loop
    async def _before_auto_backup(self) -> None:
        await self.bot.wait_until_ready()
        logger.info("[INFO] Auto-backup task started.")

    # ── Access check shorthand ────────────────────────────────────────────────

    async def _require_access(self, interaction: discord.Interaction, action: str) -> bool:
        if not isinstance(interaction.user, discord.Member) or not has_backup_access(interaction.user):
            await interaction.response.send_message(
                embed=_error_embed("You do not have permission to use the Backup System."),
                ephemeral=True,
            )
            logger.warning(
                "[SECURITY] Unauthorized backup access by %d (action: %s)",
                interaction.user.id, action,
            )
            return False
        return True

    # ── Slash command group ───────────────────────────────────────────────────

    backup_group = app_commands.Group(
        name="backup",
        description="🐶 Server backup management (Owner only)",
    )

    # /backup create
    @backup_group.command(name="create", description="Create a full backup of the current server structure.")
    @app_commands.describe(name="Name for this backup (no spaces)")
    async def backup_create(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_access(interaction, "create"):
            return
        clean_name = name.strip().replace(" ", "_")[:64]
        await interaction.response.defer(ephemeral=True)
        await self._do_create_backup(interaction, clean_name, defer_done=True)

    # /backup load
    @backup_group.command(name="load", description="⚠️ Restore server from a backup. DANGEROUS.")
    @app_commands.describe(name="Name of the backup to restore")
    async def backup_load(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_access(interaction, "load"):
            return

        guild = interaction.guild
        if guild_restore_locks.get(guild.id):
            await interaction.response.send_message(
                embed=_error_embed("A restore is already running for this server."), ephemeral=True
            )
            return

        path = _backup_path(guild.id, name)
        if not path.exists():
            await interaction.response.send_message(
                embed=_error_embed(f"Backup **`{name}`** does not exist."), ephemeral=True
            )
            return

        logger.info("[RESTORE] Requested — guild: %s (%d) | backup: %s | user: %s (%d)",
                    guild.name, guild.id, name, interaction.user, interaction.user.id)

        embed = _warn_embed(
            "⚠️  WARNING — Server Restore",
            (
                f"You are about to restore **{guild.name}** from backup **`{name}`**.\n\n"
                "This will **completely replace** the current server structure:\n"
                "• All roles (except @everyone, bot role, Owner role) will be deleted\n"
                "• All categories and channels will be deleted and recreated\n"
                "• Emojis, stickers, automod rules will be recreated\n\n"
                "**This operation cannot be undone.**\n\n"
                "Click **⚠️ Confirm Restore**, then type `RESTORE` in the modal to proceed."
            ),
        )
        view = RestoreStep1View(guild.id, name, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # /backup delete
    @backup_group.command(name="delete", description="Delete a backup permanently.")
    @app_commands.describe(name="Name of the backup to delete")
    async def backup_delete(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_access(interaction, "delete"):
            return

        path = _backup_path(interaction.guild_id, name)
        if not path.exists():
            await interaction.response.send_message(
                embed=_error_embed(f"Backup **`{name}`** does not exist."), ephemeral=True
            )
            return

        embed = _warn_embed(
            "🗑️  Delete Backup",
            f"Are you sure you want to permanently delete backup **`{name}`**?\n\nThis cannot be undone.",
        )
        view = ConfirmDeleteView(interaction.guild_id, name, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # /backup list
    @backup_group.command(name="list", description="List all available backups for this server.")
    async def backup_list(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction, "list"):
            return
        await self._do_list(interaction)

    # /backup info
    @backup_group.command(name="info", description="Show detailed information about a specific backup.")
    @app_commands.describe(name="Name of the backup")
    async def backup_info(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._require_access(interaction, "info"):
            return

        row = await _db_get(interaction.guild_id, name)
        if not row:
            await interaction.response.send_message(
                embed=_error_embed(f"Backup **`{name}`** not found."), ephemeral=True
            )
            return

        path = _backup_path(interaction.guild_id, name)
        size_str = f"{row['file_size'] // 1024} KB"
        auto_label = "Yes *(auto)*" if row["is_auto"] else "No"

        embed = discord.Embed(title=f"📦  Backup Info — {name}", colour=COL_INFO)
        embed.add_field(name="🏷️ Name",         value=row["backup_name"],                              inline=False)
        embed.add_field(name="📅 Created",       value=f"`{row['created_at'][:19].replace('T',' ')} UTC`", inline=True)
        embed.add_field(name="🤖 Auto Backup",   value=auto_label,                                      inline=True)
        embed.add_field(name="📄 File Size",     value=size_str,                                        inline=True)
        embed.add_field(name="🎭 Roles",         value=str(row["role_count"]),                          inline=True)
        embed.add_field(name="📁 Categories",    value=str(row["category_count"]),                      inline=True)
        embed.add_field(name="💬 Channels",      value=str(row["channel_count"]),                       inline=True)
        embed.add_field(name="😀 Emojis",        value=str(row["emoji_count"]),                         inline=True)
        embed.add_field(name="🎨 Stickers",      value=str(row["sticker_count"]),                       inline=True)
        embed.add_field(name="🛡️ AutoMod Rules", value=str(row["automod_count"]),                       inline=True)
        embed.set_footer(text="🐶 Puppy Backup System")
        embed.timestamp = discord.utils.utcnow()

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /backuppanel
    @app_commands.command(name="backuppanel", description="Post the persistent Backup Center panel.")
    async def backuppanel(self, interaction: discord.Interaction) -> None:
        if not await self._require_access(interaction, "backuppanel"):
            return

        await interaction.response.defer(ephemeral=True)

        rows       = await _db_list(interaction.guild_id)
        count      = len(rows)
        latest     = rows[0]["backup_name"] if rows else "None"
        auto_rows  = await _db_find_auto(interaction.guild_id)
        auto_status = f"✅ `{auto_rows[0]['backup_name']}`" if auto_rows else "⚠️ No auto backup yet"

        auto_enabled = await _db_get_auto_enabled(interaction.guild_id)
        auto_status_icon = "🟢 **Enabled**" if auto_enabled else "🔴 **Disabled**"

        embed = discord.Embed(
            title="💾 Puppy Backup Center",
            description=(
                "Manage server backups using the buttons below.\n\n"
                "**💾 Create Backup** — Snapshot the current server structure\n"
                "**📥 Load Backup** — Restore from a saved backup *(dangerous)*\n"
                "**🟢 Auto Backup ON** — Enable daily automatic backups\n"
                "**🔴 Auto Backup OFF** — Disable daily automatic backups\n"
                "**📋 Backup List** — Browse all available backups"
            ),
            colour=COL_INFO,
        )
        embed.add_field(name="📦 Total Backups",   value=str(count),         inline=True)
        embed.add_field(name="🕐 Latest Backup",   value=latest,             inline=True)
        embed.add_field(name="🤖 Auto Backup",     value=auto_status_icon,   inline=True)
        embed.add_field(name="📅 Next Auto",       value="`AUTO_" + datetime.now(timezone.utc).strftime("%Y_%m_%d") + "`" if auto_enabled else "Disabled", inline=False)
        embed.set_footer(text="🐶 Puppy Backup System • Owner only")
        embed.timestamp = discord.utils.utcnow()

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                embed=_error_embed("This command must be used in a text channel."), ephemeral=True
            )
            return

        try:
            await interaction.channel.send(embed=embed, view=BackupPanelView())
            await interaction.followup.send(
                embed=_success_embed("✅  Panel Posted", "Backup Center panel has been posted."),
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=_error_embed("I don't have permission to send messages in this channel."), ephemeral=True
            )
        except Exception:
            logger.exception("[ERROR] Failed to post backuppanel")
            await interaction.followup.send(
                embed=_error_embed("Failed to post panel. Check bot logs."), ephemeral=True
            )


# ══════════════════════════════════════════════════════════════════════════════
# Setup
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    """Entry point called by discord.py's load_extension."""
    await bot.add_cog(BackupSystem(bot))
