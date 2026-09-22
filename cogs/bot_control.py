"""
cogs/bot_control.py
Bot Configuration Control System.
Allows authorized users to update roles, channel IDs, and permissions in config.json.

🐶 Powered by PupPet — The World's Fluffiest Bot Framework™
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.helpers import COL_ERROR, COL_SUCCESS, error_embed, success_embed

log = logging.getLogger("PupPet.cogs.bot_control")

# ── Constants ───────────────────────────────────────────────────────────────────
CONTROL_ROLE_ID: int = 1551515669026177077

def _is_authorized(interaction: discord.Interaction) -> bool:
    """Check if the user has the required control role."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == CONTROL_ROLE_ID for role in interaction.user.roles)

# ── UI Components ──────────────────────────────────────────────────────────────────

class ConfigEditModal(discord.ui.Modal):
    """Modal to input a new value for a specific config key."""
    def __init__(self, key: str, current_value: Any, bot: commands.Bot):
        super().__init__(title=f"Edit {key}")
        self.key = key
        self.bot = bot

        self.input = discord.ui.TextInput(
            label=f"New value for {key}",
            default=str(current_value) if current_value is not None else "",
            placeholder="Enter the new value (ID, string, or [123, 456] for lists)",
            required=True,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.input.value
        try:
            processed_val: any = value
            if value.isdigit():
                processed_val = int(value)
            elif value.startswith("[") and value.endswith("]"):
                import ast
                try:
                    processed_val = ast.literal_eval(value)
                except Exception:
                    pass

            self.bot.config.set(self.key, processed_val)
            await interaction.response.send_message(
                embed=success_embed("Updated", f"Successfully updated `{self.key}` to `{processed_val}`."),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(
                embed=error_embed("Error", f"Failed to update `{self.key}`: {exc}"),
                ephemeral=True,
            )

class BotControlView(discord.ui.View):
    """UI View for managing bot configuration."""
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="⚙️ Edit Setting",
        style=discord.ButtonStyle.primary,
        custom_id="bot_control:edit",
    )
    async def edit_setting(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(embed=_unauthorised_embed(), ephemeral=True)

        # Get all keys from config
        config_data = self.bot.config._data
        if not config_data:
            return await interaction.response.send_message(
                embed=error_embed("No Settings", "The configuration file is empty. Add some settings first."),
                ephemeral=True
            )

        # Create a select menu for choosing the key
        view = discord.ui.View()
        select = discord.ui.Select(placeholder="Choose a setting to edit...")

        for key in config_data.keys():
            select.add_option(label=key, value=key)

        async def select_callback(inter: discord.Interaction):
            key = select.values[0]
            val = config_data.get(key)
            await inter.response.send_modal(ConfigEditModal(key, val, self.bot))

        select.callback = select_callback
        view.add_item(select)

        await interaction.response.send_message("Select the setting you want to change:", view=view, ephemeral=True)

    @discord.ui.button(
        label="🔄 Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="bot_control:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_authorized(interaction):
            return await interaction.response.send_message(embed=_unauthorised_embed(), ephemeral=True)

        self.bot.config.reload()
        await interaction.response.defer()
        await interaction.edit_original_response(embed=self.create_status_embed())

    def create_status_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🤖 PupPet Configuration Dashboard",
            description="Current active settings for the bot.",
            color=0x3498db,
        )

        # List common settings
        data = self.bot.config._data
        settings_str = ""
        for k, v in data.items():
            settings_str += f"**{k}**: `{v}`\n"

        embed.add_field(name="Active Settings", value=settings_str or "No settings found.", inline=False)
        embed.set_footer(text="🐶 PupPet Admin Tools • Authorized Access Only")
        return embed

def _unauthorised_embed() -> discord.Embed:
    return discord.Embed(
        title="❌ Unauthorized",
        description="You do not have permission to use this control.",
        color=0x607D8B,
    ).set_footer(text="🐶 PupPet Security Shield")

# ── Cog ───────────────────────────────────────────────────────────────────────────
class BotControl(commands.Cog, name="BotControl"):
    """Administrative control for bot settings."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config

    @app_commands.command(
        name="controllbot",
        description="[CONTROL ROLE ONLY] Open the Bot Configuration UI.",
    )
    async def controllbot(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not _is_authorized(interaction):
            log.warning("Unauthorized /controllbot attempt by %s (%d)", interaction.user, interaction.user.id)
            await interaction.response.send_message(
                embed=error_embed("Unauthorized", "You do not have the required Control Role to use this command."),
                ephemeral=True,
            )
            return

        view = BotControlView(self.bot)
        await interaction.response.send_message(
            embed=view.create_status_embed(),
            view=view,
            ephemeral=True,
        )

# ── Setup ─────────────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BotControl(bot))
    log.info("BotControl cog loaded.")
