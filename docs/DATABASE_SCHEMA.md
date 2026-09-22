# PupPet — Database Schema Reference

_Auto-generated from live `.db` files on 2026-06-17 07:26 UTC. Do not edit by hand — re-run `tools/generate_schema_docs.py` instead._

## Overview

PupPet uses one SQLite file per feature area rather than a single shared database. Each file is owned by one cog, which is the only code allowed to write to it.

| Database | Owning cog | Purpose |
|---|---|---|
| `moderation.db` | `cogs/puppy_moderation.py` | Mod cases, spam tracking, ghost pings, alt flags, mod tickets |
| `birthdays.db` | `cogs/birthday.py` | Member birthdays and nicknames |
| `backups.db` | `cogs/backup.py` | Server structure backup metadata |
| `badwords.db` | `cogs/badwords.py` | Per-guild bad word list and filter settings |
| `reaction_roles.db` | `cogs/reaction_roles.py` | Role panels, button/select roles, sticky/temp roles |
| `puppycoin.db` | `cogs/puppycoin.py` | Puppycoin balances and economy metadata |

## ⚠️ Orphaned / unreferenced database files

The following `.db` files exist in `data/` but are **not opened by any current cog**. They may be leftovers from a renamed feature, an old bot version, or a manual copy. Confirm before deleting — back them up first.

- `botatoin.db`

---

### 📦 `backups.db`

- **Schema version (`PRAGMA user_version`):** 1
- **Tables:** 2

#### `backup_metadata`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | NOT NULL | — |
| `backup_name` | TEXT | NOT NULL | — |
| `created_at` | TEXT | NOT NULL | — |
| `file_size` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `role_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `category_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `channel_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `emoji_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `sticker_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `automod_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `is_auto` | INTEGER | NOT NULL, DEFAULT 0 | — |

#### `backup_settings`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `auto_enabled` | INTEGER | NOT NULL, DEFAULT 1 | — |

---

### 📦 `badwords.db`

- **Schema version (`PRAGMA user_version`):** 1
- **Tables:** 2

#### `badword_settings`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `enabled` | INTEGER | DEFAULT 1 | — |
| `deleted_count` | INTEGER | DEFAULT 0 | — |

#### `badwords`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `guild_id` | INTEGER | NOT NULL | — |
| `word` | TEXT | NOT NULL | — |

---

### 📦 `birthdays.db`

- **Schema version (`PRAGMA user_version`):** 1
- **Tables:** 2

#### `birthday_nicknames`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `original_nickname` | TEXT | — | — |

#### `birthdays`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `year` | INTEGER | — | — |
| `month` | INTEGER | NOT NULL | — |
| `day` | INTEGER | NOT NULL | — |
| `age` | INTEGER | — | — |
| `last_announced_year` | INTEGER | — | — |
| `created_at` | TEXT | NOT NULL | — |

---

### 📦 `botatoin.db`

- **Schema version (`PRAGMA user_version`):** 0
- **Tables:** 2

#### `botatoin_meta`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `key` | TEXT | PRIMARY KEY | — |
| `value` | TEXT | — | — |

#### `botatoin_users`  (2 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `total_coins` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `daily_earned` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `daily_paid` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `message_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `messages_since_reward` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `last_message_time` | REAL | NOT NULL, DEFAULT 0 | — |
| `last_message_content` | TEXT | NOT NULL, DEFAULT '' | — |
| `last_reset` | REAL | NOT NULL, DEFAULT 0 | — |

---

### 📦 `moderation.db`

- **Schema version (`PRAGMA user_version`):** 1
- **Tables:** 8

#### `active_channel_mutes`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `channel_id` | INTEGER | PRIMARY KEY | — |
| `expires` | TEXT | NOT NULL | — |

#### `alt_flags`  (2 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `user_id` | INTEGER | NOT NULL | — |
| `guild_id` | INTEGER | NOT NULL | — |
| `reason` | TEXT | NOT NULL | — |
| `timestamp` | TEXT | NOT NULL | — |

#### `cases`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `user_id` | INTEGER | NOT NULL | — |
| `moderator_id` | INTEGER | — | — |
| `action` | TEXT | NOT NULL | — |
| `reason` | TEXT | — | — |
| `timestamp` | TEXT | NOT NULL | — |

