"""
cogs/ratings.py
Ticket rating system.
Handles DM rating submissions via RatingView buttons and /ratings command.
"""

from __future__ import annotations

import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.data_manager import DataManager
from utils.helpers import COL_SUCCESS, stars

log = logging.getLogger("PupPet.cogs.ratings")


class Ratings(commands.Cog):
    """Ticket rating system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self.data: DataManager = bot.data

    # ──────────────────────────────────────────────────────────────────────────
    # submit_rating (called from RatingView)
    # ──────────────────────────────────────────────────────────────────────────

    async def submit_rating(
        self, interaction: discord.Interaction, rating: int
    ) -> None:
        """Process a rating submitted via the DM rating view."""
        # Extract ticket_id and staff from embed fields
        msg = interaction.message
        ticket_id   = None
        claimer_id  = None

        if msg and msg.embeds:
            for field in msg.embeds[0].fields:
                if field.name == "Ticket ID":
                    ticket_id = field.value.strip()
                elif field.name == "Staff":
                    # Extract ID from mention <@ID>
                    import re
                    m = re.search(r"\d+", field.value)
                    if m:
                        claimer_id = int(m.group())

        if not ticket_id:
            await interaction.followup.send(
                "Could not determine ticket ID. Rating not saved.", ephemeral=True
            )
            return

        # Save rating
        rating_data = {
            "rating":     rating,
            "rater_id":   interaction.user.id,
            "staff_id":   claimer_id,
            "ticket_id":  ticket_id,
            "submitted":  discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        await self.data.save_rating(ticket_id, rating_data)

        # Update staff analytics
        if claimer_id:
            await self.data.add_rating(claimer_id, rating)

        star_str = stars(rating)
        embed = discord.Embed(
            title="⭐ Rating Submitted — Thank You!",
            description=(
                f"**Your Rating:** {star_str} ({rating}/5)\n\n"
                "Thank you for taking the time to rate your support experience. "
                "Your feedback helps us improve!"
            ),
            color=COL_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="PupPet • Support Ratings")
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("User %s rated ticket %s → %d stars", interaction.user, ticket_id, rating)

    # ──────────────────────────────────────────────────────────────────────────
    # /ratings
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="ratings", description="View all ratings for a staff member.")
    @app_commands.describe(member="The staff member (leave blank for yourself)")
    async def ratings_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        all_ratings = await self.data.get_all_ratings()

        # Filter ratings for this staff member
        staff_ratings = [
            r for r in all_ratings.values()
            if r.get("staff_id") == target.id
        ]

        if not staff_ratings:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description=f"No ratings found for **{target.display_name}**.",
                    color=0xF1C40F,
                ),
                ephemeral=True,
            )
            return

        total   = sum(r["rating"] for r in staff_ratings)
        count   = len(staff_ratings)
        average = total / count

        dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for r in staff_ratings:
            dist[r["rating"]] = dist.get(r["rating"], 0) + 1

        embed = discord.Embed(
            title=f"⭐ Ratings — {target.display_name}",
            color=0xF1C40F,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name="Overall",
            value=f"{stars(round(average))}  **{average:.2f}/5.00**",
            inline=False,
        )
        embed.add_field(name="Total Ratings", value=f"`{count}`", inline=True)

        dist_str = "\n".join(
            f"{'⭐' * s}  `{dist[s]}` rating(s)"
            for s in range(5, 0, -1)
        )
        embed.add_field(name="Distribution", value=dist_str, inline=False)
        embed.set_footer(text="PupPet • Staff Ratings")

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ratings(bot))
