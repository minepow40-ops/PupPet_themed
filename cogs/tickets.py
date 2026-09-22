"""
cogs/tickets.py
Core ticket management cog.
Handles: panel creation, ticket opening, closing, priority,
         transcript generation, auto-response timer, duplicate check,
         category intake questions (modal), AFK warning + auto-close.

🐶 Powered by PupPet — The World's Fluffiest Bot Framework™
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import re
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.data_manager import DataManager
from utils.helpers import (
    error_embed, info_embed, is_admin, is_staff, log_embed,
    set_ticket_permissions, success_embed, ticket_embed, warn_embed,
    generate_transcript, COL_WARN, COL_ERROR, COL_SUCCESS, admin_check,
)
from views.ticket_views import TicketControlView, TicketPanelView, RatingView

log = logging.getLogger("PupPet.cogs.tickets")

TRANSCRIPT_DIR = Path(__file__).parent.parent / "data" / "transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

# ── AFK auto-close timings ─────────────────────────────────────────────────
AFK_WARN_SECONDS  = 7 * 60   # 7 min  → send warning to user
AFK_CLOSE_SECONDS = 10 * 60  # 10 min → force close + transcript

# ── Ping roles ───────────────────────────────────────────────────────────────
STAFF_ROLE_ID = 1551515669026177077   # Staff role — pinged on every new ticket
OWNER_ROLE_ID = 1551515669026177077   # Owner role — pinged on every new ticket

# ── Category intake questions ──────────────────────────────────────────────
# Each category maps to a list of (label, placeholder) tuples shown in the
# intake modal. Max 5 questions per modal (Discord limit).
CATEGORY_QUESTIONS: dict[str, list[tuple[str, str]]] = {
    "support": [
        ("What's the issue?",          "Describe your problem in detail."),
        ("What have you tried?",        "Steps you've already taken to fix it."),
        ("Your in-game name / Discord", "e.g. PupPetKing#1234"),
    ],
    "report": [
        ("Who are you reporting?",      "Username / Discord tag of the offender."),
        ("What did they do?",           "Describe the incident clearly."),
        ("Evidence (links / details)",  "Screenshot links, timestamps, etc."),
        ("Your in-game name / Discord", "e.g. PupPetKing#1234"),
    ],
}


class Tickets(commands.Cog):
    """Core ticket system. Fully cooked. Absolutely starchy."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self.data: DataManager = bot.data
        # Map channel_id → asyncio.Task for waiting-for-staff timer message edits
        # (The puppy waits for no one, but it will be patient.)
        self._timer_tasks: dict[int, asyncio.Task] = {}
        # Map channel_id → asyncio.Task for AFK auto-close monitoring
        self._afk_tasks: dict[int, asyncio.Task] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Slash: /panel
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="panel", description="🐶 Summon the sacred PupPet support panel.")
    @admin_check()
    async def panel(self, interaction: discord.Interaction) -> None:
        """Post the ticket panel embed with category selector. Exactly as per design."""
        embed = discord.Embed(
            title="🎫 PupPet Support",
            description=(
                "Welcome to **PupPet** support!\n"
                "Please select the category that best matches your inquiry below.\n\n"
                "🆘 **Support** — General help & questions\n"
                "🚨 **Report** — Report a player or issue\n"
                "🤝 **Partnership** — Partnership inquiries\n"
                "💰 **Purchase** — Billing & purchases\n"
                "💻 **Developer Help** — Development questions\n"
                "📄 **Staff Application** — Apply for staff\n\n"
                "*Our team will assist you as soon as possible.*"
            ),
            color=0x5865F2,
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        embed.set_footer(text="PupPet - Ticket System")
        embed.timestamp = discord.utils.utcnow()

        view = TicketPanelView()
        ticket_img = None
        ticket_img_path = Path(__file__).parent.parent / "assets" / "ticket.png"
        if ticket_img_path.exists():
            ticket_img = discord.File(ticket_img_path, filename="ticket.png")
            embed.set_image(url="attachment://ticket.png")

        if ticket_img:
            await interaction.channel.send(embed=embed, view=view, file=ticket_img)
        else:
            await interaction.channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            embed=success_embed("Ticket panel posted!", ""),
            ephemeral=True,
        )
        log.info("Ticket panel posted by %s in #%s", interaction.user, interaction.channel)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash: /close
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="close", description="🪦 Put this ticket out of its misery.")
    async def close_cmd(self, interaction: discord.Interaction) -> None:
        from views.ticket_views import CloseConfirmModal
        ticket = await self.data.get_ticket(interaction.channel.id)
        if not ticket:
            await interaction.response.send_message(
                embed=error_embed(
                    "Not a ticket channel",
                    "This command can only be used inside an active ticket channel."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(CloseConfirmModal())

    # ──────────────────────────────────────────────────────────────────────────
    # Slash: /new  (direct open without panel)
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="new", description="🆕 Open a fresh, piping-hot support ticket.")
    @app_commands.describe(category="What flavour of problem do you have today?")
    @app_commands.choices(category=[
        app_commands.Choice(name="🛟 Support (I am confusion)",      value="support"),
        app_commands.Choice(name="🚨 Report (Someone did an oopsie)", value="report"),
    ])
    async def new_cmd(self, interaction: discord.Interaction, category: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.open_ticket(interaction, category)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash: /setup  (configure bot settings)
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="setup", description="⚙️ Configure PupPet (very serious admin stuff).")
    @app_commands.describe(
        category="Category where tickets shall be born",
        log_channel="Channel for logging all the drama",
        transcript_channel="Channel for saving evidence of said drama",
        staff_role="The chosen ones who handle tickets",
    )
    @admin_check()
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        log_channel: discord.TextChannel,
        transcript_channel: discord.TextChannel,
        staff_role: discord.Role,
    ) -> None:
        self.config.set("ticket_category_id", category.id)
        self.config.set("log_channel_id", log_channel.id)
        self.config.set("transcript_channel_id", transcript_channel.id)
        existing = self.config.get("staff_roles", [])
        if staff_role.id not in existing:
            existing.append(staff_role.id)
            self.config.set("staff_roles", existing)

        embed = discord.Embed(
            title="⚙️ Setup Complete",
            color=COL_SUCCESS,
            description=(
                f"**Ticket Category:** {category.mention}\n"
                f"**Drama Log Channel:** {log_channel.mention}\n"
                f"**Evidence Vault:** {transcript_channel.mention}\n"
                f"**The Chosen Staff Role:** {staff_role.mention}\n\n"
                f"*Bot is configured and ready.*"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        log.info("Bot configured by %s.", interaction.user)

    # ──────────────────────────────────────────────────────────────────────────
    # Core: open_ticket
    # ──────────────────────────────────────────────────────────────────────────

    async def open_ticket(
        self,
        interaction: discord.Interaction,
        category: str,
    ) -> None:
        """Hatch a brand new ticket channel for the user. Like a puppy egg."""
        guild = interaction.guild
        user  = interaction.user

        # ── Duplicate check ──────────────────────────────────────────
        existing = await self.data.get_user_ticket(user.id)
        if existing:
            ch = guild.get_channel(existing["channel_id"])
            if ch:
                await interaction.followup.send(
                    embed=warn_embed(
                        "You already have an open ticket",
                        f"You already have an open ticket: {ch.mention}\n"
                        "Please ask staff to close it before opening a new one.",
                    ),
                    ephemeral=True,
                )
                return
            else:
                # Channel was deleted externally — clean up the stale record so the user
                # can open a new ticket without being permanently blocked.
                await self.data.delete_ticket(existing["channel_id"])
                log.info(
                    "Cleaned up stale ticket #%d for user %s (channel %d no longer exists)",
                    existing.get("number", 0), user, existing["channel_id"],
                )

        # ── Category intake questions (modal) ────────────────────────
        questions = CATEGORY_QUESTIONS.get(category, [])
        intake_answers: dict[str, str] = {}
        if questions:
            modal = _IntakeModal(category=category, questions=questions)
            # We must send the modal via the original interaction response.
            # open_ticket is always called after defer(), so we use followup
            # to re-surface the modal via send_modal on the underlying client.
            # Discord 2.x allows send_modal only on non-deferred interactions,
            # so we store a second interaction obtained via a button click when
            # coming from the panel, or we use a workaround: send an ephemeral
            # button that opens the modal.
            # Simplest compatible approach: ask the user to fill a modal by
            # sending an ephemeral message with a "Fill in details" button,
            # then await the modal submit via asyncio.Event.
            event  = asyncio.Event()
            button = _IntakeButton(modal=modal, event=event)
            view   = discord.ui.View(timeout=120)
            view.add_item(button)
            await interaction.followup.send(
                embed=info_embed(
                    "Almost there — fill in a few details 📝",
                    "Click **Fill in Details** to answer a few quick questions "
                    "before your ticket is created. You have **2 minutes**.",
                ),
                view=view,
                ephemeral=True,
            )
            try:
                await asyncio.wait_for(event.wait(), timeout=120)
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=warn_embed(
                        "Intake form timed out",
                        "You didn't fill in the form within 2 minutes. Please open a new ticket and try again.",
                    ),
                    ephemeral=True,
                )
                return
            intake_answers = modal.answers

        # ── Category channel setup ───────────────────────────────────
        # Use configured ticket_category_id from /setup (fallback to hardcoded default).
        TICKET_CATEGORY_ID = self.config.ticket_category_id or 1512002760638337064
        cat_obj = guild.get_channel(TICKET_CATEGORY_ID)
        if cat_obj is None:
            await interaction.followup.send(
                embed=error_embed(
                    "Category not found",
                    "The configured ticket category channel was not found. "
                    "Please run `/setup` again with a valid category channel."
                ),
                ephemeral=True,
            )
            return

        # ── Ticket number ────────────────────────────────────────────
        all_tickets = await self.data.get_tickets()
        existing_numbers = [t.get("number", 0) for t in all_tickets.values()]
        ticket_number = (max(existing_numbers) + 1) if existing_numbers else 1

        cat_cfg = self.config.categories.get(category, {})
        cat_label = cat_cfg.get("label", category).lower().replace(" ", "-")
        channel_name = f"{cat_label}-{user.name.lower()}"
        channel_name = re.sub(r"[^a-z0-9\-]", "", channel_name)[:80]

        # ── Create channel ───────────────────────────────────────────
        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=cat_obj,
                topic=f"🐶 Ticket #{ticket_number:04d} | Owner: {user.id} | Priority: none | Claimed: none | Powered by PupPet",
                reason=f"🐶 Ticket #{ticket_number} opened by {user} (a lovely human)",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Missing permissions",
                    "I don't have permission to create channels. Please contact an administrator."
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                embed=error_embed("Failed to create ticket channel", str(e)),
                ephemeral=True,
            )
            return

        # ── Set permissions ──────────────────────────────────────────
        await set_ticket_permissions(channel, guild, user, self.config, visible=True)

        # ── Persist ticket data ──────────────────────────────────────
        now = discord.utils.utcnow()
        ticket_data = {
            "channel_id": channel.id,
            "guild_id":   guild.id,
            "owner_id":   user.id,
            "category":   category,
            "number":     ticket_number,
            "priority":   "none",
            "claimed_by": None,
            "status":     "open",
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "created_ts": now.timestamp(),
            "last_message_ts": now.timestamp(),
            "first_response_ts": None,
            "extra_users": [],
            "timer_message_id": None,
        }
        await self.data.save_ticket(channel.id, ticket_data)

        # ── Welcome embed + control view ─────────────────────────────
        welcome = ticket_embed(category, cat_cfg, user, ticket_number)
        control_view = TicketControlView()
        # Mention the ticket owner, staff role, and server owner role
        staff_mention = f"<@&{STAFF_ROLE_ID}>"
        owner_role_mention = f"<@&{OWNER_ROLE_ID}>"
        await channel.send(
            content=(
                f"{user.mention} — your ticket has been created.\n"
                f"{staff_mention} {owner_role_mention} — a new ticket is waiting."
            ),
            embed=welcome,
            view=control_view,
        )

        # ── Intake answers embed ─────────────────────────────────────
        if intake_answers:
            intake_embed = discord.Embed(
                title="📋 Intake Information",
                description="\n".join(
                    f"**{q}**\n{a}" for q, a in intake_answers.items()
                ),
                color=0x5865F2,
                timestamp=discord.utils.utcnow(),
            )
            intake_embed.set_footer(text=f"Submitted by {user.display_name} | PupPet™")
            await channel.send(embed=intake_embed)

        # ── Waiting-for-staff timer message ──────────────────────────
        timer_embed = discord.Embed(
            description=(
                "⏳ **Staff have been notified.**\n"
                "A team member will be with you shortly. Please stay in the channel."
            ),
            color=0xF39C12,
        )
        timer_msg = await channel.send(embed=timer_embed)
        await self.data.update_ticket_field(channel.id, timer_message_id=timer_msg.id)

        # ── Notify user ──────────────────────────────────────────────
        cat_emoji = cat_cfg.get("emoji", "🐶")
        await interaction.followup.send(
            embed=success_embed(
                "Ticket created ✅",
                f"Your ticket is open: {channel.mention}\n"
                f"{cat_emoji} **Category:** {cat_cfg.get('label', category.title())}",
            ),
            ephemeral=True,
        )

        # ── Log ──────────────────────────────────────────────────────
        await self._send_log(
            guild, "🎫 Ticket Opened", ticket_data, user,
            color=COL_SUCCESS,
            extra=[("Channel", channel.mention, True)],
        )
        log.info("🐶 Ticket #%d opened by %s in #%s", ticket_number, user, channel)

        # ── Kick off AFK monitor ─────────────────────────────────────
        self._start_afk_monitor(channel.id, guild)

    # ──────────────────────────────────────────────────────────────────────────
    # Core: close_ticket
    # ──────────────────────────────────────────────────────────────────────────

    async def close_ticket(
        self,
        interaction: discord.Interaction,
        reason: str = "No reason provided. (Mysterious.)",
    ) -> None:
        """Close the ticket: transcript → log → rating → delete channel. RIP little ticket."""
        channel = interaction.channel
        guild   = interaction.guild
        actor   = interaction.user

        ticket = await self.data.get_ticket(channel.id)
        if not ticket:
            await interaction.followup.send(
                embed=error_embed(
                    "Not a ticket channel",
                    "This is not an active ticket channel."
                ),
                ephemeral=True,
            )
            return

        # ── Closing embed ────────────────────────────────────────────
        closing_embed = discord.Embed(
            title="🔒 Closing Ticket…",
            description=(
                f"**Reason:** {reason}\n\n"
                "Generating transcript... please hold while we collect the receipts. 🧾"
            ),
            color=COL_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        closing_embed.set_footer(text=f"Closed by {actor.display_name} | PupPet")
        await interaction.followup.send(embed=closing_embed)

        # ── Analytics: increment closed ──────────────────────────────
        if ticket.get("claimed_by"):
            await self.data.increment_stat(int(ticket["claimed_by"]), "closed")

        # ── Generate transcript ──────────────────────────────────────
        html_content = None
        ts_settings  = self.config.ticket_settings.get("transcript_enabled", True)
        if ts_settings:
            try:
                html_content = await generate_transcript(channel, ticket)
            except Exception as e:
                log.error("💥 Transcript generation went boom: %s", e)

        # ── Send transcript to transcript channel ────────────────────
        if html_content:
            filename = f"transcript-{ticket['number']:04d}.html"
            ts_ch_id = self.config.transcript_channel_id
            ts_ch = guild.get_channel(ts_ch_id) if ts_ch_id else None
            if ts_ch:
                ts_embed = discord.Embed(
                    title=f"📄 Transcript — Ticket #{ticket['number']:04d}",
                    description=(
                        f"**Category:** {ticket['category'].title()}\n"
                        f"**Owner:** <@{ticket['owner_id']}>\n"
                        f"**Closed by:** {actor.mention}\n"
                        f"**Reason:** {reason}"
                    ),
                    color=0x5865F2,
                    timestamp=discord.utils.utcnow(),
                )
                ts_embed.set_footer(text="PupPet Transcript Archive")
                ts_file = discord.File(
                    io.BytesIO(html_content.encode()),
                    filename=filename,
                )
                try:
                    await ts_ch.send(embed=ts_embed, file=ts_file)
                except discord.HTTPException as e:
                    log.error("Failed to send transcript: %s", e)

        # ── Log closure ──────────────────────────────────────────────
        await self._send_log(
            guild, "🔒 Ticket Closed", ticket, actor,
            color=COL_ERROR,
            extra=[
                ("Last Words (Reason)", reason, False),
                ("Claimed By", f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "Nobody (spooky)", True),
            ],
        )

        # ── Remove ticket from active list ───────────────────────────
        await self.data.delete_ticket(channel.id)

        # ── Cancel AFK monitor (ticket closed normally) ───────────────
        self._cancel_afk_monitor(channel.id)

        # ── Send rating prompt to owner (DM) ─────────────────────────
        if self.config.ticket_settings.get("rating_enabled", True):
            owner = guild.get_member(int(ticket["owner_id"]))
            if owner and ticket.get("claimed_by"):
                await self._send_rating_prompt(owner, ticket)

        # ── Delete channel after short delay ─────────────────────────
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {actor}.")
        except discord.HTTPException as e:
            log.error("Failed to yeet ticket channel: %s", e)

        log.info("🪦 Ticket #%d has been sent to the shadow realm by %s.", ticket["number"], actor)

    # ──────────────────────────────────────────────────────────────────────────
    # Core: set_priority
    # ──────────────────────────────────────────────────────────────────────────

    async def set_priority(
        self,
        interaction: discord.Interaction,
        priority: str,
    ) -> None:
        """Update ticket priority and rename the channel. Drama level intensifying."""
        channel = interaction.channel
        ticket  = await self.data.get_ticket(channel.id)
        if not ticket:
            await interaction.followup.send(
                embed=error_embed(
                    "Not a ticket channel",
                    "This command can only be used inside an active ticket channel."
                ),
                ephemeral=True,
            )
            return

        old_priority = ticket.get("priority", "none")
        if old_priority == priority:
            await interaction.followup.send(
                embed=info_embed(
                    "No change",
                    f"Priority is already set to **{priority}**."
                ),
                ephemeral=True,
            )
            return

        # Rename channel: priority-category-username
        cat_cfg   = self.config.categories.get(ticket["category"], {})
        cat_label = cat_cfg.get("label", ticket["category"]).lower().replace(" ", "-")
        owner     = interaction.guild.get_member(int(ticket["owner_id"]))
        uname     = owner.name.lower() if owner else "mystery-pup"
        new_name  = re.sub(r"[^a-z0-9\-]", "", f"{priority}-{cat_label}-{uname}")[:80]

        await self.data.update_ticket_field(channel.id, priority=priority)

        color = self.config.priority_colors.get(priority, 0x5865F2)
        priority_labels = {
            "low":    "🟢 Low (Chill Vibes)",
            "medium": "🟡 Medium (A Bit Spicy)",
            "high":   "🔴 HIGH ALERT (Everything Is On Fire)",
        }
        prio_display = priority_labels.get(priority, priority.title())

        embed = discord.Embed(
            title="🏷️ Priority Level: Adjusted",
            description=f"This ticket is now classified as **{prio_display}**.\nAct accordingly.",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Updated by {interaction.user.display_name} | PupPet")
        await interaction.channel.send(embed=embed)
        await interaction.followup.send(f"Priority updated to **{prio_display}**.", ephemeral=True)

        ticket_number = ticket["number"]
        claimed_by = ticket.get("claimed_by", "none")
        try:
            await channel.edit(
                name=new_name,
                topic=(
                    f"🐶 Ticket #{ticket_number:04d} | Owner: {ticket['owner_id']} | "
                    f"Priority: {priority} | Claimed: {claimed_by} | PupPet™"
                ),
            )
        except discord.HTTPException as e:
            log.warning("Could not rename channel (sadge): %s", e)

        log.info("🏷️ Ticket #%d priority -> %s by %s", ticket["number"], priority, interaction.user)

    # ──────────────────────────────────────────────────────────────────────────
    # First-response tracker (called by on_message in this cog)
    # ──────────────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not message.guild:
            return

        ticket = await self.data.get_ticket(message.channel.id)
        if not ticket:
            return

        now = discord.utils.utcnow().timestamp()
        # Update last message timestamp (for auto-close / anti-ghost detection)
        await self.data.update_ticket_field(message.channel.id, last_message_ts=now)

        # ── Reset AFK timer on every message ────────────────────────
        # Restart the monitor so the 7-min warn + 10-min close
        # counts from *this* message, not the ticket creation.
        self._start_afk_monitor(message.channel.id, message.guild)

        # First response time tracking — did a staff puppy show up?
        if (
            ticket.get("first_response_ts") is None
            and is_staff(message.author, self.config)
            and message.author.id != ticket["owner_id"]
        ):
            created_ts = ticket.get("created_ts", now)
            response_time = now - created_ts
            await self.data.update_ticket_field(
                message.channel.id, first_response_ts=now
            )
            claimer_id = ticket.get("claimed_by")
            if claimer_id:
                await self.data.add_response_time(int(claimer_id), response_time)

            # Edit the "waiting for staff" timer message — puppy has arrived!
            timer_msg_id = ticket.get("timer_message_id")
            if timer_msg_id:
                from utils.helpers import format_duration
                try:
                    timer_msg = await message.channel.fetch_message(int(timer_msg_id))
                    resp_embed = discord.Embed(
                        description=(
                            f"⚡ **A Staff Puppy Has Arrived!**\n"
                            f"First response time: **{format_duration(response_time)}**\n"
                            f"*(The wait is over. Rejoice.)*"
                        ),
                        color=COL_SUCCESS,
                    )
                    await timer_msg.edit(embed=resp_embed)
                except (discord.NotFound, discord.HTTPException):
                    pass  # The timer message perished. Pour one out.

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_log(
        self,
        guild: discord.Guild,
        action: str,
        ticket_data: dict,
        actor: discord.Member,
        color: int = 0x5865F2,
        extra: list | None = None,
    ) -> None:
        """Dispatch a log embed to the drama channel."""
        log_ch_id = self.config.log_channel_id
        if not log_ch_id:
            return
        ch = guild.get_channel(log_ch_id)
        if not ch:
            return
        embed = log_embed(action, ticket_data, actor, color=color, extra_fields=extra)
        try:
            await ch.send(embed=embed)
        except discord.HTTPException as e:
            log.error("Failed to send log (the drama has been lost forever): %s", e)

    async def _send_rating_prompt(
        self,
        owner: discord.Member,
        ticket: dict,
    ) -> None:
        """DM the ticket owner asking them to rate their experience. No pressure. (Some pressure.)"""
        try:
            embed = discord.Embed(
                title="⭐ Rate Your Support Experience",
                description=(
                    f"Your ticket **#{ticket['number']:04d}** has been closed.\n\n"
                    "How would you rate the support you received?\n"
                    "Click a button below to leave your rating."
                ),
                color=0xF1C40F,
            )
            embed.set_footer(text="PupPet Support System")
            view = RatingView()
            embed.add_field(name="Ticket ID", value=str(ticket["channel_id"]), inline=True)
            embed.add_field(name="Staff", value=f"<@{ticket['claimed_by']}>", inline=True)
            await owner.send(embed=embed, view=view)
        except discord.Forbidden:
            log.info("Could not DM rating prompt to %s — their DMs are sealed like a bag of chips.", owner)
        except Exception as e:
            log.error("Rating prompt catastrophe: %s", e)

    # ──────────────────────────────────────────────────────────────────────────
    # AFK monitor
    # ──────────────────────────────────────────────────────────────────────────

    def _start_afk_monitor(self, channel_id: int, guild: discord.Guild) -> None:
        """Cancel any existing AFK task for this channel and start a fresh one."""
        self._cancel_afk_monitor(channel_id)
        task = self.bot.loop.create_task(
            self._afk_monitor_loop(channel_id, guild),
            name=f"afk_monitor_{channel_id}",
        )
        self._afk_tasks[channel_id] = task

    def _cancel_afk_monitor(self, channel_id: int) -> None:
        """Cancel the AFK task for a channel if it exists."""
        task = self._afk_tasks.pop(channel_id, None)
        if task and not task.done():
            task.cancel()

    async def _afk_monitor_loop(self, channel_id: int, guild: discord.Guild) -> None:
        """
        Background loop that watches a ticket channel for inactivity.

        Timeline (from last message):
          0 min  → ticket opens / any message resets the clock
          7 min  → AFK warning sent, clock continues
          10 min → ticket force-closed with transcript
        """
        warn_sent = False
        try:
            while True:
                await asyncio.sleep(30)  # check every 30 s (low overhead)

                ticket = await self.data.get_ticket(channel_id)
                if not ticket:
                    return  # ticket was closed normally

                last_ts   = ticket.get("last_message_ts", ticket.get("created_ts", 0))
                idle_secs = discord.utils.utcnow().timestamp() - last_ts

                channel = guild.get_channel(channel_id)
                if not channel:
                    return  # channel deleted externally

                # ── 7-min warn ───────────────────────────────────────
                if not warn_sent and idle_secs >= AFK_WARN_SECONDS:
                    owner = guild.get_member(int(ticket["owner_id"]))
                    mention = owner.mention if owner else "there"
                    afk_warn_embed = discord.Embed(
                        title="⚠️ Inactivity Warning",
                        description=(
                            f"{mention} This ticket has been idle for **7 minutes**.\n\n"
                            "If there is no activity in the next **3 minutes**, "
                            "this ticket will be **automatically closed** and a transcript will be saved.\n\n"
                            "Send any message to reset the timer."
                        ),
                        color=COL_WARN,
                        timestamp=discord.utils.utcnow(),
                    )
                    afk_warn_embed.set_footer(text="PupPet Ticket System")
                    try:
                        await channel.send(
                            content=mention,  # plain mention so the ping fires
                            embed=afk_warn_embed,
                        )
                    except discord.HTTPException:
                        pass
                    warn_sent = True
                    log.info("⚠️ AFK warning sent in ticket channel %d", channel_id)

                # ── 10-min force close ───────────────────────────────
                if idle_secs >= AFK_CLOSE_SECONDS:
                    log.info("💤 Auto-closing ticket %d due to AFK (%.0fs idle)", channel_id, idle_secs)
                    await self._afk_force_close(channel, guild, ticket)
                    return

        except asyncio.CancelledError:
            pass  # Normal cancellation (ticket closed or bot restart)
        except Exception as e:
            log.error("AFK monitor loop error for channel %d: %s", channel_id, e)

    async def _afk_force_close(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        ticket: dict,
    ) -> None:
        """Force-close a ticket due to AFK inactivity, generating a transcript."""
        # ── Closing embed ────────────────────────────────────────────
        closing_embed = discord.Embed(
            title="🔒 Ticket Closed — Inactivity",
            description=(
                "This ticket was **automatically closed** after **10 minutes of inactivity**.\n\n"
                "A transcript has been saved. If you still need help, please open a new ticket."
            ),
            color=COL_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        closing_embed.set_footer(text="PupPet Ticket System")
        try:
            await channel.send(embed=closing_embed)
        except discord.HTTPException:
            pass

        # ── Analytics ────────────────────────────────────────────────
        if ticket.get("claimed_by"):
            await self.data.increment_stat(int(ticket["claimed_by"]), "closed")

        # ── Transcript ───────────────────────────────────────────────
        html_content = None
        if self.config.ticket_settings.get("transcript_enabled", True):
            try:
                html_content = await generate_transcript(channel, ticket)
            except Exception as e:
                log.error("AFK transcript generation error: %s", e)

        if html_content:
            filename = f"transcript-{ticket['number']:04d}.html"
            ts_ch_id = self.config.transcript_channel_id
            ts_ch    = guild.get_channel(ts_ch_id) if ts_ch_id else None
            if ts_ch:
                ts_embed = discord.Embed(
                    title=f"📄 AFK Auto-Close — Ticket #{ticket['number']:04d}",
                    description=(
                        f"**Category:** {ticket['category'].title()}\n"
                        f"**Owner:** <@{ticket['owner_id']}>\n"
                        f"**Closed by:** AFK Auto-Close (10 min inactivity)"
                    ),
                    color=0x5865F2,
                    timestamp=discord.utils.utcnow(),
                )
                ts_embed.set_footer(text="PupPet Transcript Vault")
                ts_file = discord.File(
                    io.BytesIO(html_content.encode()),
                    filename=filename,
                )
                try:
                    await ts_ch.send(embed=ts_embed, file=ts_file)
                except discord.HTTPException as e:
                    log.error("Failed to send AFK transcript: %s", e)

        # ── Log ──────────────────────────────────────────────────────
        fake_actor = guild.me  # bot is the actor for AFK closes
        await self._send_log(
            guild, "🔒 Ticket Auto-Closed (Inactivity)", ticket, fake_actor,
            color=COL_WARN,
            extra=[("Reason", "10 minutes of inactivity", False)],
        )

        # ── Rating prompt ────────────────────────────────────────────
        if self.config.ticket_settings.get("rating_enabled", True):
            owner = guild.get_member(int(ticket["owner_id"]))
            if owner and ticket.get("claimed_by"):
                await self._send_rating_prompt(owner, ticket)

        # ── Remove from data + delete channel ────────────────────────
        await self.data.delete_ticket(channel.id)
        self._afk_tasks.pop(channel.id, None)
        await asyncio.sleep(3)
        try:
            await channel.delete(reason="Ticket auto-closed due to inactivity.")
        except discord.HTTPException as e:
            log.error("Failed to delete AFK-closed channel: %s", e)


# ──────────────────────────────────────────────────────────────────────────────
# Intake modal + button  (used inside open_ticket)
# ──────────────────────────────────────────────────────────────────────────────

class _IntakeModal(discord.ui.Modal):
    """
    Dynamically built modal that shows category-specific intake questions.
    Up to 5 TextInput fields (Discord hard limit per modal).
    """

    def __init__(self, category: str, questions: list[tuple[str, str]]) -> None:
        super().__init__(title=f"📋 Ticket Info — {category.replace('_', ' ').title()}", timeout=120)
        self.answers: dict[str, str] = {}

        for label, placeholder in questions[:5]:  # Discord modal cap = 5 items
            item = discord.ui.TextInput(
                label=label,
                placeholder=placeholder,
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=500,
            )
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Collect answers keyed by the TextInput label
        self.answers = {
            child.label: child.value
            for child in self.children
            if isinstance(child, discord.ui.TextInput)
        }
        await interaction.response.send_message(
            embed=discord.Embed(
                description="✅ Details received — creating your ticket now.",
                color=0x57F287,
            ),
            ephemeral=True,
        )
        # Signal the waiting open_ticket coroutine that the form is done
        if hasattr(self, "_event"):
            self._event.set()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("IntakeModal error: %s", error)
        await interaction.response.send_message("Something went wrong with the form. Try again!", ephemeral=True)


class _IntakeButton(discord.ui.Button):
    """
    Ephemeral button that opens the intake modal.
    Used because open_ticket is called after defer(), and modals can only
    be sent as a direct response to a fresh interaction.
    """

    def __init__(self, modal: _IntakeModal, event: asyncio.Event) -> None:
        super().__init__(
            label="Fill in Details",
            style=discord.ButtonStyle.primary,
            emoji="📝",
        )
        self.modal = modal
        self.event = event
        modal._event = event  # back-reference so on_submit can signal

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(self.modal)


async def setup(bot: commands.Bot) -> None:
    """Load the Tickets cog. The puppy awakens."""
    await bot.add_cog(Tickets(bot))