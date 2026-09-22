"""
cogs/welcome.py — PupPetGANG Welcome System
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

try:
    from PIL import Image
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("welcome")

# ── Constants ─────────────────────────────────────────────────────────────────
AUTO_ROLE_ID       = 1551515675825274961
WELCOME_CHANNEL_ID = 1551515701053755412  # dedicated welcome channel
JOIN_LOG_CH_ID     = 1551515686440804424  # join-log channel (same channel)

BANNER_PATH = Path(__file__).parent.parent / "assets" / "puppet_banner.png"

EMBED_COLOR    = 0xFFC107   # yellow/orange
CARD_W, CARD_H = 602,350


# ═══════════════════════════════════════════════════════════════════════════════
# Banner loader
# ═══════════════════════════════════════════════════════════════════════════════

async def load_banner() -> Optional[io.BytesIO]:
    """
    Opens assets/puppet_banner.png, resizes to 1100x400, returns as PNG BytesIO.
    Returns None if Pillow is missing or the file does not exist.
    """
    if not PILLOW_OK:
        log.warning("[Banner] Pillow not installed — banner skipped.")
        return None

    if not BANNER_PATH.exists():
        log.warning("[Banner] File not found: %s", BANNER_PATH)
        return None

    try:
        img = Image.open(BANNER_PATH).convert("RGB")
        img = img.resize((CARD_W, CARD_H), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        log.debug("[Banner] Loaded successfully (%dx%d).", CARD_W, CARD_H)
        return buf
    except Exception as exc:
        log.error("[Banner] Failed to load: %s", exc, exc_info=True)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Cog
# ═══════════════════════════════════════════════════════════════════════════════

class Welcome(commands.Cog):
    """Auto-role, welcome embed with banner, join logging, /welcome-test."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_welcome_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Returns the dedicated welcome channel, or None with a log warning."""
        channel = guild.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            log.warning(
                "[Welcome] Dedicated channel %s not found in guild '%s'. "
                "Check WELCOME_CHANNEL_ID or bot permissions.",
                WELCOME_CHANNEL_ID, guild.name,
            )
        return channel  # type: ignore[return-value]

    async def _assign_auto_role(self, member: discord.Member) -> None:
        """Assigns AUTO_ROLE_ID to the new member."""
        role = member.guild.get_role(AUTO_ROLE_ID)
        if role is None:
            log.error("[AutoRole] ❌ Role %s not found in '%s'.", AUTO_ROLE_ID, member.guild.name)
            return
        try:
            await member.add_roles(role, reason="PupPetGANG auto-role on join")
            log.info("[AutoRole] ✅ Assigned to %s (%s).", member.display_name, member.id)
        except discord.Forbidden:
            log.error("[AutoRole] ❌ Missing permission to assign role to %s.", member)
        except discord.HTTPException as exc:
            log.error("[AutoRole] ❌ HTTPException: %s", exc)

    def _build_welcome_embed(self, member: discord.Member) -> discord.Embed:
        """Builds the welcome embed (avatar thumbnail + description + footer)."""
        embed = discord.Embed(
            color=EMBED_COLOR,
            timestamp=datetime.now(timezone.utc),
        )
        embed.description = (
            f"🐶 Welcome {member.mention}\n"
            "🔥 You just joined the official **PuppyGANG**!\n\n"
            "🌱 Fresh puppy status: ACTIVE\n"
            "🦴 Good boy status: PENDING\n\n"
            "🐾 *Born a Puppy ;)*"
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(
            
        )
        # NOTE: set_image is applied in _send_welcome / cmd_welcome_test
        # after the banner file is prepared, so filename always matches.
        return embed

    async def _send_welcome(self, member: discord.Member) -> None:
        """
        Sends the welcome embed + banner to WELCOME_CHANNEL_ID.
        """
        channel = self._get_welcome_channel(member.guild)
        if channel is None:
            return  # warning already logged in _get_welcome_channel

        embed      = self._build_welcome_embed(member)
        banner_buf = await load_banner()

        try:
            if banner_buf:
                # filename must match the attachment:// URL exactly
                banner_file = discord.File(banner_buf, filename="welcome.png")
                embed.set_image(url="attachment://welcome.png")
                await channel.send(file=banner_file, embed=embed)
            else:
                # Fallback — send embed without banner
                await channel.send(embed=embed)
            log.info("[Welcome] ✅ Sent for %s (%s).", member.display_name, member.id)
        except discord.Forbidden:
            log.error("[Welcome] ❌ Missing permission to send in channel %s.", channel.id)
        except discord.HTTPException as exc:
            log.error("[Welcome] ❌ HTTPException: %s", exc)
        except Exception as exc:
            log.error("[Welcome] ❌ Unexpected error: %s", exc, exc_info=True)

    async def _send_join_log(self, member: discord.Member) -> None:
        """Sends a structured join-log embed to JOIN_LOG_CH_ID."""
        if not JOIN_LOG_CH_ID:
            return

        channel = member.guild.get_channel(JOIN_LOG_CH_ID)
        if channel is None:
            log.warning("[JoinLog] Channel %s not found in '%s'.", JOIN_LOG_CH_ID, member.guild.name)
            return

        embed = discord.Embed(
            title="📥 Member Joined",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(
            name="User",
            value=f"{member.mention} (`{member.id}`)",
            inline=False,
        )
        embed.add_field(
            name="Account Created",
            value=discord.utils.format_dt(member.created_at, style="R"),
            inline=True,
        )
        embed.add_field(
            name="Member Count",
            value=str(member.guild.member_count),
            inline=True,
        )
        embed.set_footer(text=f"Guild: {member.guild.name}")

        try:
            await channel.send(embed=embed)
            log.info("[JoinLog] ✅ Logged %s (%s).", member.display_name, member.id)
        except discord.Forbidden:
            log.error("[JoinLog] ❌ Missing permission to send in channel %s.", JOIN_LOG_CH_ID)
        except discord.HTTPException as exc:
            log.error("[JoinLog] ❌ HTTPException: %s", exc)

    # ── Events ────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        log.info(
            "[Event] Joined: %s (%s) | Guild: %s",
            member.display_name, member.id, member.guild.name,
        )
        await self._assign_auto_role(member)
        await self._send_welcome(member)
        await self._send_join_log(member)

    # ── Slash Commands ────────────────────────────────────────────────────────

    @app_commands.command(name="welcome-test", description="Preview the welcome card 🐶")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_welcome_test(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        if interaction.guild is None:
            await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
            return

        embed      = self._build_welcome_embed(interaction.user)  # type: ignore[arg-type]
        banner_buf = await load_banner()

        if banner_buf:
            banner_file = discord.File(banner_buf, filename="welcome.png")
            embed.set_image(url="attachment://welcome.png")
            await interaction.followup.send(file=banner_file, embed=embed)
        else:
            await interaction.followup.send(embed=embed)

        log.info("[Cmd] /welcome-test used by %s.", interaction.user)

    @cmd_welcome_test.error
    async def _welcome_test_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ You need **Manage Server** permission to use this command."
        else:
            log.error("[Cmd] /welcome-test error: %s", error, exc_info=True)
            msg = "❌ Something went wrong. Please try again."
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(msg, ephemeral=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Setup
# ═══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
    log.info("[Welcome] Cog registered.")
