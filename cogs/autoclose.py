"""
cogs/autoclose.py
Auto-close system: warns after 24h inactivity, closes after 48h.
Runs a background task every 30 minutes to check inactive tickets.
"""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord.ext import commands, tasks

from utils.config import Config
from utils.data_manager import DataManager
from utils.helpers import COL_ERROR, COL_WARN

log = logging.getLogger("PupPet.cogs.autoclose")

CHECK_INTERVAL_MINUTES = 30


class AutoClose(commands.Cog):
    """Automatic ticket inactivity system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self.data: DataManager = bot.data
        self._warned: set[int] = set()  # channel IDs that have received a warning
        self.auto_close_loop.start()

    def cog_unload(self) -> None:
        self.auto_close_loop.cancel()

    # ──────────────────────────────────────────────────────────────────────────
    # Background task
    # ──────────────────────────────────────────────────────────────────────────

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def auto_close_loop(self) -> None:
        """Check all open tickets for inactivity and warn/close as needed."""
        try:
            await self._check_inactive_tickets()
        except Exception as e:
            log.error("auto_close_loop error: %s", e, exc_info=True)

    @auto_close_loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ──────────────────────────────────────────────────────────────────────────
    # Core check logic
    # ──────────────────────────────────────────────────────────────────────────

    async def _check_inactive_tickets(self) -> None:
        tickets = await self.data.get_tickets()
        now     = discord.utils.utcnow().timestamp()

        warn_hours  = self.config.ticket_settings.get("auto_close_warning_hours", 24)
        close_hours = self.config.ticket_settings.get("auto_close_hours", 48)
        warn_secs   = warn_hours  * 3600
        close_secs  = close_hours * 3600

        for ch_id_str, ticket in list(tickets.items()):
            ch_id     = int(ch_id_str)
            last_ts   = ticket.get("last_message_ts", ticket.get("created_ts", now))
            inactive  = now - last_ts

            guild_id = ticket.get("guild_id")
            guild    = None
            if guild_id:
                guild = self.bot.get_guild(int(guild_id))
            if not guild:
                # Try to find by channel
                channel = self.bot.get_channel(ch_id)
                if channel:
                    guild = channel.guild

            channel = self.bot.get_channel(ch_id)
            if not channel:
                # Channel was deleted externally — clean up stale ticket record
                await self.data.delete_ticket(ch_id)
                self._warned.discard(ch_id)
                log.info("Cleaned up stale ticket record for deleted channel %d", ch_id)
                continue

            # ── Auto-close ───────────────────────────────────────────
            if inactive >= close_secs:
                log.info(
                    "Auto-closing ticket #%d (inactive %.1fh)",
                    ticket["number"], inactive / 3600,
                )
                await self._auto_close(channel, ticket)
                continue

            # ── Warning ──────────────────────────────────────────────
            if inactive >= warn_secs and ch_id not in self._warned:
                await self._send_warning(channel, ticket, close_hours - warn_hours)
                self._warned.add(ch_id)

    async def _send_warning(
        self,
        channel: discord.TextChannel,
        ticket: dict,
        hours_until_close: int,
    ) -> None:
        owner = channel.guild.get_member(int(ticket["owner_id"]))
        mention = owner.mention if owner else "there"
        embed = discord.Embed(
            title="⚠️ Inactivity Warning",
            description=(
                f"Hey {mention}, this ticket has been inactive.\n\n"
                f"If there is no activity within the next **{hours_until_close} hour(s)**, "
                "this ticket will be **automatically closed**.\n\n"
                "*Please respond or ask staff to close the ticket if your issue is resolved.*"
            ),
            color=COL_WARN,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="PupPet • Auto-Close System")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("Could not send inactivity warning to #%s: %s", channel, e)

    async def _auto_close(
        self,
        channel: discord.TextChannel,
        ticket: dict,
    ) -> None:
        embed = discord.Embed(
            title="🔒 Ticket Auto-Closed",
            description=(
                "This ticket has been automatically closed due to **48 hours of inactivity**.\n\n"
                "If you still need help, please open a new ticket."
            ),
            color=COL_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="PupPet • Auto-Close System")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

        # Update analytics
        claimer_id = ticket.get("claimed_by")
        if claimer_id:
            await self.data.increment_stat(int(claimer_id), "closed")

        # Generate and save transcript
        try:
            from utils.helpers import generate_transcript
            html_content = await generate_transcript(channel, ticket)
            if html_content:
                filename = f"transcript-{ticket['number']:04d}.html"
                ts_ch_id = self.config.transcript_channel_id
                ts_ch = self.bot.get_channel(ts_ch_id) if ts_ch_id else None
                if ts_ch:
                    ts_embed = discord.Embed(
                        title=f"📄 Transcript — Ticket #{ticket['number']:04d}",
                        description=(
                            f"**Category:** {ticket['category'].title()}\n"
                            f"**Owner:** <@{ticket['owner_id']}>\n"
                            f"**Closed by:** Auto-Close\n"
                            f"**Reason:** 48h inactivity"
                        ),
                        color=0x5865F2,
                        timestamp=discord.utils.utcnow(),
                    )
                    ts_file = discord.File(io.BytesIO(html_content.encode("utf-8")), filename=filename)
                    await ts_ch.send(embed=ts_embed, file=ts_file)
        except Exception as e:
            log.error("Auto-close transcript error: %s", e)

        # Send rating DM to owner if ticket was claimed
        try:
            if self.config.ticket_settings.get("rating_enabled", True) and ticket.get("claimed_by"):
                guild_id = ticket.get("guild_id")
                guild = self.bot.get_guild(int(guild_id)) if guild_id else channel.guild
                if guild:
                    owner = guild.get_member(int(ticket["owner_id"]))
                    if owner:
                        from views.ticket_views import RatingView
                        rating_embed = discord.Embed(
                            title="⭐ Rate Your Support Experience",
                            description=(
                                f"Your ticket **#{ticket['number']:04d}** has been closed.\n\n"
                                "How would you rate the support you received?\n"
                                "Click a button below to submit your rating."
                            ),
                            color=0xF1C40F,
                        )
                        rating_embed.set_footer(text="PupPet • Support Ratings")
                        rating_embed.add_field(name="Ticket ID", value=str(ticket["channel_id"]), inline=True)
                        rating_embed.add_field(name="Staff", value=f"<@{ticket['claimed_by']}>", inline=True)
                        await owner.send(embed=rating_embed, view=RatingView())
        except Exception as e:
            log.error("Auto-close rating prompt error: %s", e)

        await self.data.delete_ticket(channel.id)
        self._warned.discard(channel.id)

        # Log to log channel
        try:
            log_ch_id = self.config.log_channel_id
            if log_ch_id:
                log_ch = self.bot.get_channel(log_ch_id)
                if log_ch:
                    log_embed_msg = discord.Embed(
                        title="🤖 Auto-Close — Ticket Closed",
                        description=(
                            f"**Ticket:** #{ticket['number']:04d}\n"
                            f"**Owner:** <@{ticket['owner_id']}>\n"
                            f"**Reason:** 48h inactivity"
                        ),
                        color=COL_ERROR,
                        timestamp=discord.utils.utcnow(),
                    )
                    await log_ch.send(embed=log_embed_msg)
        except Exception:
            pass

        await asyncio.sleep(3)
        try:
            await channel.delete(reason="Auto-closed: 48h inactivity")
        except discord.HTTPException as e:
            log.error("Failed to delete auto-closed ticket: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoClose(bot))
