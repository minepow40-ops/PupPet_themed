"""
PupPet Ticket Bot — main.py
Entry point: loads cogs, registers persistent views, starts the bot.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# ── SSL FIX (IMPORTANT) ─────────────────────────────────────────
import certifi
os.environ["SSL_CERT_FILE"] = certifi.where()

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ── Environment ─────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    sys.exit("ERROR: DISCORD_TOKEN not set in environment / .env file.")

# ── Logging ─────────────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)

log = logging.getLogger("PupPet.main")

# ── Intents ─────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


# ── Bot Class ───────────────────────────────────────────────────
class PupPetBot(commands.Bot):
    """Custom Bot class with startup lifecycle management."""

    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.log = logging.getLogger("PupPet.bot")

    # ── Startup Hook ─────────────────────────────────────────────
    async def setup_hook(self) -> None:
        """Load cogs + register persistent views"""

        # Views
        from views.ticket_views import (
            TicketPanelView,
            TicketControlView,
            RatingView,
        )
        from views.staff_views import StaffNoteView
        from cogs.emergency import EmergencyPanelView
        from cogs.backup import BackupPanelView

        # Utils
        from utils.config import Config
        from utils.data_manager import DataManager

        self.config = Config()
        self.data = DataManager()

        # Persistent Views
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        self.add_view(RatingView())
        self.add_view(StaffNoteView())
        self.add_view(EmergencyPanelView())
        self.add_view(BackupPanelView())

        self.log.info("Persistent views registered.")

        # Cogs
        cogs = [
            "cogs.tickets",
            "cogs.staff",
            "cogs.analytics",
            "cogs.ratings",
            "cogs.autoclose",
            "cogs.welcome",
            "cogs.clear",
            "cogs.puppy_counter",
            "cogs.puppy_moderation",
            "cogs.help",
            "cogs.puppycoin",
            "cogs.dm",
            "cogs.birthday",
            "cogs.badwords",
            "cogs.emergency",
            "cogs.bot_control",
            "cogs.reaction_roles",
            "cogs.backup",
            "cogs.rerolepanel",
            "cogs.webhelp",
        ]

        for cog in cogs:
            try:
                await self.load_extension(cog)
                self.log.info(f"Loaded cog: {cog}")
            except Exception as exc:
                self.log.error(f"Failed to load cog {cog}: {exc}", exc_info=True)

        # Slash command sync
        try:
            synced = await self.tree.sync()
            self.log.info("Synced %d slash command(s).", len(synced))
        except Exception as exc:
            self.log.error(f"Command sync failed: {exc}", exc_info=True)

    # ── Ready Event ──────────────────────────────────────────────
    async def on_ready(self):
        self.log.info(f"Bot ready as {self.user} (ID: {self.user.id})")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="📩 PupPet Support System",
            )
        )

    # ── Error Handler ────────────────────────────────────────────
    async def on_command_error(self, ctx, error):
        self.log.warning(f"Command error: {error}")

    async def invoke(self, ctx):
        """Restrict admin commands to a specific super-admin user and admin roles."""
        SUPER_ADMIN_ID = 1371080029265465406
        ADMIN_ROLE_IDS = {1551515669026177077, 1551515670553042995, 1551515673174212638, 1551515673702703147}

        # List of command names that are considered administrative
        ADMIN_COMMANDS = [
            "op", "clear", "backup", "reload", "setup",
            "ban", "kick", "mute", "unmute", "warn", "purge"
        ]

        is_super_admin = ctx.author.id == SUPER_ADMIN_ID
        has_admin_role = any(role.id in ADMIN_ROLE_IDS for role in ctx.author.roles)

        if not (is_super_admin or has_admin_role):
            if ctx.command and ctx.command.name in ADMIN_COMMANDS:
                await ctx.send("❌ Only the super administrator or authorized admin roles can use this command.")
                return

        await super().invoke(ctx)


# ── Runner ──────────────────────────────────────────────────────
async def main():
    bot = PupPetBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot shut down by user.")