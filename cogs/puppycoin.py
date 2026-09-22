"""
cogs/puppycoin.py
Puppycoin Economy System — PupPet
─────────────────────────────────────────────────────────────────────────────
• Earn Puppycoins by chatting (100 messages → 2 coins, cap 10/day)
• /puppycoin     — wallet & stats
• /puppycoinlb   — top-3 leaderboard embed
• /paypuppycoin  — peer-to-peer transfer (cap 5/day)
• Auto daily leaderboard post (top-5, persistent schedule)
• Rolling 24-hour personal reset per user
• SQLite backend via aiosqlite
• Automated daily DB backups (14-day retention)
• Rich Puppy role (RICH_ROLE_ID 1512575341669912576): the all-time #1
  user by total_coins, provided they have ≥ 50 coins, holds the role.
  Only ONE user holds it at a time. Role transfers automatically when
  someone overtakes the current holder. If nobody has 50+ coins, the
  role is removed from everyone. Synced on every coin earn, every
  /paypuppycoin transfer, and every 5 minutes via background task.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.migrations import Migration, run_migrations

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("PupPet.puppycoin")

# ── Constants ─────────────────────────────────────────────────────────────────
REQUIRED_ROLE_ID       : int   = 1551515675825274961
RICH_ROLE_ID           : int   = 1551515674646683711   # 👑 Rich Puppy role
RICH_ROLE_MIN_COINS    : int   = 50                    # Minimum total_coins to qualify for Rich Puppy
LEADERBOARD_CHANNEL_ID : int   = 15515157183589715850
MESSAGES_PER_REWARD    : int   = 100
COINS_PER_REWARD       : int   = 2
DAILY_EARN_LIMIT       : int   = 10
DAILY_TRANSFER_LIMIT   : int   = 5
MESSAGE_COOLDOWN       : float = 10.0        # seconds
RESET_INTERVAL         : float = 86_400.0    # 24 hours in seconds
BACKUP_RETENTION       : int   = 14          # keep this many daily backups

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH      = Path("data/puppycoin.db")
BACKUP_DIR   = Path("data/backups")

# ── DDL ───────────────────────────────────────────────────────────────────────
CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS puppycoin_users (
    user_id                INTEGER PRIMARY KEY,
    total_coins            INTEGER NOT NULL DEFAULT 0,
    daily_earned           INTEGER NOT NULL DEFAULT 0,
    daily_paid             INTEGER NOT NULL DEFAULT 0,
    message_count          INTEGER NOT NULL DEFAULT 0,
    messages_since_reward  INTEGER NOT NULL DEFAULT 0,
    last_message_time      REAL    NOT NULL DEFAULT 0,
    last_message_content   TEXT    NOT NULL DEFAULT '',
    last_reset             REAL    NOT NULL DEFAULT 0
);
"""

# FIX 13 — Persistent scheduling metadata table.
CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS puppycoin_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ── Colour palette ────────────────────────────────────────────────────────────
COL_GOLD    = 0xF1C40F
COL_SUCCESS = 0x2ECC71
COL_ERROR   = 0xE74C3C
COL_INFO    = 0x5865F2
COL_WARN    = 0xF39C12

MEDAL = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


# ─────────────────────────────────────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────────────────────────────────────

# Every schema change for this database gets ONE new entry appended here.
# NEVER edit an existing entry once it has shipped — add a new one instead,
# even for something as small as a typo in a column name.
MIGRATIONS = [
    Migration(1, "initial schema — puppycoin_users + puppycoin_meta", (
        CREATE_USERS_SQL + CREATE_META_SQL
    )),
]


