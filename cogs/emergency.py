"""
cogs/emergency.py
Emergency Lockdown Control System for PupPet.
Full persistent button panel — lock, unlock, status.

🐶 Powered by PupPet — The World's Fluffiest Bot Framework™
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("PupPet.cogs.emergency")

# ── Constants ─────────────────────────────────────────────────────────────────
EMERGENCY_ROLE_ID: int = 1551515669026177077
ANNOUNCEMENT_CHANNEL_ID: int = 1551515703952277580

PING_ROLE_IDS: list[int] = [
    1551515669026177077,
    1551515673174212638,
    1551515675825274961,
]

EMERGENCY_IMAGE: Path = Path(__file__).parent.parent / "assets" / "emergency.png"

# ── Colours ───────────────────────────────────────────────────────────────────
COL_ORANGE = 0xFF8C00
COL_RED    = 0xE53935
COL_GREEN  = 0x43A047
COL_GREY   = 0x607D8B

# ── Shared state (module-level so the View can reach it without a bot ref) ────
_emergency_active: bool = False


def _is_authorised(interaction: discord.Interaction) -> bool:
    """Return True if the member holds EMERGENCY_ROLE_ID or is super-admin."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.id == 1371080029265465406:
        return True
    return any(r.id == EMERGENCY_ROLE_ID for r in interaction.user.roles)


def _role_ping_str() -> str:
    return " ".join(f"<@&{rid}>" for rid in PING_ROLE_IDS)


def _unauthorised_embed() -> discord.Embed:
    return discord.Embed(
        title="❌ Unauthorized",
        description="You do not have permission to use this control.",
        color=COL_GREY,
    ).set_footer(text="🐶 PupPet Security Shield • Emergency Response System")


def _panel_embed(active: bool) -> discord.Embed:
    status_value = "🔴 Emergency Lockdown Active" if active else "🟢 Normal Operation"
    embed = discord.Embed(
        title="🚨 PUPPET EMERGENCY CONTROL CENTER",
        description=(
            "Server Protection & Emergency Management System\n\n"
            "⚠️ Authorized Personnel Only"
        ),
        color=COL_ORANGE,
    )
    embed.add_field(name="Current Status:", value=status_value, inline=False)
    embed.set_footer(text="🐶 PupPet Security Shield • Emergency Response System")
    return embed


def _lockdown_embed(triggered_by: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="🚨 EMERGENCY MODE ACTIVATED",
        description=(
            "The server has entered Emergency Lockdown Mode.\n\n"
            "🔒 All public channels have been locked.\n"
            "🔒 Staff are currently investigating the situation.\n"
            "🔒 Please wait for further instructions."
        ),
        color=COL_RED,
    )
    embed.add_field(name="Triggered By", value=triggered_by.mention, inline=True)
    embed.add_field(
        name="Timestamp",
        value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
        inline=True,
    )
    embed.set_image(url="attachment://emergency.png")
    embed.set_footer(text="🐶 PupPet Security Shield • Emergency Response System")
    return embed


def _unlock_embed(triggered_by: discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title="🔓 EMERGENCY MODE DEACTIVATED",
        description=(
            "Emergency lockdown has been lifted.\n\n"
            "✅ All channels restored\n"
            "✅ Server back to normal\n"
            "✅ Thank you for cooperation"
        ),
        color=COL_GREEN,
    )
    embed.add_field(name="Triggered By", value=triggered_by.mention, inline=True)
    embed.add_field(
        name="Timestamp",
        value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
        inline=True,
    )
    embed.set_footer(text="🐶 PupPet Security Shield • Emergency Response System")
    return embed


def _status_embed(guild: discord.Guild, active: bool) -> discord.Embed:
    state_str = "🔴 Emergency Lockdown Active" if active else "🟢 Normal Operation"
    embed = discord.Embed(
        title="📊 Emergency System Status",
        color=COL_RED if active else COL_GREEN,
    )
    embed.add_field(name="Current State", value=state_str, inline=False)
    embed.add_field(name="Guild Name", value=guild.name, inline=True)
    embed.add_field(name="Guild ID", value=str(guild.id), inline=True)
    embed.add_field(
        name="Timestamp",
        value=discord.utils.format_dt(discord.utils.utcnow(), style="F"),
        inline=False,
    )
    embed.set_footer(text="🐶 PupPet Security Shield • Emergency Response System")
    return embed


