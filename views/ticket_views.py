"""
views/ticket_views.py
All persistent Views and Modals for the ticket system:
  - TicketPanelView  (category select + open button)
  - TicketControlView (close / claim / priority / add/remove user)
  - CloseConfirmModal
  - AddUserModal / RemoveUserModal / RenameModal
  - RatingView
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import tempfile
from typing import Optional

import discord
from discord.ui import Button, Modal, Select, TextInput, View

log = logging.getLogger("PupPet.views.tickets")

# ── Helpers imported lazily inside methods to avoid circular imports ──────────

def _get_bot(interaction: discord.Interaction):
    return interaction.client


# ─────────────────────────────────────────────────────────────────────────────
# Category Select Menu
# ─────────────────────────────────────────────────────────────────────────────

class CategorySelect(discord.ui.Select):
    """Select menu for choosing a ticket category."""

    def __init__(self):
        options = [
            discord.SelectOption(label="Support",          value="support",     emoji="🆘",
                                 description="General help & questions"),
            discord.SelectOption(label="Report",           value="report",      emoji="🚨",
                                 description="Report a player or issue"),
            discord.SelectOption(label="Partnership",      value="partnership", emoji="🤝",
                                 description="Partnership inquiries"),
            discord.SelectOption(label="Purchase",         value="purchase",    emoji="💰",
                                 description="Billing & purchases"),
            discord.SelectOption(label="Developer Help",   value="dev_help",    emoji="💻",
                                 description="Development questions"),
            discord.SelectOption(label="Staff Application",value="application", emoji="📄",
                                 description="Apply for staff"),
        ]
        super().__init__(
            custom_id="ticket_category_select",
            placeholder="Select a category to open a ticket...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        category = self.values[0]
        bot = _get_bot(interaction)
        # Delegate to the tickets cog
        cog = bot.get_cog("Tickets")
        if cog:
            await cog.open_ticket(interaction, category)
        else:
            await interaction.followup.send("Ticket system is unavailable.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Panel View
# ─────────────────────────────────────────────────────────────────────────────

class TicketPanelView(View):
    """Persistent view attached to the ticket panel message."""

    def __init__(self):
        super().__init__(timeout=None)  # persistent
        self.add_item(CategorySelect())


# ─────────────────────────────────────────────────────────────────────────────
# Modals
# ─────────────────────────────────────────────────────────────────────────────

class CloseConfirmModal(Modal, title="Close Ticket"):
    reason = TextInput(
        label="Reason for closing",
        placeholder="Enter the reason for closing this ticket…",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        bot = _get_bot(interaction)
        cog = bot.get_cog("Tickets")
        if cog:
            await cog.close_ticket(interaction, reason=self.reason.value or "No reason provided.")
        else:
            await interaction.followup.send("Ticket system unavailable.", ephemeral=True)


class AddUserModal(Modal, title="Add User to Ticket"):
    user_id = TextInput(
        label="User ID or @mention",
        placeholder="Enter a User ID (e.g. 123456789012345678)",
        required=True,
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _get_bot(interaction)
        cog = bot.get_cog("Staff")
        if cog:
            await cog.add_user_to_ticket(interaction, self.user_id.value.strip())


class RemoveUserModal(Modal, title="Remove User from Ticket"):
    user_id = TextInput(
        label="User ID",
        placeholder="Enter the User ID to remove",
        required=True,
        max_length=30,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _get_bot(interaction)
        cog = bot.get_cog("Staff")
        if cog:
            await cog.remove_user_from_ticket(interaction, self.user_id.value.strip())


class RenameModal(Modal, title="Rename Ticket"):
    new_name = TextInput(
        label="New ticket name",
        placeholder="e.g. high-support-username",
        required=True,
        max_length=80,
        min_length=2,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        name = re.sub(r"[^a-z0-9\-]", "", self.new_name.value.lower().replace(" ", "-"))
        if not name:
            await interaction.followup.send("Invalid name — use only letters, numbers, hyphens.", ephemeral=True)
            return
        try:
            await interaction.channel.edit(name=name)
            await interaction.followup.send(f"✅ Channel renamed to **{name}**.", ephemeral=True)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to rename: {e}", ephemeral=True)


class NoteModal(Modal, title="Add Internal Note"):
    note = TextInput(
        label="Staff Note",
        placeholder="Write your internal note here…",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="📝 Internal Staff Note",
            description=self.note.value,
            color=0x9B59B6,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=f"Note by {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_footer(text="🔒 This note is only visible to staff")
        # Post in channel — staff can see it; no perm for user
        try:
            from utils.config import Config
            from utils.helpers import is_staff
            config = _get_bot(interaction).config
            if not is_staff(interaction.user, config):
                await interaction.followup.send("Only staff can add notes.", ephemeral=True)
                return
        except Exception:
            pass
        await interaction.channel.send(embed=embed)
        await interaction.followup.send("Note posted.", ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Priority Select
# ─────────────────────────────────────────────────────────────────────────────

class PrioritySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Low",    value="low",    emoji="🟢", description="Non-urgent issue"),
            discord.SelectOption(label="Medium", value="medium", emoji="🟡", description="Moderate priority"),
            discord.SelectOption(label="High",   value="high",   emoji="🔴", description="Critical / urgent issue"),
        ]
        super().__init__(
            custom_id="ticket_priority_select",
            placeholder="🏷️  Set ticket priority…",
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.followup.send("Only staff can change priority.", ephemeral=True)
            return
        priority = self.values[0]
        cog = bot.get_cog("Tickets")
        if cog:
            await cog.set_priority(interaction, priority)


# ─────────────────────────────────────────────────────────────────────────────
# Ticket Control View (buttons inside ticket channel)
# ─────────────────────────────────────────────────────────────────────────────

class TicketControlView(View):
    """Persistent control panel inside every ticket channel."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PrioritySelect())

    # ── Close ────────────────────────────────────────────────────────
    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket_close_btn",
        row=1,
    )
    async def close_btn(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.send_modal(CloseConfirmModal())

    # ── Claim ────────────────────────────────────────────────────────
    @discord.ui.button(
        label="Claim",
        style=discord.ButtonStyle.success,
        emoji="✋",
        custom_id="ticket_claim_btn",
        row=1,
    )
    async def claim_btn(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.followup.send("Only staff can claim tickets.", ephemeral=True)
            return
        cog = bot.get_cog("Staff")
        if cog:
            await cog.claim_ticket(interaction)

    # ── Unclaim ──────────────────────────────────────────────────────
    @discord.ui.button(
        label="Unclaim",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="ticket_unclaim_btn",
        row=1,
    )
    async def unclaim_btn(self, interaction: discord.Interaction, button: Button) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.followup.send("Only staff can unclaim tickets.", ephemeral=True)
            return
        cog = bot.get_cog("Staff")
        if cog:
            await cog.unclaim_ticket(interaction)

    # ── Add User ─────────────────────────────────────────────────────
    @discord.ui.button(
        label="Add User",
        style=discord.ButtonStyle.primary,
        emoji="➕",
        custom_id="ticket_add_user_btn",
        row=2,
    )
    async def add_user_btn(self, interaction: discord.Interaction, button: Button) -> None:
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.response.send_message("Only staff can add users.", ephemeral=True)
            return
        await interaction.response.send_modal(AddUserModal())

    # ── Remove User ──────────────────────────────────────────────────
    @discord.ui.button(
        label="Remove User",
        style=discord.ButtonStyle.secondary,
        emoji="➖",
        custom_id="ticket_remove_user_btn",
        row=2,
    )
    async def remove_user_btn(self, interaction: discord.Interaction, button: Button) -> None:
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.response.send_message("Only staff can remove users.", ephemeral=True)
            return
        await interaction.response.send_modal(RemoveUserModal())

    # ── Rename ───────────────────────────────────────────────────────
    @discord.ui.button(
        label="Rename",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        custom_id="ticket_rename_btn",
        row=2,
    )
    async def rename_btn(self, interaction: discord.Interaction, button: Button) -> None:
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.response.send_message("Only staff can rename tickets.", ephemeral=True)
            return
        await interaction.response.send_modal(RenameModal())

    # ── Note ─────────────────────────────────────────────────────────
    @discord.ui.button(
        label="Add Note",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        custom_id="ticket_note_btn",
        row=2,
    )
    async def note_btn(self, interaction: discord.Interaction, button: Button) -> None:
        bot = _get_bot(interaction)
        from utils.helpers import is_staff
        if not is_staff(interaction.user, bot.config):
            await interaction.response.send_message("Only staff can add notes.", ephemeral=True)
            return
        await interaction.response.send_modal(NoteModal())


# ─────────────────────────────────────────────────────────────────────────────
# Rating View
# ─────────────────────────────────────────────────────────────────────────────

class RatingView(View):
    """Persistent rating buttons sent to users when their ticket closes."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _rate(self, interaction: discord.Interaction, stars: int) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = _get_bot(interaction)
        cog = bot.get_cog("Ratings")
        if cog:
            await cog.submit_rating(interaction, stars)
        # Disable all buttons after rating
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="⭐", custom_id="rate_1", style=discord.ButtonStyle.secondary)
    async def r1(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._rate(interaction, 1)

    @discord.ui.button(label="⭐⭐", custom_id="rate_2", style=discord.ButtonStyle.secondary)
    async def r2(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._rate(interaction, 2)

    @discord.ui.button(label="⭐⭐⭐", custom_id="rate_3", style=discord.ButtonStyle.primary)
    async def r3(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._rate(interaction, 3)

    @discord.ui.button(label="⭐⭐⭐⭐", custom_id="rate_4", style=discord.ButtonStyle.primary)
    async def r4(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._rate(interaction, 4)

    @discord.ui.button(label="⭐⭐⭐⭐⭐", custom_id="rate_5", style=discord.ButtonStyle.success)
    async def r5(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._rate(interaction, 5)
