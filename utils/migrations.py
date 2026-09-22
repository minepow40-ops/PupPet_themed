"""
utils/migrations.py
Lightweight schema-versioning / migration runner for PupPet's SQLite databases.

Why this exists
----------------
Every cog currently does:

    CREATE TABLE IF NOT EXISTS foo (...)

That only handles brand-new tables. It does NOT handle:
  - adding a column to an existing table
  - renaming/dropping a column
  - changing a constraint (e.g. PRIMARY KEY)
  - backfilling data for a new column

If you ever ship a schema change, every user's existing .db file on disk
will NOT get that change automatically, and the bot can crash with
"no such column" errors at runtime.

How this works
---------------
Each database gets a numbered list of migrations:

    MIGRATIONS = [
        Migration(1, "create cases table", "CREATE TABLE IF NOT EXISTS cases (...)"),
        Migration(2, "add severity column", "ALTER TABLE cases ADD COLUMN severity TEXT DEFAULT 'low'"),
    ]

SQLite has a built-in per-file counter for exactly this purpose:
`PRAGMA user_version`. We store "the last migration number that was
successfully applied" there. On every bot startup we:

    1. Read current user_version (0 if brand new file)
    2. Run every Migration with number > current version, in order
    3. Bump user_version after each successful migration
    4. Wrap the whole batch in a transaction so a crash mid-migration
       doesn't leave the db half-upgraded

Usage in a cog
---------------
    from utils.migrations import Migration, run_migrations

    MIGRATIONS = [
        Migration(1, "create cases table", '''
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ...
            )
        '''),
    ]

    def init_db():
        with get_connection() as con:
            run_migrations(con, MIGRATIONS, db_name="moderation.db")

Each cog keeps its own MIGRATIONS list (this stays decentralized on
purpose — one cog's schema change should never require touching
another cog's file). The shared logic (version checking, transaction
safety, logging) lives here once instead of being copy-pasted six times.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Sequence

log = logging.getLogger("PupPet.migrations")


@dataclass(frozen=True)
class Migration:
    version: int        # must be unique and sequential within a db's list
    description: str    # short human-readable summary, shown in logs
    sql: str             # one or more SQL statements (executescript-compatible)


def get_user_version(con: sqlite3.Connection) -> int:
    cur = con.execute("PRAGMA user_version")
    return cur.fetchone()[0]


def set_user_version(con: sqlite3.Connection, version: int) -> None:
    # PRAGMA doesn't support bound parameters, so validate it's an int first.
    if not isinstance(version, int):
        raise TypeError(f"user_version must be int, got {type(version)}")
    con.execute(f"PRAGMA user_version = {version}")


def run_migrations(
    con: sqlite3.Connection,
    migrations: Sequence[Migration],
    db_name: str = "",
) -> int:
    """
    Apply every migration whose version is greater than the database's
    current user_version, in ascending order. Returns the final version.

    Safe to call on every bot startup — if there's nothing new to apply,
    this is a single fast PRAGMA read and a no-op.
    """
    ordered = sorted(migrations, key=lambda m: m.version)

    # Fail fast on programmer error: duplicate or non-sequential versions
    # are almost always a copy-paste mistake.
    seen = set()
    for m in ordered:
        if m.version in seen:
            raise ValueError(f"[{db_name}] duplicate migration version: {m.version}")
        seen.add(m.version)

    current = get_user_version(con)
    pending = [m for m in ordered if m.version > current]

    if not pending:
        log.debug("[%s] schema up to date (version %d)", db_name, current)
        return current

    log.info(
        "[%s] applying %d pending migration(s), current version %d -> %d",
        db_name, len(pending), current, pending[-1].version,
    )

    for m in pending:
        try:
            # NOTE: sqlite3's executescript() issues an implicit COMMIT of any
            # open transaction before it runs, and statements inside it can't
            # be rolled back as a unit. That means a half-failed multi-statement
            # migration can leave the schema partially applied even if we
            # "rollback" afterwards. To keep each migration atomic, run its
            # statements one at a time inside an explicit transaction instead
            # of handing the whole blob to executescript().
            statements = [s.strip() for s in m.sql.split(";") if s.strip()]
            con.execute("BEGIN")
            for stmt in statements:
                con.execute(stmt)
            set_user_version(con, m.version)
            con.commit()
            log.info("[%s] migration %d applied: %s", db_name, m.version, m.description)
        except Exception:
            con.rollback()
            log.exception(
                "[%s] migration %d FAILED (%s) — stopping here, db left at version %d",
                db_name, m.version, m.description, get_user_version(con),
            )
            raise

    return get_user_version(con)
