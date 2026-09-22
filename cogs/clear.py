"""
cogs/clear.py
Clear (purge) messages from a channel.
Command: /clear [amount]  — staff/admin only.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.helpers import error_embed, success_embed, is_staff, is_admin

log = logging.getLogger("PupPet.cogs.clear")

MAX_BULK = 100   # Discord API hard limit per bulk-delete call
DEFAULT  = 10    # default if the user doesn't specify an amount


class Clear(commands.Cog):
    """Message purge commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config

    # ──────────────────────────────────────────────────────────────────────────
    # /clear
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="clear",
        description="Delete a number of recent messages from this channel (staff only).",
    )
    @app_commands.describe(amount="Number of messages to delete (1–100, default 10).")
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, MAX_BULK] = DEFAULT,
    ) -> None:
        """Bulk-delete *amount* messages from the current channel."""

        member = interaction.user

        # ── Permission check ──────────────────────────────────────────────────
        if not (is_staff(member, self.config) or is_admin(member, self.config)):
            await interaction.response.send_message(
                embed=error_embed(
                    "No Permission",
                    "Only **staff** or **admins** can use `/clear`.",
                ),
                ephemeral=True,
            )
            return

        # ── Defer so Discord doesn't time-out while we purge ──────────────────
        await interaction.response.defer(ephemeral=True)

        try:
            deleted = await interaction.channel.purge(
                limit=amount,
                reason=f"/clear used by {member} ({member.id})",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Missing Permissions",
                    "I don't have **Manage Messages** permission in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("Purge failed in #%s: %s", interaction.channel, exc)
            await interaction.followup.send(
                embed=error_embed("Purge Failed", f"Discord returned an error: {exc}"),
                ephemeral=True,
            )
            return

        count = len(deleted)
        log.info(
            "%s used /clear in #%s — deleted %d message(s).",
            member,
            interaction.channel,
            count,
        )

        await interaction.followup.send(
            embed=success_embed(
                "Messages Cleared",
                f"Successfully deleted **{count}** message{'s' if count != 1 else ''}.",
            ),
            ephemeral=True,
        )


# ── Cog loader ────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Clear(bot))