#### `ghost_ping_log`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `user_id` | INTEGER | NOT NULL | — |
| `channel_id` | INTEGER | NOT NULL | — |
| `mentioned` | TEXT | NOT NULL | — |
| `timestamp` | TEXT | NOT NULL | — |

#### `spam_counter`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `count` | INTEGER | NOT NULL, DEFAULT 0 | — |

#### `staff_audit_log`  (3 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `staff_id` | INTEGER | NOT NULL | — |
| `staff_tag` | TEXT | NOT NULL | — |
| `action` | TEXT | NOT NULL | — |
| `target_id` | INTEGER | — | — |
| `target_tag` | TEXT | — | — |
| `reason` | TEXT | — | — |
| `guild_id` | INTEGER | NOT NULL | — |
| `timestamp` | TEXT | NOT NULL | — |

#### `tickets`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `user_id` | INTEGER | NOT NULL | — |
| `guild_id` | INTEGER | NOT NULL | — |
| `channel_id` | INTEGER | NOT NULL | — |
| `status` | TEXT | NOT NULL, DEFAULT 'open' | — |
| `opened_at` | TEXT | NOT NULL | — |
| `closed_at` | TEXT | — | — |
| `case_id` | INTEGER | — | — |

#### `timeout_roles`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `role_ids` | TEXT | NOT NULL | — |

---

### 📦 `puppycoin.db`

- **Schema version (`PRAGMA user_version`):** 1
- **Tables:** 2

#### `puppycoin_meta`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `key` | TEXT | PRIMARY KEY | — |
| `value` | TEXT | — | — |

#### `puppycoin_users`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `user_id` | INTEGER | PRIMARY KEY | — |
| `total_coins` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `daily_earned` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `daily_paid` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `message_count` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `messages_since_reward` | INTEGER | NOT NULL, DEFAULT 0 | — |
| `last_message_time` | REAL | NOT NULL, DEFAULT 0 | — |
| `last_message_content` | TEXT | NOT NULL, DEFAULT '' | — |
| `last_reset` | REAL | NOT NULL, DEFAULT 0 | — |

---

### 📦 `reaction_roles.db`

- **Schema version (`PRAGMA user_version`):** 1
- **Tables:** 6

#### `role_items`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `panel_id` | INTEGER | NOT NULL | `role_panels.panel_id` |
| `role_id` | INTEGER | NOT NULL | — |
| `emoji` | TEXT | — | — |
| `label` | TEXT | NOT NULL | — |
| `description` | TEXT | NOT NULL, DEFAULT '' | — |
| `style` | INTEGER | NOT NULL, DEFAULT 1 | — |
| `group_name` | TEXT | — | — |
| `required_role_id` | INTEGER | — | — |
| `blacklisted_role_id` | INTEGER | — | — |
| `temp_duration` | INTEGER | — | — |
| `position` | INTEGER | NOT NULL, DEFAULT 0 | — |

#### `role_logs`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | NOT NULL | — |
| `user_id` | INTEGER | NOT NULL | — |
| `role_id` | INTEGER | NOT NULL | — |
| `action` | TEXT | NOT NULL | — |
| `timestamp` | TEXT | NOT NULL | — |

#### `role_panels`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `panel_id` | INTEGER | PRIMARY KEY | — |
| `guild_id` | INTEGER | NOT NULL | — |
| `channel_id` | INTEGER | NOT NULL | — |
| `message_id` | INTEGER | — | — |
| `title` | TEXT | NOT NULL | — |
| `description` | TEXT | NOT NULL, DEFAULT '' | — |
| `panel_type` | TEXT | NOT NULL, DEFAULT 'button' | — |
| `created_by` | INTEGER | NOT NULL | — |
| `created_at` | TEXT | NOT NULL | — |

#### `role_settings`  (1 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `log_channel_id` | INTEGER | — | — |
| `sticky_enabled` | INTEGER | NOT NULL, DEFAULT 0 | — |

#### `sticky_roles`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `user_id` | INTEGER | PRIMARY KEY | — |
| `role_id` | INTEGER | PRIMARY KEY | — |

#### `temp_roles`  (0 rows)

| Column | Type | Constraints | References |
|---|---|---|---|
| `guild_id` | INTEGER | PRIMARY KEY | — |
| `user_id` | INTEGER | PRIMARY KEY | — |
| `role_id` | INTEGER | PRIMARY KEY | — |
| `expires_at` | TEXT | NOT NULL | — |

---
