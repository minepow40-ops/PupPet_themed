# 📩 PupPet Ticket Bot

A professional, production-ready Discord Ticket Bot built with **discord.py 2.x**.
Features a complete modular cog system, persistent views, HTML transcripts, staff analytics, rating system, auto-close, and a modern UI.

---

## ✨ Features

| Feature | Details |
|---|---|
| 🎫 **Ticket Categories** | Support, Report, Partnership, Purchase, Developer Help, Staff Application |
| 🔒 **One Ticket Rule** | Users can only have one active ticket at a time |
| ✋ **Claim / Unclaim** | Staff can claim and unclaim tickets with button UI |
| 🏷️ **Priority System** | Low / Medium / High — auto-renames channel (`high-support-username`) |
| ➕➖ **User Management** | Staff can add/remove users from any ticket via modal UI |
| 📝 **Internal Notes** | Staff-only notes invisible to the ticket owner |
| 📄 **HTML Transcripts** | Beautiful dark-mode HTML transcripts with avatars & attachments |
| ⭐ **Rating System** | 5-star DM rating prompt after ticket closure |
| 📊 **Staff Analytics** | Track claimed, closed, avg response time, avg rating |
| 🏆 **Leaderboard** | `/leaderboard` with 🥇🥈🥉 medals sorted by closed/claimed/rating |
| ⏰ **Auto-Response Timer** | `⏳ Waiting for Staff…` → `⚡ First Response: Xs` |
| 🤖 **Auto-Close** | 24h inactivity warning, 48h auto-close |
| 💾 **JSON Storage** | No external database required |
| 🔄 **Persistent Views** | Buttons survive bot restarts |
| ⚙️ **Config System** | Full `config.json` for all settings |

---

## 🗂️ Project Structure

```
bot/
├── main.py                  # Bot entry point
├── requirements.txt
├── .env.example
├── .gitignore
│
├── cogs/
│   ├── __init__.py
│   ├── tickets.py           # Core ticket system (open, close, priority)
│   ├── staff.py             # Claim, unclaim, add/remove user
│   ├── analytics.py         # /stats, /leaderboard
│   ├── ratings.py           # Rating submission, /ratings
│   └── autoclose.py         # 24h warn / 48h auto-close background task
│
├── views/
│   ├── __init__.py
│   ├── ticket_views.py      # TicketPanelView, TicketControlView, RatingView, all Modals
│   └── staff_views.py       # StaffNoteView
│
├── utils/
│   ├── __init__.py
│   ├── config.py            # Config loader with hot-reload
│   ├── data_manager.py      # Async JSON I/O with asyncio locks
│   └── helpers.py           # Embed builders, permissions, transcript generator, formatters
│
└── data/
    ├── config.json           # Bot configuration
    ├── tickets.json          # Active tickets
    ├── stats.json            # Staff analytics
    ├── ratings.json          # Ticket ratings
    └── transcripts/          # Auto-generated HTML transcripts
```

---

## 🚀 Setup

### 1. Prerequisites

- Python **3.10+**
- A Discord Bot with:
  - **Message Content Intent** enabled
  - **Server Members Intent** enabled
  - **Guilds Intent** enabled
  - Bot invited with `bot` and `applications.commands` scopes

### 2. Install

```bash
git clone https://github.com/yourrepo/puppet-ticket-bot
cd puppet-ticket-bot/bot
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:
```env
DISCORD_TOKEN=your_bot_token_here
```

### 4. Configure the Bot In-Server

Run `/setup` after inviting the bot:

```
/setup
  category: #Tickets (category channel)
  log_channel: #ticket-logs
  transcript_channel: #transcripts
  staff_role: @Staff
