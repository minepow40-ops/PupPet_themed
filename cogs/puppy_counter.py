"""
cogs/membercounter.py
Member Count Cog — PupPet
Updates a voice channel name with the count of a specific role.

Channel : 1511807950870544576  →  "🐶 Puppies: 1,234"
Role    : 1511800497210789969
Updates : every 5 minutes only.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

log = logging.getLogger("PupPet.cogs.membercounter")

# ── Config ─────────────────────────────────────────────────────────────────────
COUNT_ROLE_ID    = 1551515675825274961
COUNT_CHANNEL_ID = 1551515697459232849
CHANNEL_TEMPLATE = "🐶 Puppies: {count:,}"


class MemberCounter(commands.Cog):
    """Role-member counter updated every 5 minutes."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_count: int = -1
        self.refresh_loop.start()

    def cog_unload(self) -> None:
        self.refresh_loop.cancel()

    def _get_role_count(self, guild: discord.Guild) -> int:
        role = guild.get_role(COUNT_ROLE_ID)
        if role is None:
            log.warning("[Counter] Role %s not found in '%s'.", COUNT_ROLE_ID, guild.name)
            return 0
        return len(role.members)

    async def _update_channel(self, guild: discord.Guild, *, force: bool = False) -> None:
        channel = guild.get_channel(COUNT_CHANNEL_ID)
        if channel is None:
            log.warning("[Counter] Channel %s not found in '%s'.", COUNT_CHANNEL_ID, guild.name)
            return

        count    = self._get_role_count(guild)
        new_name = CHANNEL_TEMPLATE.format(count=count)

        if not force and count == self._last_count:
            return

        try:
            await channel.edit(name=new_name, reason="Member counter update")
            log.info("[Counter] Updated → '%s'", new_name)
            self._last_count = count
        except discord.Forbidden:
            log.error("[Counter] Missing Manage Channels permission.")
        except discord.HTTPException as exc:
            log.error("[Counter] HTTP error: %s", exc)

    # ── 5-minute loop ──────────────────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def refresh_loop(self) -> None:
        for guild in self.bot.guilds:
            await self._update_channel(guild)

    @refresh_loop.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    # ── /countupdate (admin force refresh) ─────────────────────────────────────

    @app_commands.command(
        name="countupdate",
        description="Force-refresh the member counter right now (admin only).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def countupdate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._update_channel(interaction.guild, force=True)
        count = self._get_role_count(interaction.guild)
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Counter Updated",
                description=(
                    f"Channel → **{CHANNEL_TEMPLATE.format(count=count)}**\n"
                    f"Role members: `{count:,}`"
                ),
                color=0x2ECC71,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemberCounter(bot))
    log.info("[Counter] MemberCounter cog loaded.")
