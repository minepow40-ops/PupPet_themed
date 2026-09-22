"""
cogs/analytics.py
Staff analytics and leaderboard.
Slash commands: /stats, /leaderboard
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.data_manager import DataManager
from utils.helpers import COL_MAIN, COL_PURPLE, format_duration, stars

log = logging.getLogger("PupPet.cogs.analytics")

MEDAL = ["🥇", "🥈", "🥉"]


class Analytics(commands.Cog):
    """Staff analytics system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self.data: DataManager = bot.data

    # ──────────────────────────────────────────────────────────────────────────
    # /stats
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="stats", description="View ticket stats for a staff member.")
    @app_commands.describe(member="The staff member to view (leave blank for yourself)")
    async def stats_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        data   = await self.data.get_staff_stats(target.id)

        claimed  = data.get("claimed", 0)
        closed   = data.get("closed", 0)
        rc       = data.get("response_count", 0)
        rt       = data.get("total_response_time", 0)
        avg_rt   = (rt / rc) if rc else 0

        rat_count = data.get("rating_count", 0)
        rat_total = data.get("total_rating", 0)
        avg_rat   = (rat_total / rat_count) if rat_count else 0

        embed = discord.Embed(
            title=f"📊 Staff Statistics — {target.display_name}",
            color=COL_MAIN,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="✋ Tickets Claimed", value=f"`{claimed}`",  inline=True)
        embed.add_field(name="🔒 Tickets Closed",  value=f"`{closed}`",  inline=True)
        embed.add_field(name="​", value="​", inline=True)  # spacer

        embed.add_field(
            name="⚡ Avg. First Response",
            value=f"`{format_duration(avg_rt)}`" if rc else "`N/A`",
            inline=True,
        )
        embed.add_field(
            name="⭐ Avg. Rating",
            value=f"`{avg_rat:.1f}/5.0`  {stars(round(avg_rat))}" if rat_count else "`No ratings yet`",
            inline=True,
        )
        embed.add_field(
            name="🗳️ Total Ratings",
            value=f"`{rat_count}`",
            inline=True,
        )
        embed.set_footer(text="PupPet • Staff Analytics")
        await interaction.response.send_message(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────
    # /leaderboard
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="leaderboard", description="View the top ticket staff leaderboard.")
    @app_commands.describe(sort_by="Sort by: closed (default), claimed, or rating")
    @app_commands.choices(sort_by=[
        app_commands.Choice(name="Tickets Closed", value="closed"),
        app_commands.Choice(name="Tickets Claimed", value="claimed"),
        app_commands.Choice(name="Average Rating", value="rating"),
    ])
    async def leaderboard_cmd(
        self,
        interaction: discord.Interaction,
        sort_by: str = "closed",
    ) -> None:
        await interaction.response.defer()

        all_stats = await self.data.get_all_stats()
        if not all_stats:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="No staff data yet. Close some tickets first!",
                    color=COL_MAIN,
                )
            )
            return

        # Build sorted list
        def sort_key(item):
            uid, d = item
            if sort_by == "claimed":
                return d.get("claimed", 0)
            if sort_by == "rating":
                rc = d.get("rating_count", 0)
                rt = d.get("total_rating", 0)
                return (rt / rc) if rc else 0
            return d.get("closed", 0)

        sorted_staff = sorted(all_stats.items(), key=sort_key, reverse=True)[:10]

        embed = discord.Embed(
            title="🏆 Staff Leaderboard",
            color=COL_PURPLE,
            timestamp=discord.utils.utcnow(),
        )
        sort_labels = {"closed": "Tickets Closed", "claimed": "Tickets Claimed", "rating": "Avg. Rating"}
        embed.description = f"*Sorted by **{sort_labels.get(sort_by, sort_by)}***\n\u200b"

        lines = []
        for idx, (uid, d) in enumerate(sorted_staff):
            member = interaction.guild.get_member(int(uid))
            name   = member.display_name if member else f"User #{uid}"
            medal  = MEDAL[idx] if idx < 3 else f"`#{idx+1}`"

            closed  = d.get("closed", 0)
            claimed = d.get("claimed", 0)
            rc      = d.get("rating_count", 0)
            rt      = d.get("total_rating", 0)
            avg_rat = (rt / rc) if rc else 0

            rc2     = d.get("response_count", 0)
            rt2     = d.get("total_response_time", 0)
            avg_rsp = (rt2 / rc2) if rc2 else 0

            line = (
                f"{medal} **{name}**\n"
                f"┣ 🔒 Closed: `{closed}`  ✋ Claimed: `{claimed}`\n"
                f"┣ ⭐ Avg Rating: `{avg_rat:.1f}/5`  ⚡ Avg Response: `{format_duration(avg_rsp)}`\n"
            )
            lines.append(line)

        embed.description = f"*Sorted by **{sort_labels.get(sort_by, sort_by)}***\n\u200b\n" + "\n".join(lines)
        embed.set_footer(text="PupPet • Staff Analytics")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Analytics(bot))
