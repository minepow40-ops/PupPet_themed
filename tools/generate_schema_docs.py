"""
tools/generate_schema_docs.py

Generates docs/DATABASE_SCHEMA.md by introspecting every .db file in data/.

Run this any time you add/change a table so the docs never drift out of
sync with reality. It reads live SQLite metadata (sqlite_master,
PRAGMA table_info, PRAGMA foreign_key_list) rather than parsing source
code, so what you get is always the *actual* on-disk schema — including
columns added by migrations that source-code grepping would miss.

Usage:
    python tools/generate_schema_docs.py
    python tools/generate_schema_docs.py --data-dir data --out docs/DATABASE_SCHEMA.md

This intentionally has zero dependencies beyond the stdlib so it can be
run in any environment the bot runs in, including CI, without installing
anything extra.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def get_user_version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA user_version").fetchone()[0]


def list_tables(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_columns(con: sqlite3.Connection, table: str) -> list[dict]:
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    # cid, name, type, notnull, dflt_value, pk
    return [
        {
            "name": r[1],
            "type": r[2] or "ANY",
            "notnull": bool(r[3]),
            "default": r[4],
            "pk": bool(r[5]),
        }
        for r in rows
    ]


def table_foreign_keys(con: sqlite3.Connection, table: str) -> list[dict]:
    rows = con.execute(f"PRAGMA foreign_key_list('{table}')").fetchall()
    # id, seq, table, from, to, on_update, on_delete, match
    return [
        {"from": r[3], "ref_table": r[2], "ref_col": r[4]}
        for r in rows
    ]


def table_row_count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]


def render_table_section(con: sqlite3.Connection, table: str) -> str:
    cols = table_columns(con, table)
    fks = {fk["from"]: fk for fk in table_foreign_keys(con, table)}
    try:
        row_count = table_row_count(con, table)
    except sqlite3.Error:
        row_count = "?"

    lines = [f"#### `{table}`  ({row_count} rows)", ""]
    lines.append("| Column | Type | Constraints | References |")
    lines.append("|---|---|---|---|")
    for c in cols:
        constraints = []
        if c["pk"]:
            constraints.append("PRIMARY KEY")
        if c["notnull"] and not c["pk"]:
            constraints.append("NOT NULL")
        if c["default"] is not None:
            constraints.append(f"DEFAULT {c['default']}")
        ref = ""
        if c["name"] in fks:
            fk = fks[c["name"]]
            ref = f"`{fk['ref_table']}.{fk['ref_col']}`"
        lines.append(
            f"| `{c['name']}` | {c['type']} | {', '.join(constraints) or '—'} | {ref or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_db_section(db_path: Path) -> str:
    con = sqlite3.connect(db_path)
    try:
        version = get_user_version(con)
        tables = list_tables(con)

        out = [f"### 📦 `{db_path.name}`", ""]
        out.append(f"- **Schema version (`PRAGMA user_version`):** {version}")
        out.append(f"- **Tables:** {len(tables)}")
        out.append("")

        if not tables:
            out.append("_No tables found — file may be empty or not yet initialized._")
            out.append("")
        else:
            for t in tables:
                out.append(render_table_section(con, t))

        return "\n".join(out)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate DB schema docs from live .db files")
    parser.add_argument("--data-dir", default="data", help="Directory containing .db files")
    parser.add_argument("--out", default="docs/DATABASE_SCHEMA.md", help="Output markdown path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    db_files = sorted(data_dir.glob("*.db"))
    # Skip dated/backup copies like "botatoin_2026_06_06.db" — only document
    # the live databases that cogs actually open at runtime.
    db_files = [p for p in db_files if not any(ch.isdigit() for ch in p.stem.split("_")[-1])]

    if not db_files:
        print(f"No .db files found in {data_dir}/")
        return

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = [
        "# PupPet — Database Schema Reference",
        "",
        f"_Auto-generated from live `.db` files on {generated_at}. "
        f"Do not edit by hand — re-run `tools/generate_schema_docs.py` instead._",
        "",
        "## Overview",
        "",
        "PupPet uses one SQLite file per feature area rather than a single shared "
        "database. Each file is owned by one cog, which is the only code allowed "
        "to write to it.",
        "",
        "| Database | Owning cog | Purpose |",
        "|---|---|---|",
        "| `moderation.db` | `cogs/puppy_moderation.py` | Mod cases, spam tracking, ghost pings, alt flags, mod tickets |",
        "| `birthdays.db` | `cogs/birthday.py` | Member birthdays and nicknames |",
        "| `backups.db` | `cogs/backup.py` | Server structure backup metadata |",
        "| `badwords.db` | `cogs/badwords.py` | Per-guild bad word list and filter settings |",
        "| `reaction_roles.db` | `cogs/reaction_roles.py` | Role panels, button/select roles, sticky/temp roles |",
        "| `puppycoin.db` | `cogs/puppycoin.py` | Puppycoin balances and economy metadata |",
        "",
    ]

    known_dbs = {
        "moderation.db", "birthdays.db", "backups.db",
        "badwords.db", "reaction_roles.db", "puppycoin.db",
    }
    orphans = [p for p in db_files if p.name not in known_dbs]
    if orphans:
        sections.append("## ⚠️ Orphaned / unreferenced database files")
        sections.append("")
        sections.append(
            "The following `.db` files exist in `data/` but are **not opened by any "
            "current cog**. They may be leftovers from a renamed feature, an old bot "
            "version, or a manual copy. Confirm before deleting — back them up first."
        )
        sections.append("")
        for p in orphans:
            sections.append(f"- `{p.name}`")
        sections.append("")

    sections.append("---")
    sections.append("")

    for db_path in db_files:
        sections.append(render_db_section(db_path))
        sections.append("---")
        sections.append("")

    out_path.write_text("\n".join(sections), encoding="utf-8")
    print(f"Wrote schema docs for {len(db_files)} database(s) to {out_path}")


if __name__ == "__main__":
    main()
