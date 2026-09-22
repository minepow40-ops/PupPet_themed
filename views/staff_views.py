"""
views/staff_views.py
Staff-only persistent view used for internal note management.
"""

from __future__ import annotations

import discord
from discord.ui import View


class StaffNoteView(View):
    """
    Persistent placeholder view attached to staff-note embeds.
    Exists so notes can be identified and managed in future updates.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Delete Note",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="staff_note_delete_btn",
    )
    async def delete_note(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        from utils.helpers import is_staff
        bot = interaction.client
        if not is_staff(interaction.user, bot.config):
            await interaction.response.send_message(
                "Only staff can delete notes.", ephemeral=True
            )
            return
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            await interaction.followup.send("Could not delete the note.", ephemeral=True)
