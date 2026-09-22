"""
cogs/rerolepanel.py
Self-Role Panel — PupPet
─────────────────────────────────────────────────────────────────────────────
A clean, ticket-panel-style self-role system.

PUBLIC PANEL  (/rerole panel)
  • Posts a persistent embed + Select dropdown in any channel.
  • Users pick their role(s) from a dropdown — roles toggle on/off.
  • Panel survives bot restarts (persistent custom_ids).

ADMIN COMMANDS  (/rerole setup | clear | preview | roles)
  • /rerole setup   — multi-page modal wizard to set all 9 role IDs.
  • /rerole clear   — wipe saved role config for this guild.
  • /rerole preview — preview the panel embed before posting.
  • /rerole roles   — show current saved role IDs (admin only).

ROLES MANAGED
  Page 1 (5 roles)  →  PewPuppy 🐶 | PupPet 💻 | Birthday Pup 🎂 | Pup Manager | Puppy Feed
  Page 2 (4 roles)  →  Rich Puppy | Puppy | Bad Puppy | Good Boy

DB: data/rerole.json   (plain JSON, per-guild)
─────────────────────────────────────────────────────────────────────────────
🐶 Powered by PupPet
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("PupPet.rerolepanel")

# ── Storage ───────────────────────────────────────────────────────────────────

FILE = Path("data/rerole.json")

# Role display config: key → (display label, emoji, description shown in dropdown)
ROLE_META: dict[str, tuple[str, str, str]] = {
    "PewPuppy🐶":    ("PewPuppy",     "🐶", "The classic PewPuppy community role"),
    "PupPet💻":      ("PupPet",       "💻", "Tech-savvy puppy devs & enthusiasts"),
    "BirthdayPup🎂": ("Birthday Pup", "🎂", "Celebrate your birthday with the pack"),
    "PupManager":    ("Pup Manager",  "🎛️", "Server helpers & coordinators"),
    "PuppyFeed":     ("Puppy Feed",   "📰", "Get pinged for community feed updates"),
    "RichPuppy":     ("Rich Puppy",   "💰", "High-roller puppies of the server"),
    "Puppy":         ("Puppy",        "🐕", "Standard puppy membership role"),
    "BadPuppy":      ("Bad Puppy",    "😈", "Mischievous pups — you know who you are"),
    "GoodBoy":       ("Good Boy",     "⭐", "Awarded for exemplary puppy behaviour"),
}

# Colours matching the PupPet palette
COL_BRAND   = 0xFFA500   # PupPet orange
COL_SUCCESS = 0x57F287   # green
COL_ERROR   = 0xED4245   # red
COL_BLUE    = 0x5865F2   # blurple

FOOTER = "PupPet • Self-Roles"


def load_data() -> dict:
    if FILE.exists():
        with open(FILE, "r") as f:
            return json.load(f)
    return {}


def save_data(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FILE, "w") as f:
        json.dump(data, f, indent=4)


def get_guild_roles(guild_id: int) -> dict[str, str]:
    """Return the saved {key: role_id_str} map for a guild, or {}."""
    return load_data().get(str(guild_id), {})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🐶  PupPet — Self Roles",
        description=(
            "Pick the roles that suit you from the menu below.\n"
            "Selecting a role you **already have** will **remove** it.\n\n"
            "> 🐶 **PewPuppy** — Classic community member\n"
            "> 💻 **PupPet** — Tech-savvy devs & fans\n"
            "> 🎂 **Birthday Pup** — Let us celebrate you!\n"
            "> 📰 **Puppy Feed** — Community announcements\n"
            "> 💰 **Rich Puppy** — High-roller pups\n"
            "> 🐕 **Puppy** — Standard pack member\n"
            "> ⭐ **Good Boy** — Exceptional pups only\n"
            "> 😈 **Bad Puppy** — You know what you did\n\n"
            "*Use the dropdown below — roles toggle on/off instantly.*"
        ),
        color=COL_BRAND,
    )
    embed.set_footer(text=FOOTER)
    embed.timestamp = discord.utils.utcnow()
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


# ── Persistent Select Menu View ───────────────────────────────────────────────

class ReroleSelect(discord.ui.Select):
    """
    Persistent self-role dropdown.
    custom_id must be stable across restarts — do NOT include dynamic data.
    """

    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            custom_id="puppet:rerole:select",
            placeholder="🐾  Pick your roles here…",
            min_values=1,
            max_values=min(len(options), 9),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        guild_roles = get_guild_roles(interaction.guild.id)
        if not guild_roles:
            await interaction.followup.send(
                embed=discord.Embed(
                    description="❌  Role panel hasn't been configured yet. Ask an admin to run `/rerole setup`.",
                    color=COL_ERROR,
                ),
                ephemeral=True,
            )
            return

        member = interaction.user
        added, removed, failed = [], [], []

        for key in self.values:
            role_id_str = guild_roles.get(key)
            if not role_id_str:
                failed.append(key)
                continue

            role = interaction.guild.get_role(int(role_id_str))
            if not role:
                failed.append(key)
                log.warning("Role %s (id=%s) not found in guild %s", key, role_id_str, interaction.guild.id)
                continue

            try:
                if role in member.roles:
                    await member.remove_roles(role, reason="PupPet self-role panel")
                    removed.append(role.mention)
                else:
                    await member.add_roles(role, reason="PupPet self-role panel")
                    added.append(role.mention)
            except discord.Forbidden:
                failed.append(role.name)
            except discord.HTTPException as e:
                log.error("Failed to modify role %s: %s", role.name, e)
                failed.append(role.name)

        # Build response embed
        embed = discord.Embed(title="🐾  Roles Updated!", color=COL_SUCCESS)
        if added:
            embed.add_field(name="✅  Added", value=" ".join(added), inline=False)
        if removed:
            embed.add_field(name="➖  Removed", value=" ".join(removed), inline=False)
        if failed:
            embed.add_field(
                name="⚠️  Couldn't Apply",
                value=", ".join(failed) + "\n*Check the bot's role hierarchy.*",
                inline=False,
            )
        if not added and not removed and not failed:
            embed.description = "No changes made."
            embed.color = COL_BLUE

        embed.set_footer(text=FOOTER)
        await interaction.followup.send(embed=embed, ephemeral=True)


class ReroleView(discord.ui.View):
    """
    Persistent View — registered in setup_hook so it survives restarts.
    Build options dynamically from saved guild data OR show a placeholder
    when no config exists yet (so the panel embed still renders cleanly).
    """

    def __init__(self, guild_roles: Optional[dict[str, str]] = None):
        super().__init__(timeout=None)   # persistent

        options = self._build_options(guild_roles or {})

        # Always need at least 1 option for Discord to accept the component.
        if not options:
            options = [
                discord.SelectOption(
                    label="Not configured yet",
                    value="__none__",
                    emoji="⚠️",
                    description="Admin: run /rerole setup first",
                )
            ]

        self.add_item(ReroleSelect(options))

    @staticmethod
    def _build_options(guild_roles: dict[str, str]) -> list[discord.SelectOption]:
        opts = []
        for key, (label, emoji, desc) in ROLE_META.items():
            if key not in guild_roles:
                continue   # skip unconfigured roles silently
            opts.append(discord.SelectOption(
                label=label,
                value=key,
                emoji=emoji,
                description=desc,
            ))
        return opts


# ── Admin Modals ──────────────────────────────────────────────────────────────

class ReroleModalPage2(discord.ui.Modal, title="🐶 PupPet Self-Roles (2/2)"):
    """Collects the final 4 role IDs and saves everything."""

    rich_puppy = discord.ui.TextInput(
        label="Rich Puppy 💰 — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    puppy = discord.ui.TextInput(
        label="Puppy 🐕 — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    bad_puppy = discord.ui.TextInput(
        label="Bad Puppy 😈 — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    good_boy = discord.ui.TextInput(
        label="Good Boy ⭐ — Role ID",
        placeholder="e.g. 123456789012345678",
    )

    def __init__(self, base_data: dict):
        super().__init__()
        self.base_data = base_data

    async def on_submit(self, interaction: discord.Interaction):
        self.base_data.update({
            "RichPuppy":  self.rich_puppy.value.strip(),
            "Puppy":      self.puppy.value.strip(),
            "BadPuppy":   self.bad_puppy.value.strip(),
            "GoodBoy":    self.good_boy.value.strip(),
        })

        data = load_data()
        data[str(interaction.guild.id)] = self.base_data
        save_data(data)

        log.info("Rerole config saved for guild %s by %s", interaction.guild.id, interaction.user)

        embed = discord.Embed(
            title="✅  Roles Saved!",
            description=(
                "All 9 role IDs have been stored.\n\n"
                "**Next step:** run `/rerole panel` in your roles channel to post the public panel.\n"
                "Use `/rerole roles` anytime to review saved IDs."
            ),
            color=COL_SUCCESS,
        )
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ReroleNextView(discord.ui.View):
    """Single 'Next →' button leading from page 1 modal to page 2."""

    def __init__(self, base_data: dict):
        super().__init__(timeout=180)
        self.base_data = base_data

    @discord.ui.button(label="Next  ➡️", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ReroleModalPage2(self.base_data))


class ReroleModalPage1(discord.ui.Modal, title="🐶 PupPet Self-Roles (1/2)"):
    """First of two setup modals — collects 5 role IDs."""

    pewpuppy = discord.ui.TextInput(
        label="PewPuppy 🐶 — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    puppet = discord.ui.TextInput(
        label="PupPet 💻 — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    birthday_pup = discord.ui.TextInput(
        label="Birthday Pup 🎂 — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    pup_manager = discord.ui.TextInput(
        label="Pup Manager 🎛️ — Role ID",
        placeholder="e.g. 123456789012345678",
    )
    puppy_feed = discord.ui.TextInput(
        label="Puppy Feed 📰 — Role ID",
        placeholder="e.g. 123456789012345678",
    )

    async def on_submit(self, interaction: discord.Interaction):
        base_data = {
            "PewPuppy🐶":    self.pewpuppy.value.strip(),
            "PupPet💻":      self.puppet.value.strip(),
            "BirthdayPup🎂": self.birthday_pup.value.strip(),
            "PupManager":    self.pup_manager.value.strip(),
            "PuppyFeed":     self.puppy_feed.value.strip(),
        }

        embed = discord.Embed(
            description="✅ Page 1 saved! Click **Next** to set the remaining 4 roles.",
            color=COL_BLUE,
        )
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(
            embed=embed,
            view=ReroleNextView(base_data),
            ephemeral=True,
        )


# ── Cog ───────────────────────────────────────────────────────────────────────

class RerolePanel(commands.Cog):
    """
    Self-role panel system.
    /rerole panel   — post the public panel (admin)
    /rerole setup   — configure role IDs (admin, 2-page modal)
    /rerole roles   — view saved role IDs (admin)
    /rerole preview — preview the panel embed (admin)
    /rerole clear   — wipe config for this guild (admin)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Command group ─────────────────────────────────────────────────────────

    rerole = app_commands.Group(
        name="rerole",
        description="🐶 PupPet self-role panel system",
        default_permissions=discord.Permissions(manage_roles=True),
    )

    # ── /rerole setup ─────────────────────────────────────────────────────────

    @rerole.command(name="setup", description="Configure which roles appear in the self-role panel")
    async def rerole_setup(self, interaction: discord.Interaction):
        """Opens a 2-page modal wizard to set all 9 role IDs."""
        await interaction.response.send_modal(ReroleModalPage1())

    # ── /rerole panel ─────────────────────────────────────────────────────────

    @rerole.command(name="panel", description="Post the self-role panel in this channel")
    async def rerole_panel(self, interaction: discord.Interaction):
        """Post the persistent self-role panel embed + dropdown."""
        guild_roles = get_guild_roles(interaction.guild.id)

        if not guild_roles:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️  Not Configured",
                    description="Run `/rerole setup` first to configure the role IDs.",
                    color=COL_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = _panel_embed(interaction.guild)
        view  = ReroleView(guild_roles)

        # Defer + send publicly
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.send(embed=embed, view=view)

        await interaction.followup.send(
            embed=discord.Embed(
                description="✅  Self-role panel posted!",
                color=COL_SUCCESS,
            ),
            ephemeral=True,
        )
        log.info("Rerole panel posted in #%s (%s) by %s", interaction.channel, interaction.guild, interaction.user)

    # ── /rerole preview ───────────────────────────────────────────────────────

    @rerole.command(name="preview", description="Preview the panel embed (only you can see it)")
    async def rerole_preview(self, interaction: discord.Interaction):
        """Sends the panel embed ephemerally so you can check it before posting."""
        guild_roles = get_guild_roles(interaction.guild.id)
        embed = _panel_embed(interaction.guild)
        view  = ReroleView(guild_roles)
        await interaction.response.send_message(
            content="*This is a preview — only you can see this.*",
            embed=embed,
            view=view,
            ephemeral=True,
        )

    # ── /rerole roles ─────────────────────────────────────────────────────────

    @rerole.command(name="roles", description="Show currently saved self-role IDs for this server")
    async def rerole_roles(self, interaction: discord.Interaction):
        """Displays all configured role IDs for this guild."""
        guild_roles = get_guild_roles(interaction.guild.id)

        if not guild_roles:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="❌  No roles configured yet. Run `/rerole setup` first.",
                    color=COL_ERROR,
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎛️  Configured Self-Roles",
            color=COL_BRAND,
        )

        for key, role_id_str in guild_roles.items():
            meta = ROLE_META.get(key)
            if meta:
                label, emoji, _ = meta
                display = f"{emoji} {label}"
            else:
                display = key

            role = interaction.guild.get_role(int(role_id_str)) if role_id_str.isdigit() else None
            value = role.mention if role else f"`{role_id_str}` *(not found)*"
            embed.add_field(name=display, value=value, inline=True)

        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /rerole clear ─────────────────────────────────────────────────────────

    @rerole.command(name="clear", description="⚠️ Delete the role config for this server")
    async def rerole_clear(self, interaction: discord.Interaction):
        """Wipes saved role IDs for this guild. Requires confirmation."""
        data = load_data()
        guild_key = str(interaction.guild.id)

        if guild_key not in data:
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="Nothing to clear — no config saved for this server.",
                    color=COL_BLUE,
                ),
                ephemeral=True,
            )
            return

        del data[guild_key]
        save_data(data)

        log.info("Rerole config cleared for guild %s by %s", interaction.guild.id, interaction.user)

        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️  Config Cleared",
                description="Role IDs have been wiped. Run `/rerole setup` to reconfigure.",
                color=COL_ERROR,
            ),
            ephemeral=True,
        )


# ── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(RerolePanel(bot))