```

Or edit `data/config.json` directly.

### 5. Start

```bash
python main.py
```

---

## ⚙️ config.json Reference

```json
{
  "ticket_category_id": 1234567890,     // Category for new ticket channels
  "log_channel_id": 1234567890,         // Ticket event log channel
  "transcript_channel_id": 1234567890, // HTML transcript uploads
  "staff_roles": [1234567890],          // Role IDs with staff privileges
  "admin_roles": [],                    // Role IDs with admin privileges
  "ticket_settings": {
    "max_tickets_per_user": 1,
    "auto_close_warning_hours": 24,
    "auto_close_hours": 48,
    "transcript_enabled": true,
    "rating_enabled": true,
    "notes_enabled": true
  }
}
```

---

## 🤖 Slash Commands

| Command | Permission | Description |
|---|---|---|
| `/panel` | Administrator | Post the ticket panel embed |
| `/new [category]` | Everyone | Open a ticket directly |
| `/close` | Everyone (in ticket) | Close current ticket |
| `/setup` | Administrator | Configure the bot |
| `/addstaff [role]` | Administrator | Add a staff role |
| `/removestaff [role]` | Administrator | Remove a staff role |
| `/stats [member]` | Everyone | View staff analytics |
| `/leaderboard [sort_by]` | Everyone | Top staff leaderboard |
| `/ratings [member]` | Everyone | View staff ratings |

---

## 🎛️ Ticket Control Buttons

All buttons appear inside every ticket channel:

| Button | Access | Description |
|---|---|---|
| 🔒 Close Ticket | Everyone | Opens close confirmation modal |
| ✋ Claim | Staff | Claim the ticket |
| ↩️ Unclaim | Staff (claimer / admin) | Release claim |
| ➕ Add User | Staff | Add a user via modal |
| ➖ Remove User | Staff | Remove a user via modal |
| ✏️ Rename | Staff | Rename channel via modal |
| 📝 Add Note | Staff | Post a staff-only internal note |
| 🏷️ Priority Select | Staff | Set Low / Medium / High priority |

---

## 🏷️ Priority System

When priority is set, the channel is automatically renamed:

```
low-support-username
medium-report-playerxyz
high-purchase-johndoe
```

---

## 📄 HTML Transcript Example

Transcripts are styled dark-mode HTML files with:
- Discord-like message bubbles
- User avatars
- Staff vs. user colour coding
- Image attachments rendered inline
- File attachment links
- Embed content
- Full timestamps

---

## ⭐ Rating Flow

1. Staff closes a ticket
2. Ticket owner receives a DM with 5 star buttons
3. Owner clicks a rating
4. Rating is saved to `data/ratings.json`
5. Staff analytics updated immediately

---

## 📊 Leaderboard

```
/leaderboard sort_by:Tickets Closed
```

```
🏆 Staff Leaderboard
Sorted by Tickets Closed

🥇 JohnStaff
┣ 🔒 Closed: 42  ✋ Claimed: 50
┣ ⭐ Avg Rating: 4.8/5  ⚡ Avg Response: 3m 12s

🥈 SarahMod
┣ 🔒 Closed: 38  ✋ Claimed: 41
┣ ⭐ Avg Rating: 4.6/5  ⚡ Avg Response: 5m 44s

🥉 AlexHelper
...
```

---

## 🔄 Persistent Views

All views use `custom_id`-based persistence. On restart, the bot re-registers:
- `TicketPanelView`
- `TicketControlView`
- `RatingView`
- `StaffNoteView`

Buttons on existing messages continue to work immediately after restart.

---

## 📝 Logs

Logs are written to both stdout and `logs/bot.log`:

```
2024-01-15 12:00:00 [INFO] PupPet.main: Bot ready — logged in as PupPet Bot#1234 (ID: ...)
2024-01-15 12:01:00 [INFO] PupPet.cogs.tickets: Ticket #0001 opened by User#5678 in #support-user
2024-01-15 12:45:00 [INFO] PupPet.cogs.tickets: Ticket #0001 closed by Staff#9012.
```

---

## 🛡️ Security Notes

- Staff checks use role IDs from `config.json` — never rely on role names
- Channel permissions use Discord's native permission overwrites
- Internal notes are posted as embeds in the ticket channel; since users don't have `read_messages` after removal, notes remain invisible to them during closure

---

## 📦 Dependencies

```
discord.py>=2.3.2
python-dotenv>=1.0.0
aiofiles>=23.2.1
```

---

## 📜 License

MIT — free to use, modify, and distribute.

---

*PupPet Ticket Bot — Built for production.*
