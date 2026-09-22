"""
cogs/help.py
Interactive /help command — paginated embed with all bot commands.
Usable by everyone. No permissions required.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("PupPet.cogs.help")

# ── Colour palette ────────────────────────────────────────────────────────────
COL_TICKET    = 0x5865F2   # blurple
COL_STAFF     = 0x2ECC71   # green
COL_MOD       = 0xE74C3C   # red
COL_ANALYTICS = 0x9B59B6   # purple
COL_UTIL      = 0xF39C12   # orange
COL_AUTO      = 0x1ABC9C   # teal

# ── Page definitions ──────────────────────────────────────────────────────────
# Each page is (title, colour, description_header, [(name, value, inline), ...])

PAGES: list[tuple[str, int, str, list[tuple[str, str, bool]]]] = [
    # ── PAGE 0 — Overview ────────────────────────────────────────────────────
    (
        "🐶 PupPet — Command Overview",
        COL_TICKET,
        (
            "Welcome to **PupPet**! Use the buttons below to browse all commands.\n\n"
            "**Navigation**\n"
            "◀ ▶ to flip pages  •  ✖ to close\n\n"
            "**Pages**"
        ),
        [
            ("📄 Page 1", "🎫 Ticket Commands", True),
            ("📄 Page 2", "🛠️ Staff Ticket Actions", True),
            ("📄 Page 3", "🐶 Moderation Commands", True),
            ("📄 Page 4", "📊 Analytics & Ratings", True),
            ("📄 Page 5", "🔧 Utility & Misc", True),
            ("📄 Page 6", "🤖 Auto-Mod Features", True),
            ("📄 Page 7", "🪙 Puppycoin Economy", True),
        ],
    ),
    # ── PAGE 1 — Ticket Commands ──────────────────────────────────────────────
    (
        "🎫 Ticket Commands",
        COL_TICKET,
        "Commands for opening and managing support tickets.",
        [
            (
                "</panel> 🔒 Admin",
                "Post the ticket panel with category selector in the current channel.\n"
                "*Anyone can open a ticket from the panel.*",
                False,
            ),
            (
                "</new> 👤 Everyone",
                "Open a ticket directly without using the panel.\n"
                "`/new [category]` — choose from: Support, Report, Partnership, Purchase, Dev Help, Staff Application.",
                False,
            ),
            (
                "</close> 👤 Everyone (in ticket)",
                "Close the current ticket. A modal will ask for a reason.\n"
                "*Transcript is saved automatically.*",
                False,
            ),
            (
                "</setup> 🔒 Admin",
                "Configure the bot:\n"
                "`category` — where ticket channels are created\n"
                "`log_channel` — where events are logged\n"
                "`transcript_channel` — where HTML transcripts are saved\n"
                "`staff_role` — role granted access to all tickets",
                False,
            ),
            (
                "📋 Intake Forms",
                "Every ticket category shows a short intake form asking relevant questions "
                "before the channel is created.",
                False,
            ),
        ],
    ),
    # ── PAGE 2 — Staff Ticket Actions ────────────────────────────────────────
    (
        "🛠️ Staff Ticket Actions",
        COL_STAFF,
        "Buttons available inside every ticket channel (staff only unless noted).",
        [
            (
                "✋ Claim  •  ↩️ Unclaim",
                "**Claim** — assign yourself to a ticket (tracks your stats).\n"
                "**Unclaim** — release ownership so another staff can take over.",
                False,
            ),
            (
                "🏷️ Set Priority",
                "Dropdown inside the ticket channel.\n"
                "🟢 Low  •  🟡 Medium  •  🔴 High\n"
                "*Renames the channel to reflect priority level.*",
                False,
            ),
            (
                "➕ Add User  •  ➖ Remove User",
                "Grant or revoke channel access for any server member by User ID.",
                False,
            ),
            (
                "✏️ Rename",
                "Rename the ticket channel (letters, numbers, hyphens only).",
                False,
            ),
            (
                "📝 Add Note",
                "Post an internal staff note inside the ticket.\n"
                "*Only staff can see and delete notes.*",
                False,
            ),
            (
                "</addstaff> & </removestaff> 🔒 Admin",
                "Add or remove a Discord role from the bot's staff role list.\n"
                "`/addstaff [role]`  •  `/removestaff [role]`",
                False,
            ),
        ],
    ),
    # ── PAGE 3 — Moderation ───────────────────────────────────────────────────
    (
        "🐶 Moderation Commands",
        COL_MOD,
        "Admin-only moderation tools. Requires the Owner / Admin role.",
        [
            (
                "</mod> 🔒 Admin",
                "Opens the **Puppy Justice Panel** for a member.\n"
                "Contains: Ban • Kick • Freeze (timeout) • Unfreeze • Unban • "
                "Backed Puppy role • Remove Backed Puppy • User Info • Case History • Open Ticket",
                False,
            ),
            (
                "</cases> 🔒 Admin",
                "`/cases [user]` — View the full moderation case history for a user.",
                False,
            ),
            (
                "</clearcases> 🔒 Admin",
                "`/clearcases [user]` — Clear all moderation cases for a user.\n"
                "*Requires confirmation.*",
                False,
            ),
            (
                "</puppycheck> 🔒 Admin",
                "`/puppycheck [user]` — Full risk report: case history, alt flags, active channel mutes.",
                False,
            ),
            (
                "</exporthistory> 🔒 Admin",
                "`/exporthistory [user] [format]` — Export a user's full history as `.txt` or `.html`.",
                False,
            ),
            (
                "</staffaudit> 🔒 Admin",
                "`/staffaudit [limit]` — View recent staff action audit log entries (default 20, max 50).",
                False,
            ),
            (
                "</exporthistory> 🔒 Admin",
                "`/exporthistory [user] [format]` — Export a user's full case history as a downloadable `.txt` or styled `.html` file.",
                False,
            ),
            (
                "</modhelp> 🔒 Admin",
                "Paginated guide to all moderation commands and auto-mod rules.",
                False,
            ),
        ],
    ),
    # ── PAGE 4 — Analytics & Ratings ─────────────────────────────────────────
    (
        "📊 Analytics & Ratings",
        COL_ANALYTICS,
        "Track staff performance and ticket satisfaction.",
        [
            (
                "</stats> 👤 Everyone",
                "`/stats [member]` — View stats for a staff member (or yourself).\n"
                "Shows: Tickets Claimed, Tickets Closed, Avg First Response Time, Avg Rating.",
                False,
            ),
            (
                "</leaderboard> 👤 Everyone",
                "`/leaderboard [sort_by]` — Top 10 staff leaderboard.\n"
                "Sort by: **Tickets Closed** (default) • **Tickets Claimed** • **Average Rating**",
                False,
            ),
            (
                "</ratings> 👤 Everyone",
                "`/ratings [member]` — View star rating distribution for a staff member.\n"
                "Shows overall score, total count, and per-star breakdown.",
                False,
            ),
            (
                "⭐ Rating System",
                "When a ticket is closed, the ticket owner receives a DM with rating buttons (1–5 ⭐).\n"
                "Ratings are tied to the staff member who claimed the ticket.",
                False,
            ),
        ],
    ),
    # ── PAGE 5 — Utility ──────────────────────────────────────────────────────
    (
        "🔧 Utility Commands",
        COL_UTIL,
        "General utility and configuration commands.",
        [
            (
                "</clear> 🛡️ Staff / Admin",
                "`/clear [amount]` — Bulk-delete 1–100 messages from the current channel.\n"
                "Default: 10 messages.",
                False,
            ),
            (
                "</countupdate> 🔒 Admin",
                "Force-refresh the 🐶 **Puppies** member counter voice channel immediately.\n"
                "*Normally auto-updates every 5 minutes.*",
                False,
            ),
            (
                "</welcome-test> 🔒 Manage Server",
                "Preview the welcome card with the PupPet banner in the current channel.\n"
                "*Shows exactly what new members see when they join.*",
                False,
            ),
            (
                "</help> 👤 Everyone",
                "You're looking at it! Shows this paginated command guide.",
                False,
            ),
        ],
    ),
    # ── PAGE 6 — Auto-Mod ─────────────────────────────────────────────────────
    (
        "🤖 Auto-Mod & Passive Features",
        COL_AUTO,
        "These features run automatically — no command needed.",
        [
            (
                "🎉 Welcome System",
                "New members receive: auto-role assignment, welcome embed with banner in the welcome channel, and a join-log entry.",
                False,
            ),
            (
                "⏰ AFK Auto-Close",
                "Tickets inactive for **7 minutes** get a warning ping.\n"
                "After **10 minutes** of silence, the ticket is force-closed with a transcript saved.",
                False,
            ),
            (
                "📆 Long-Inactivity Auto-Close",
                "Tickets inactive for **24 hours** receive a warning.\n"
                "After **48 hours** the ticket is automatically closed.",
                False,
            ),
            (
                "🚫 Spam Detection",
                "5+ messages in 5 seconds → channel mute (30s).\n"
                "Spam in 3+ channels → server timeout (5 min).",
                False,
            ),
            (
                "🔇 Content Filters",
                "Auto-deletes: excessive CAPS (>90% of ≥10-char messages), emoji floods (20+), "
                "mass mentions (5+), @everyone/@here abuse, bad words, Discord invite links.",
                False,
            ),
            (
                "📺 YouTube Channel",
                "Dedicated channel enforces YouTube links only — non-YouTube content is deleted and the sender is timed out for 3 minutes.",
                False,
            ),
            (
                "👻 Ghost Ping Detection",
                "Logs ghost pings (messages with mentions that are deleted) to the mod log channel.",
                False,
            ),
            (
                "🆕 Alt Account Detection",
                "New accounts younger than 30 days or that joined recently are automatically flagged in the mod log.",
                False,
            ),
            (
                "🐶 Member Counter",
                "A voice channel displays the live count of members with the 🐶 Puppies role, updated every 5 minutes.",
                False,
            ),
        ],
    ),
    # ── PAGE 7 — Puppycoin Economy ─────────────────────────────────────────────
    (
        "🪙 Puppycoin Economy",
        0xF1C40F,
        "Earn and spend **Puppycoins** 🦴 — the server's puppy currency.\n"
        "*Only members with the 🐶 Puppies role can earn coins.*",
        [
            (
                "</puppycoin> 👤 Everyone",
                "View your Puppycoin wallet.\n"
                "Shows: Total Coins, Global Rank, Earned Today (x/10), Transferred Today (x/5), "
                "Messages Counted, Next Reward, and Daily Reset timer.\n"
                "`/puppycoin [member]` — optionally look up another member's wallet.",
                False,
            ),
            (
                "</puppycoinlb> 👤 Everyone",
                "View the **Top 3** Puppycoin holders on the server.\n"
                "Displays: 🥇🥈🥉 with total coins and earned today.",
                False,
            ),
            (
                "</paypuppycoin> 👤 Everyone",
                "Send Puppycoins to another member.\n"
                "`/paypuppycoin [user] [amount]`\n"
                "• Cannot pay yourself or bots\n"
                f"• Max **5** coins transferred per day (resets every 24 h)\n"
                "• Must have enough coins in your wallet",
                False,
            ),
            (
                "💬 How to Earn",
                f"Chat in any channel while holding the 🐶 Puppies role.\n"
                f"Every **100** counted messages → **+2 Puppycoins** 🦴\n"
                f"Daily earning cap: **10 Puppycoins** (resets every 24 h).\n\n"
                "**Rules for a message to count:**\n"
                "• Not a duplicate of your previous message\n"
                "• At least 10 seconds since your last counted message\n"
                "• Message must not be empty",
                False,
            ),
            (
                "👑 Rich Puppy Role",
                "The member with the **most total Puppycoins** (all-time lifetime) "
                "automatically holds the <@&1551515674646683711> role — **one person only**.\n\n"
                "**Requirements:**\n"
                "• Must have **at least 50 Puppycoins** total to qualify\n"
                "• Must be the **#1 richest** user in the server\n\n"
                "**Behaviour:**\n"
                "• Role transfers automatically when someone overtakes the current holder\n"
                "• If nobody has 50+ coins, the role is removed from everyone\n"
                "• Updated on every coin earn, every `/paypuppycoin`, and every 5 minutes",
                False,
            ),
            (
                "🏆 Daily Leaderboard",
                "Every 24 hours a leaderboard is automatically posted showing the top-5 daily earners.",
                False,
            ),
        ],
    ),
]


# ── View with navigation buttons ──────────────────────────────────────────────

class HelpView(discord.ui.View):
    """Paginated help view with prev/next/close buttons."""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=120)
        self.page      = 0
        self.author_id = author_id
        self._update_buttons()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _update_buttons(self) -> None:
        self.btn_prev.disabled = (self.page == 0)
        self.btn_next.disabled = (self.page == len(PAGES) - 1)
        self.btn_page.label    = f"{self.page + 1} / {len(PAGES)}"

    def build_embed(self) -> discord.Embed:
        title, colour, header, fields = PAGES[self.page]
        embed = discord.Embed(
            title=title,
            description=header,
            color=colour,
            timestamp=discord.utils.utcnow(),
        )
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        embed.set_footer(
            text=f"PupPet Help  •  Page {self.page + 1}/{len(PAGES)}  •  Use ◀ ▶ to navigate"
        )
        return embed

    async def _update(self, interaction: discord.Interaction) -> None:
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    # ── Interaction guard: only the original user can navigate ────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/help` can navigate this menu.", ephemeral=True
            )
            return False
        return True

    # ── Buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="help_prev")
    async def btn_prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    @discord.ui.button(label="1 / 8", style=discord.ButtonStyle.primary, disabled=True, custom_id="help_page")
    async def btn_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        pass  # display-only counter button

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="help_next")
    async def btn_next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(len(PAGES) - 1, self.page + 1)
        await self._update(interaction)

    @discord.ui.button(label="✖ Close", style=discord.ButtonStyle.danger, custom_id="help_close")
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


# ── Cog ───────────────────────────────────────────────────────────────────────

class Help(commands.Cog):
    """Interactive /help command — available to everyone."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="📖 Browse all PupPet commands and features.")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        view  = HelpView(author_id=interaction.user.id)
        embed = view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        log.info("/help used by %s", interaction.user)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