# ── Persistent View ────────────────────────────────────────────────────────────
class EmergencyPanelView(discord.ui.View):
    """
    Persistent control panel view.
    Registered via bot.add_view() at startup so buttons survive restarts.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _lock_channel(
        self,
        channel: discord.abc.GuildChannel,
        guild_default_role: discord.Role,
    ) -> None:
        """Apply lockdown overrides to a single channel."""
        try:
            if isinstance(channel, discord.TextChannel):
                await channel.set_permissions(
                    guild_default_role, send_messages=False
                )
            elif isinstance(channel, discord.VoiceChannel):
                await channel.set_permissions(
                    guild_default_role, connect=False
                )
            elif isinstance(channel, discord.StageChannel):
                await channel.set_permissions(
                    guild_default_role, connect=False
                )
            elif isinstance(channel, discord.ForumChannel):
                await channel.set_permissions(
                    guild_default_role, send_messages=False
                )
        except discord.Forbidden:
            log.warning("Lock: missing permissions for channel %s (%d)", channel.name, channel.id)
        except discord.HTTPException as exc:
            log.error("Lock: HTTP error on channel %s: %s", channel.name, exc)
        except Exception as exc:
            log.exception("Lock: unexpected error on channel %s: %s", channel.name, exc)

    async def _unlock_channel(
        self,
        channel: discord.abc.GuildChannel,
        guild_default_role: discord.Role,
    ) -> None:
        """Restore permission overrides for a single channel."""
        try:
            if isinstance(channel, discord.TextChannel):
                await channel.set_permissions(
                    guild_default_role, send_messages=None
                )
            elif isinstance(channel, discord.VoiceChannel):
                await channel.set_permissions(
                    guild_default_role, connect=None
                )
            elif isinstance(channel, discord.StageChannel):
                await channel.set_permissions(
                    guild_default_role, connect=None
                )
            elif isinstance(channel, discord.ForumChannel):
                await channel.set_permissions(
                    guild_default_role, send_messages=None
                )
        except discord.Forbidden:
            log.warning("Unlock: missing permissions for channel %s (%d)", channel.name, channel.id)
        except discord.HTTPException as exc:
            log.error("Unlock: HTTP error on channel %s: %s", channel.name, exc)
        except Exception as exc:
            log.exception("Unlock: unexpected error on channel %s: %s", channel.name, exc)

    # ── Button: Activate Lockdown ─────────────────────────────────────────────

    @discord.ui.button(
        label="🔒 Activate Lockdown",
        style=discord.ButtonStyle.danger,
        custom_id="emergency:activate",
    )
    async def activate_lockdown(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        global _emergency_active

        if not _is_authorised(interaction):
            log.warning(
                "Unauthorized activate attempt by %s (%d)",
                interaction.user, interaction.user.id,
            )
            await interaction.response.send_message(
                embed=_unauthorised_embed(), ephemeral=True
            )
            return

        if _emergency_active:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Already Active",
                    description="Emergency lockdown is already active.",
                    color=COL_GREY,
                ).set_footer(text="🐶 PupPet Security Shield • Emergency Response System"),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        default_role: discord.Role = guild.default_role
        locked: int = 0
        skipped: int = 0

        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            if channel.id == ANNOUNCEMENT_CHANNEL_ID:
                continue

            await self._lock_channel(channel, default_role)
            locked += 1

        _emergency_active = True
        log.info(
            "LOCKDOWN ACTIVATED by %s (%d) — %d channels locked, %d skipped.",
            interaction.user, interaction.user.id, locked, skipped,
        )

        # Announcement
        ann_channel: Optional[discord.TextChannel] = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)  # type: ignore[assignment]
        if ann_channel:
            try:
                file: Optional[discord.File] = None
                if EMERGENCY_IMAGE.exists():
                    file = discord.File(EMERGENCY_IMAGE, filename="emergency.png")
                else:
                    log.warning("Emergency image not found at %s", EMERGENCY_IMAGE)

                await ann_channel.send(
                    content=_role_ping_str(),
                    embed=_lockdown_embed(interaction.user),  # type: ignore[arg-type]
                    file=file,
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except Exception as exc:
                log.error("Failed to send lockdown announcement: %s", exc)
        else:
            log.warning("Announcement channel %d not found.", ANNOUNCEMENT_CHANNEL_ID)

        # Update panel embed
        try:
            await interaction.message.edit(embed=_panel_embed(active=True))  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("Could not update panel embed: %s", exc)

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Emergency Lockdown Activated",
                color=COL_GREEN,
            ).set_footer(text="🐶 PupPet Security Shield • Emergency Response System"),
            ephemeral=True,
        )

    # ── Button: Deactivate Lockdown ───────────────────────────────────────────

    @discord.ui.button(
        label="🔓 Deactivate Lockdown",
        style=discord.ButtonStyle.success,
        custom_id="emergency:deactivate",
    )
    async def deactivate_lockdown(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        global _emergency_active

        if not _is_authorised(interaction):
            log.warning(
                "Unauthorized deactivate attempt by %s (%d)",
                interaction.user, interaction.user.id,
            )
            await interaction.response.send_message(
                embed=_unauthorised_embed(), ephemeral=True
            )
            return

        if not _emergency_active:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Not Active",
                    description="There is no active emergency lockdown to deactivate.",
                    color=COL_GREY,
                ).set_footer(text="🐶 PupPet Security Shield • Emergency Response System"),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        default_role: discord.Role = guild.default_role
        restored: int = 0

        for channel in guild.channels:
            if isinstance(channel, discord.CategoryChannel):
                continue
            if channel.id == ANNOUNCEMENT_CHANNEL_ID:
                continue

            await self._unlock_channel(channel, default_role)
            restored += 1

        _emergency_active = False
        log.info(
            "LOCKDOWN DEACTIVATED by %s (%d) — %d channels restored.",
            interaction.user, interaction.user.id, restored,
        )

        # Announcement
        ann_channel: Optional[discord.TextChannel] = guild.get_channel(ANNOUNCEMENT_CHANNEL_ID)  # type: ignore[assignment]
        if ann_channel:
            try:
                await ann_channel.send(
                    content=_role_ping_str(),
                    embed=_unlock_embed(interaction.user),  # type: ignore[arg-type]
                    allowed_mentions=discord.AllowedMentions(roles=True),
                )
            except Exception as exc:
                log.error("Failed to send unlock announcement: %s", exc)
        else:
            log.warning("Announcement channel %d not found.", ANNOUNCEMENT_CHANNEL_ID)

        # Update panel embed
        try:
            await interaction.message.edit(embed=_panel_embed(active=False))  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("Could not update panel embed: %s", exc)

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Emergency Lockdown Removed",
                color=COL_GREEN,
            ).set_footer(text="🐶 PupPet Security Shield • Emergency Response System"),
            ephemeral=True,
        )

    # ── Button: Status ────────────────────────────────────────────────────────

    @discord.ui.button(
        label="📊 Status",
        style=discord.ButtonStyle.secondary,
        custom_id="emergency:status",
    )
    async def status(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not _is_authorised(interaction):
            log.warning(
                "Unauthorized status check by %s (%d)",
                interaction.user, interaction.user.id,
            )
            await interaction.response.send_message(
                embed=_unauthorised_embed(), ephemeral=True
            )
            return

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=_status_embed(guild, _emergency_active),
            ephemeral=True,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────
class Emergency(commands.Cog, name="Emergency"):
    """Emergency Lockdown Control System for PupPet."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /emergencypanel ───────────────────────────────────────────────────────

    @app_commands.command(
        name="emergencypanel",
        description="[EMERGENCY ROLE ONLY] Open the Emergency Lockdown Control Panel.",
    )
    async def emergencypanel(self, interaction: discord.Interaction) -> None:
        if not _is_authorised(interaction):
            log.warning(
                "Unauthorized /emergencypanel attempt by %s (%d)",
                interaction.user, interaction.user.id,
            )
            await interaction.response.send_message(
                embed=_unauthorised_embed(), ephemeral=True
            )
            return

        log.info(
            "/emergencypanel invoked by %s (%d) in guild %s (%d)",
            interaction.user, interaction.user.id,
            interaction.guild, interaction.guild_id,
        )

        # ── FIX: ephemeral confirmation to invoker, panel posted publicly ──
        await interaction.response.send_message(
            embed=discord.Embed(
                title="✅ Panel Sent",
                description="The emergency control panel has been posted.",
                color=COL_GREEN,
            ).set_footer(text="🐶 PupPet Security Shield • Emergency Response System"),
            ephemeral=True,
        )

        await interaction.channel.send(  # type: ignore[union-attr]
            embed=_panel_embed(active=_emergency_active),
            view=EmergencyPanelView(),
        )


# ── Setup ─────────────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Emergency(bot))
    log.info("Emergency cog loaded — persistent view registered.")