async def init_db() -> None:
    """Create data directory and all tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Migrations run synchronously — this only happens once at cog load,
        # so blocking briefly here is fine and keeps version-tracking logic
        # in one place instead of duplicating it for aiosqlite.
        con = sqlite3.connect(DB_PATH)
        try:
            run_migrations(con, MIGRATIONS, db_name="puppycoin.db")
        finally:
            con.close()
        log.info("[Puppycoin] Database initialised at %s", DB_PATH)
    except sqlite3.Error as exc:
        log.error("[Puppycoin] Database initialisation failed: %s", exc)
        raise


async def _get_meta(db: aiosqlite.Connection, key: str) -> Optional[str]:
    """Read a value from puppycoin_meta. Returns None if absent."""
    async with db.execute(
        "SELECT value FROM puppycoin_meta WHERE key = ?", (key,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def _set_meta(db: aiosqlite.Connection, key: str, value: str) -> None:
    """Upsert a value in puppycoin_meta."""
    await db.execute(
        "INSERT INTO puppycoin_meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def _get_user(db: aiosqlite.Connection, user_id: int) -> dict:
    """
    Fetch a user row, inserting a default record if absent.
    Also performs a rolling daily reset if 24 h have elapsed.
    Returns a plain dict with all columns.
    """
    async with db.execute(
        "SELECT * FROM puppycoin_users WHERE user_id = ?", (user_id,)
    ) as cur:
        row = await cur.fetchone()

    if row is None:
        now = time.time()
        await db.execute(
            """INSERT INTO puppycoin_users
               (user_id, total_coins, daily_earned, daily_paid,
                message_count, messages_since_reward,
                last_message_time, last_message_content, last_reset)
               VALUES (?,0,0,0,0,0,0,'',?)""",
            (user_id, now),
        )
        await db.commit()
        return {
            "user_id": user_id,
            "total_coins": 0,
            "daily_earned": 0,
            "daily_paid": 0,
            "message_count": 0,
            "messages_since_reward": 0,
            "last_message_time": 0.0,
            "last_message_content": "",
            "last_reset": now,
        }

    data = dict(zip(
        ("user_id", "total_coins", "daily_earned", "daily_paid",
         "message_count", "messages_since_reward",
         "last_message_time", "last_message_content", "last_reset"),
        row,
    ))

    # Rolling daily reset — FIX 11: this runs inside db_lock via callers
    now = time.time()
    if now - data["last_reset"] >= RESET_INTERVAL:
        data["daily_earned"] = 0
        data["daily_paid"]   = 0
        data["last_reset"]   = now
        await db.execute(
            """UPDATE puppycoin_users
               SET daily_earned = 0, daily_paid = 0, last_reset = ?
               WHERE user_id = ?""",
            (now, user_id),
        )
        await db.commit()

    return data


async def _save_user(db: aiosqlite.Connection, data: dict) -> None:
    """Persist all mutable fields for a user row."""
    await db.execute(
        """UPDATE puppycoin_users
           SET total_coins           = :total_coins,
               daily_earned          = :daily_earned,
               daily_paid            = :daily_paid,
               message_count         = :message_count,
               messages_since_reward = :messages_since_reward,
               last_message_time     = :last_message_time,
               last_message_content  = :last_message_content,
               last_reset            = :last_reset
           WHERE user_id = :user_id""",
        data,
    )
    await db.commit()


# FIX 3 — Deterministic rank: ties broken by user_id ASC.
async def _get_rank(db: aiosqlite.Connection, user_id: int) -> int:
    """Return 1-based global rank by total_coins DESC, user_id ASC."""
    async with db.execute(
        """SELECT COUNT(*) + 1
           FROM puppycoin_users
           WHERE total_coins > (
               SELECT total_coins FROM puppycoin_users WHERE user_id = ?
           )
              OR (
               total_coins = (
                   SELECT total_coins FROM puppycoin_users WHERE user_id = ?
               )
               AND user_id < ?
           )""",
        (user_id, user_id, user_id),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 1


async def _top_users(db: aiosqlite.Connection, n: int = 5, *, by: str = "total_coins") -> list[dict]:
    """Return top-n rows sorted by column DESC, user_id ASC (stable)."""
    col = by if by in ("total_coins", "daily_earned") else "total_coins"
    async with db.execute(
        f"SELECT * FROM puppycoin_users ORDER BY {col} DESC, user_id ASC LIMIT ?", (n,)
    ) as cur:
        rows = await cur.fetchall()
    return [
        dict(zip(
            ("user_id", "total_coins", "daily_earned", "daily_paid",
             "message_count", "messages_since_reward",
             "last_message_time", "last_message_content", "last_reset"),
            row,
        ))
        for row in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Rich-role helper
# ─────────────────────────────────────────────────────────────────────────────

async def _sync_rich_role(guild: discord.Guild) -> None:
    """
    👑 Rich Puppy — always exactly ONE holder.

    Rules:
      1. Find the user with the highest total_coins (all-time lifetime).
      2. That user must have >= RICH_ROLE_MIN_COINS (50) to qualify.
      3. Remove the role from EVERYONE who currently holds it but is not the new #1.
      4. Grant the role to the qualifying #1 if they don't already have it.
      5. If nobody qualifies (richest < 50 coins), remove the role from everyone.

    Tie-break: lower user_id wins (stable, deterministic).

    Triggered on every coin earn, every /paypuppycoin transfer,
    and polled every 5 minutes by rich_role_task.
    """
    role = guild.get_role(RICH_ROLE_ID)
    if role is None:
        log.warning("[Puppycoin] RICH_ROLE_ID %d not found in guild.", RICH_ROLE_ID)
        return

    # 1. Find the richest user with at least RICH_ROLE_MIN_COINS
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                """SELECT user_id, total_coins
                   FROM puppycoin_users
                   WHERE total_coins >= ?
                   ORDER BY total_coins DESC, user_id ASC
                   LIMIT 1""",
                (RICH_ROLE_MIN_COINS,),
            ) as cur:
                row = await cur.fetchone()
    except aiosqlite.Error as exc:
        log.error("[Puppycoin] _sync_rich_role DB error: %s", exc)
        return

    # Nobody qualifies → strip role from all current holders and exit
    if row is None:
        for holder in [m for m in guild.members if role in m.roles]:
            try:
                await holder.remove_roles(role, reason="Puppycoin: nobody has 50+ coins")
                log.info("[Puppycoin] 👑 Rich role removed from %s (no one qualifies)", holder)
            except discord.Forbidden:
                log.warning("[Puppycoin] No permission to remove rich role from %s.", holder)
            except discord.HTTPException as exc:
                log.error("[Puppycoin] Failed to remove rich role from %s: %s", holder, exc)
        return

    top1_id: int    = row[0]
    top1_coins: int = row[1]
    top1_member: Optional[discord.Member] = guild.get_member(top1_id)

    # 2. Strip role from everyone who holds it but is NOT the new #1
    for holder in [m for m in guild.members if role in m.roles]:
        if holder.id != top1_id:
            try:
                await holder.remove_roles(role, reason="Puppycoin: no longer #1 Rich Puppy")
                log.info("[Puppycoin] 👑 Rich role removed from %s (no longer #1)", holder)
            except discord.Forbidden:
                log.warning("[Puppycoin] No permission to remove rich role from %s.", holder)
            except discord.HTTPException as exc:
                log.error("[Puppycoin] Failed to remove rich role from %s: %s", holder, exc)

    # 3. Grant role to the new #1 (must be in guild cache)
    if top1_member is None:
        log.warning(
            "[Puppycoin] 👑 Top-1 user %d (%d coins) not in guild cache — role not granted.",
            top1_id, top1_coins,
        )
        return

    if role not in top1_member.roles:
        try:
            await top1_member.add_roles(role, reason=f"Puppycoin: #1 Rich Puppy 👑 ({top1_coins} coins)")
            log.info(
                "[Puppycoin] 👑 Rich role granted to %s (%d coins — #1 with 50+ minimum)",
                top1_member, top1_coins,
            )
        except discord.Forbidden:
            log.warning("[Puppycoin] No permission to assign rich role to %s.", top1_member)
        except discord.HTTPException as exc:
            log.error("[Puppycoin] Failed to assign rich role to %s: %s", top1_member, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Leaderboard post helper (shared by task and catch-up logic)
# ─────────────────────────────────────────────────────────────────────────────

async def _post_daily_leaderboard(bot: commands.Bot) -> bool:
    """
    Fetch top earners and post the daily leaderboard embed.
    Returns True on success, False on any failure.
    """
    # FIX 7 — Missing channel protection.
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        log.warning(
            "[Puppycoin] Daily LB: channel %d not found or not a text channel — skipping post.",
            LEADERBOARD_CHANNEL_ID,
        )
        return False

    # FIX 8 — Permission check.
    perms = channel.permissions_for(channel.guild.me)
    if not perms.send_messages or not perms.embed_links:
        log.warning(
            "[Puppycoin] Daily LB: missing send_messages or embed_links in channel %d — skipping.",
            LEADERBOARD_CHANNEL_ID,
        )
        return False

    # FIX 9 — DB error protection.
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            top = await _top_users(db, n=5, by="daily_earned")
    except aiosqlite.Error as exc:
        log.error("[Puppycoin] Daily LB: DB error — %s", exc)
        return False

    next_reset_ts = int(time.time() + RESET_INTERVAL)
    embed = discord.Embed(
        title="🏆 Daily Puppycoin Earners",
        color=COL_GOLD,
        timestamp=discord.utils.utcnow(),
    )
    embed.description = (
        "Nobody earned any Puppycoins today. 🦴💤"
        if not top
        else "Top earners from the past 24 hours — well done, good pups! 🦴"
    )
    for i, row in enumerate(top):
        member = channel.guild.get_member(row["user_id"])
        name   = member.display_name if member else f"User #{row['user_id']}"
        embed.add_field(
            name=f"{MEDAL[i]}  {name}",
            value=f"**{row['daily_earned']}** 🦴 earned today",
            inline=False,
        )
    embed.add_field(name="⏰ Next Reset", value=f"<t:{next_reset_ts}:R>", inline=False)
    embed.set_footer(text=f"PupPet • Daily Max: {DAILY_EARN_LIMIT} Puppycoins")

    try:
        await channel.send(embed=embed)
        log.info("[Puppycoin] Daily leaderboard posted.")
        return True
    except discord.HTTPException as exc:
        log.error("[Puppycoin] Daily LB send failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Backup helper
# ─────────────────────────────────────────────────────────────────────────────

def _run_backup() -> None:
    """
    Copy puppycoin.db → data/backups/puppycoin_YYYY_MM_DD.db.
    Keeps only the latest BACKUP_RETENTION files. Runs synchronously
    (called via asyncio.to_thread so it won't block the event loop).
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp  = datetime.now(timezone.utc).strftime("%Y_%m_%d")
    dest   = BACKUP_DIR / f"puppycoin_{stamp}.db"
    shutil.copy2(DB_PATH, dest)
    log.info("[Puppycoin] Backup created: %s", dest.name)

    # Prune old backups beyond retention window.
    backups = sorted(BACKUP_DIR.glob("puppycoin_*.db"))
    for old in backups[:-BACKUP_RETENTION]:
        old.unlink(missing_ok=True)
        log.info("[Puppycoin] Old backup removed: %s", old.name)


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class Puppycoin(commands.Cog, name="Puppycoin"):
    """🦴 Puppycoin Economy System — earn, spend, and flex your treats."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot       = bot
        self._db_ready = asyncio.Event()
        # FIX 11 — Single shared lock covers ALL balance-changing operations:
        #           coin rewards, transfers, and daily resets.
        self.db_lock   = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        """Async setup: validate config, init DB, start background tasks."""
        # FIX 15 — Startup validation (warn but never crash).
        if not REQUIRED_ROLE_ID:
            log.warning("[Puppycoin] ⚠️  REQUIRED_ROLE_ID is not configured.")
        else:
            log.info("[Puppycoin] ✅ Required role ID: %d", REQUIRED_ROLE_ID)

        if not RICH_ROLE_ID:
            log.warning("[Puppycoin] ⚠️  RICH_ROLE_ID is not configured — rich-role feature disabled.")
        else:
            log.info("[Puppycoin] ✅ Rich role ID: %d (👑 #1 with 50+ coins — polled every 5 min)", RICH_ROLE_ID)

        if not LEADERBOARD_CHANNEL_ID:
            log.warning("[Puppycoin] ⚠️  LEADERBOARD_CHANNEL_ID is not configured.")
        else:
            log.info("[Puppycoin] ✅ Leaderboard channel ID: %d", LEADERBOARD_CHANNEL_ID)

        # Attempt DB init; warn on failure but do not abort cog load.
        try:
            await init_db()
            self._db_ready.set()
            log.info("[Puppycoin] ✅ Database connected and ready: %s", DB_PATH.resolve())
        except Exception as exc:
            log.warning("[Puppycoin] ⚠️  Database init failed — cog will retry on first use. Error: %s", exc)

        # FIX 15 — Verify DB file exists after init attempt.
        if DB_PATH.exists():
            log.info("[Puppycoin] ✅ Database file confirmed at %s", DB_PATH.resolve())
        else:
            log.warning("[Puppycoin] ⚠️  Database file not found at %s", DB_PATH.resolve())

        # FIX 16 — Backup task: start only if not already running so hot-reloads,
        #           reconnects, and cog reloads never spawn a second instance.
        if not self.backup_task.is_running():
            self.backup_task.start()
            log.info("[Puppycoin] ✅ Backup task started: True")
        else:
            log.warning("[Puppycoin] ⚠️  Backup task already running.")

        # FIX 13 — Persistent leaderboard scheduler replaces @tasks.loop(hours=24).
        #           Launch as a plain asyncio task so it survives independently.
        self._lb_task = asyncio.ensure_future(self._persistent_leaderboard_loop())

        # Rich-role background poller — catches missed events (restarts, manual DB edits).
        if not self.rich_role_task.is_running():
            self.rich_role_task.start()
            log.info("[Puppycoin] ✅ Rich-role poller started (every 5 min).")
        else:
            log.warning("[Puppycoin] ⚠️  Rich-role poller already running.")

        log.info("[Puppycoin] ✅ Puppycoin cog loaded.")

    def cog_unload(self) -> None:
        # FIX 16 — Cancel backup task only when it is actually running, then log.
        if self.backup_task.is_running():
            self.backup_task.cancel()
            log.info("[Puppycoin] 🛑 Backup task cancelled.")

        # Cancel the rich-role poller.
        if self.rich_role_task.is_running():
            self.rich_role_task.cancel()
            log.info("[Puppycoin] 🛑 Rich-role poller cancelled.")

        # Cancel the persistent leaderboard loop if it is still alive.
        if hasattr(self, "_lb_task") and not self._lb_task.done():
            self._lb_task.cancel()

    # ── DB init guard ─────────────────────────────────────────────────────────

    async def _ensure_db(self) -> None:
        if not self._db_ready.is_set():
            await init_db()
            self._db_ready.set()
            log.info("[Puppycoin] ✅ Database connected and ready.")

    # ── Member eligibility ────────────────────────────────────────────────────

    @staticmethod
    def _has_required_role(member: discord.Member) -> bool:
        return any(r.id == REQUIRED_ROLE_ID for r in member.roles)

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 13 — Persistent daily leaderboard loop
    # Stores last_daily_post in puppycoin_meta so timing survives restarts.
    # ─────────────────────────────────────────────────────────────────────────

    async def _persistent_leaderboard_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._ensure_db()

        while True:
            try:
                now = time.time()

                # Read last post time from DB.
                async with aiosqlite.connect(DB_PATH) as db:
                    raw = await _get_meta(db, "last_daily_post")
                last_post = float(raw) if raw else 0.0

                elapsed  = now - last_post
                due_in   = RESET_INTERVAL - elapsed

                if due_in <= 0:
                    # Overdue or first run — post immediately.
                    posted = await _post_daily_leaderboard(self.bot)
                    if posted:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await _set_meta(db, "last_daily_post", str(time.time()))
                    # Whether or not it succeeded, wait a full cycle before retrying.
                    await asyncio.sleep(RESET_INTERVAL)
                else:
                    # Sleep until next scheduled post.
                    log.info(
                        "[Puppycoin] Next daily leaderboard in %.1f h.",
                        due_in / 3600,
                    )
                    await asyncio.sleep(due_in)

            except asyncio.CancelledError:
                log.info("[Puppycoin] Persistent leaderboard loop cancelled.")
                return
            except Exception as exc:
                log.error("[Puppycoin] Leaderboard loop error: %s — retrying in 60 s.", exc)
                await asyncio.sleep(60)

    # ─────────────────────────────────────────────────────────────────────────
    # Rich-role background poller  (every 5 minutes)
    # Ensures the 👑 role is always on the correct #1 user even after a
    # bot restart, a manual DB edit, or a missed coin-award event.
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def rich_role_task(self) -> None:
        """Poll all guilds the bot is in and sync the Rich Puppy role."""
        for guild in self.bot.guilds:
            try:
                await _sync_rich_role(guild)
            except Exception as exc:
                log.error("[Puppycoin] rich_role_task error in guild %d: %s", guild.id, exc)

    @rich_role_task.before_loop
    async def _before_rich_role(self) -> None:
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # FIX 14 — Daily backup task
    # ─────────────────────────────────────────────────────────────────────────

    @tasks.loop(hours=24)
    async def backup_task(self) -> None:
        if not DB_PATH.exists():
            log.warning("[Puppycoin] Backup skipped — DB file not found.")
            return
        try:
            await asyncio.to_thread(_run_backup)
        except Exception as exc:
            log.error("[Puppycoin] Backup failed: %s", exc)

    @backup_task.before_loop
    async def _before_backup(self) -> None:
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────────────────────────────────
    # on_message  — message counting & coin awarding
    # ─────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not message.content:
            return

        member = message.author
        if not isinstance(member, discord.Member):
            return

        # FIX 6 — Missing role: preserve all data, only block future earning.
        if not self._has_required_role(member):
            return

        await self._ensure_db()
        now = time.time()

        # FIX 11 — db_lock serialises ALL balance-changing DB writes.
        async with self.db_lock:
            awarded = 0   # declared here so it is always defined after the try block
            data    = {}
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    data = await _get_user(db, member.id)

                    if now - data["last_message_time"] < MESSAGE_COOLDOWN:
                        return
                    if message.content == data["last_message_content"]:
                        return

                    data["message_count"]        += 1
                    data["messages_since_reward"] += 1
                    data["last_message_time"]     = now
                    data["last_message_content"]  = message.content

                    # FIX 5 — Exact reward boundary logic.

                    if data["messages_since_reward"] >= MESSAGES_PER_REWARD:
                        if data["daily_earned"] < DAILY_EARN_LIMIT:
                            reward = min(COINS_PER_REWARD, DAILY_EARN_LIMIT - data["daily_earned"])
                            data["total_coins"]  += reward
                            data["daily_earned"] += reward
                            awarded = reward
                            log.debug(
                                "[Puppycoin] +%d coins → %s (total=%d, daily=%d)",
                                reward, member, data["total_coins"], data["daily_earned"],
                            )
                        # Always reset counter even when cap is reached.
                        data["messages_since_reward"] = 0

                    await _save_user(db, data)

                # Rich-role check runs outside the DB context but still inside
                # db_lock, so total_coins is consistent with the write above.
                if awarded:
                    await _sync_rich_role(member.guild)

            except aiosqlite.Error as exc:
                log.error("[Puppycoin] on_message DB error for user %d: %s", member.id, exc)

    # ─────────────────────────────────────────────────────────────────────────
    # /puppycoin — wallet embed
    # FIX 12 — guild_only() prevents DM execution.
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="puppycoin",
        description="🦴 View your Puppycoin wallet and daily stats.",
    )
    @app_commands.guild_only()
    @app_commands.describe(member="The member to look up (leave blank for yourself).")
    async def puppycoin_cmd(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # FIX 12 — Safety net in case guild_only() is bypassed.
        if not interaction.guild:
            await interaction.followup.send(
                embed=_err("Guild Only", "This command can only be used inside a server."),
                ephemeral=True,
            )
            return

        await self._ensure_db()

        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.followup.send(
                embed=_err("Member not found", "That user doesn't appear to be in this server."),
                ephemeral=True,
            )
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                data = await _get_user(db, target.id)
                rank = await _get_rank(db, target.id)
        except aiosqlite.Error as exc:
            log.error("[Puppycoin] /puppycoin DB error for user %d: %s", target.id, exc)
            await interaction.followup.send(
                embed=_err("Database Error", "Could not load wallet data. Please try again later."),
                ephemeral=True,
            )
            return

        has_role      = self._has_required_role(target)
        next_reset_ts = int(data["last_reset"] + RESET_INTERVAL)
        msgs_to_next  = MESSAGES_PER_REWARD - data["messages_since_reward"]

        embed = discord.Embed(title="🦴 Puppycoin Wallet", color=COL_GOLD, timestamp=discord.utils.utcnow())
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.add_field(name="👤 User",               value=target.mention,                    inline=True)
        embed.add_field(name="🪙 Total Coins",         value=f"**{data['total_coins']:,}** 🦴", inline=True)
        embed.add_field(name="🌍 Global Rank",         value=f"**#{rank:,}**",                  inline=True)
        embed.add_field(name="📈 Earned Today",        value=f"`{data['daily_earned']}/{DAILY_EARN_LIMIT}`",    inline=True)
        embed.add_field(name="💸 Transferred Today",   value=f"`{data['daily_paid']}/{DAILY_TRANSFER_LIMIT}`", inline=True)
        embed.add_field(name="💬 Messages Counted",    value=f"`{data['message_count']:,}`",    inline=True)
        embed.add_field(
            name="📬 Next Reward",
            value=(
                f"`{msgs_to_next}` messages away"
                if has_role and data["daily_earned"] < DAILY_EARN_LIMIT
                else "`Daily cap reached — resets soon`"
            ),
            inline=True,
        )
        embed.add_field(name="⏰ Daily Reset",  value=f"<t:{next_reset_ts}:R>",                           inline=True)
        embed.add_field(name="🎖️ Eligible",    value="✅ Yes" if has_role else "❌ Missing required role", inline=True)
        embed.set_footer(text="PupPet • Puppycoin Economy")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /puppycoinlb — top-3 leaderboard
    # FIX 12 — guild_only()
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="puppycoinlb",
        description="🏆 View the top Puppycoin earners leaderboard.",
    )
    @app_commands.guild_only()
    async def puppycoinlb_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # FIX 12 — Safety net.
        if not interaction.guild:
            await interaction.followup.send(
                embed=_err("Guild Only", "This command can only be used inside a server."),
                ephemeral=True,
            )
            return

        await self._ensure_db()

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                top = await _top_users(db, n=3, by="total_coins")
        except aiosqlite.Error as exc:
            log.error("[Puppycoin] /puppycoinlb DB error: %s", exc)
            await interaction.followup.send(
                embed=_err("Database Error", "Could not load leaderboard. Please try again later."),
                ephemeral=True,
            )
            return

        # FIX 2 — Empty leaderboard: never crash, return correct embed.
        if not top:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🦴 Puppycoin Leaderboard",
                    description="No Puppycoins have been earned yet.",
                    color=COL_GOLD,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="🏆 Puppycoin Leaderboard — Top 3", color=COL_GOLD, timestamp=discord.utils.utcnow())
        for i, row in enumerate(top):
            uid    = row["user_id"]
            member = interaction.guild.get_member(uid)
            name   = member.display_name if member else f"User #{uid}"
            embed.add_field(
                name=f"{MEDAL[i]}  {name}",
                value=f"🪙 **{row['total_coins']:,}** total coins\n📈 **{row['daily_earned']}** earned today",
                inline=False,
            )
        embed.set_footer(text=f"PupPet • Daily Cap: {DAILY_EARN_LIMIT} Puppycoins")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────────────
    # /paypuppycoin — peer-to-peer transfer
    # FIX 12 — guild_only()
    # ─────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="paypuppycoin",
        description="💸 Send Puppycoins to another member.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        user="The member to send coins to.",
        amount="Number of Puppycoins to send (must be > 0).",
    )
    async def paypuppycoin_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        # FIX 12 — Safety net.
        if not interaction.guild:
            await interaction.followup.send(
                embed=_err("Guild Only", "This command can only be used inside a server."),
                ephemeral=True,
            )
            return

        await self._ensure_db()

        sender = interaction.user
        if not isinstance(sender, discord.Member):
            await interaction.followup.send(
                embed=_err("Error", "Could not resolve your member data."), ephemeral=True
            )
            return

        if user.id == sender.id:
            await interaction.followup.send(
                embed=_err("Invalid Transfer", "You cannot send Puppycoins to yourself."),
                ephemeral=True,
            )
            return

        if user.bot:
            await interaction.followup.send(
                embed=_err("Invalid Transfer", "Bots don't have wallets — they don't deserve coins either. 🤖"),
                ephemeral=True,
            )
            return

        # FIX 11 — db_lock makes the read-validate-write sequence atomic,
        #           preventing two simultaneous transfers from racing.
        async with self.db_lock:
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    sender_data   = await _get_user(db, sender.id)
                    receiver_data = await _get_user(db, user.id)

                    if sender_data["total_coins"] < amount:
                        await interaction.followup.send(
                            embed=_err(
                                "Insufficient Balance",
                                f"You only have **{sender_data['total_coins']:,}** 🦴 "
                                f"but tried to send **{amount:,}**.",
                            ),
                            ephemeral=True,
                        )
                        return

                    # FIX 4 — daily_paid + amount must not exceed DAILY_TRANSFER_LIMIT.
                    if sender_data["daily_paid"] + amount > DAILY_TRANSFER_LIMIT:
                        remaining     = DAILY_TRANSFER_LIMIT - sender_data["daily_paid"]
                        next_reset_ts = int(sender_data["last_reset"] + RESET_INTERVAL)
                        await interaction.followup.send(
                            embed=_err(
                                "Daily Transfer Limit",
                                f"You can only transfer **{DAILY_TRANSFER_LIMIT}** 🦴 per day.\n"
                                f"You have **{remaining}** remaining today.\n"
                                f"Resets <t:{next_reset_ts}:R>.",
                            ),
                            ephemeral=True,
                        )
                        return

                    sender_data["total_coins"]   -= amount
                    sender_data["daily_paid"]    += amount
                    receiver_data["total_coins"] += amount

                    await _save_user(db, sender_data)
                    await _save_user(db, receiver_data)

            except aiosqlite.Error as exc:
                log.error("[Puppycoin] /paypuppycoin DB error (sender=%d): %s", sender.id, exc)
                await interaction.followup.send(
                    embed=_err("Database Error", "Transfer could not be completed. Please try again later."),
                    ephemeral=True,
                )
                return

        embed = discord.Embed(title="🦴 Transfer Complete", color=COL_SUCCESS, timestamp=discord.utils.utcnow())
        embed.add_field(name="📤 Sender",               value=sender.mention,                         inline=True)
        embed.add_field(name="📥 Receiver",             value=user.mention,                           inline=True)
        embed.add_field(name="🪙 Amount",               value=f"**{amount:,}** 🦴",                   inline=True)
        embed.add_field(name="💼 Your Remaining Balance", value=f"**{sender_data['total_coins']:,}** 🦴", inline=True)
        embed.add_field(name="💸 Transferred Today",    value=f"`{sender_data['daily_paid']}/{DAILY_TRANSFER_LIMIT}`", inline=True)
        embed.set_footer(text="PupPet • Puppycoin Economy")
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Sync rich role: receiver may now qualify; sender may have lost all coins.
        await _sync_rich_role(interaction.guild)

        log.info("[Puppycoin] Transfer: %s → %s  (%d coins)", sender, user, amount)


# ─────────────────────────────────────────────────────────────────────────────
# Embed helpers
# ─────────────────────────────────────────────────────────────────────────────

def _err(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"❌ {title}", description=description, color=COL_ERROR)


def _info(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COL_INFO)


# ─────────────────────────────────────────────────────────────────────────────
# Setup
# ─────────────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Puppycoin(bot))
    log.info("[Puppycoin] Cog registered.")
