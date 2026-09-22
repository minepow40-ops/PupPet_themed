# Wiring the migration system into PupPet's existing cogs

This shows exactly how to retrofit `utils/migrations.py` into your 6
database-owning cogs. One worked example below in full
(`cogs/puppy_moderation.py`), then the same recipe applied to the other
five — they all follow the identical pattern.

## The pattern (applies to every cog)

1. Import `Migration` and `run_migrations` from `utils.migrations`
2. Take your existing `CREATE TABLE IF NOT EXISTS ...` block and wrap it
   as **migration #1** — this preserves behavior for anyone already
   running the bot (their db is already "at" schema 1, nothing re-runs)
3. Any *future* schema change becomes migration #2, #3, etc. — never
   edit migration #1 again once it has shipped to any real `.db` file
4. Call `run_migrations()` instead of `executescript()` directly inside
   `init_db()`

---

## Worked example — `cogs/puppy_moderation.py`

**Before** (current code, lines 62–145):

```python
DB_PATH = "data/moderation.db"

def get_connection() -> sqlite3.Connection:
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with get_connection() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS cases ( ... );
            CREATE TABLE IF NOT EXISTS active_channel_mutes ( ... );
            CREATE TABLE IF NOT EXISTS ghost_ping_log ( ... );
            CREATE TABLE IF NOT EXISTS staff_audit_log ( ... );
            CREATE TABLE IF NOT EXISTS timeout_roles ( ... );
            CREATE TABLE IF NOT EXISTS spam_counter ( ... );
            CREATE TABLE IF NOT EXISTS alt_flags ( ... );
            CREATE TABLE IF NOT EXISTS tickets ( ... );
        """)
        con.commit()
```

**After:**

```python
from utils.migrations import Migration, run_migrations

DB_PATH = "data/moderation.db"

def get_connection() -> sqlite3.Connection:
    os.makedirs("data", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


# Every schema change for this database gets ONE new entry appended here.
# NEVER edit an existing entry once it has shipped — add a new one instead,
# even for something as small as a typo in a column name.
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
```

That's the entire change for this file. Everything else (`add_case`,
`get_cases`, etc.) stays exactly as-is — they just query tables that
now get created/upgraded through a tracked, logged path instead of a
blind `IF NOT EXISTS`.

---

## Applying this to the other 5 cogs

Same recipe, different file. The table below tells you exactly what to
wrap as Migration #1 in each one.

| Cog file | DB_PATH | What becomes Migration #1 |
|---|---|---|
| `cogs/birthday.py` | `data/birthdays.db` | the `birthdays` + `birthday_nicknames` `CREATE TABLE` block |
| `cogs/backup.py` | `data/backups.db` | the `backup_metadata` + `backup_settings` `CREATE TABLE` block |
| `cogs/badwords.py` | `data/badwords.db` | the `badwords` + `badword_settings` `CREATE TABLE` block — **note:** this file uses `aiosqlite`, so see the async variant below |
| `cogs/reaction_roles.py` | `data/reaction_roles.db` | the 6-table block (`role_panels`, `role_items`, `sticky_roles`, `temp_roles`, `role_logs`, `role_settings`) |
| `cogs/puppycoin.py` | `data/puppycoin.db` | the `puppycoin_users` + `puppycoin_meta` `CREATE TABLE` block |

### Async variant (for `badwords.py`, which uses `aiosqlite`)

`run_migrations()` as written is sync (plain `sqlite3`). `badwords.py`
uses `aiosqlite` for everything else, so the cleanest fix is to run
migrations once at startup using a throwaway sync connection, before
any async code touches the file:

```python
import sqlite3
from utils.migrations import Migration, run_migrations

DB_PATH = "data/badwords.db"

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
    os.makedirs("data", exist_ok=True)
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
```

---

## Adding a real schema change later (example walkthrough)

Say next month you want to add a `severity` column to moderation cases.

1. Open `cogs/puppy_moderation.py`
2. Append to `MIGRATIONS` (do not touch Migration 1):
   ```python
   Migration(2, "add severity to cases", """
       ALTER TABLE cases ADD COLUMN severity TEXT DEFAULT 'normal';
   """),
   ```
3. Restart the bot. On startup, `run_migrations()` sees `user_version`
   is `1`, runs migration `2`, sets `user_version` to `2`. Every
   existing case row gets `severity = 'normal'` automatically; no data
   is lost.
4. Run `python tools/generate_schema_docs.py` so `docs/DATABASE_SCHEMA.md`
   reflects the new column.

That's the whole workflow — no separate migration tool to install, no
new dependency, and it works identically across all 6 databases.
