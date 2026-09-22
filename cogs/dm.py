import discord
import datetime
from discord.ext import commands
from discord import app_commands
from utils.helpers import admin_check

class DMCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="dm",
        description="📨 Send a DM to a user through the bot."
    )
    @app_commands.describe(
        user="The user to DM",
        message="The message to send",
        anonymous="Hide your name from the DM (default: True)"
    )
    @admin_check()
    async def dm(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        message: str,
        anonymous: bool = True
    ):
        # Must be in a guild
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.",
                ephemeral=True
            )
            return

        # Prevent DMing yourself or the bot
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You can't send a DM to yourself!",
                ephemeral=True
            )
            return

        if user.bot:
            await interaction.response.send_message(
                "❌ You can't send a DM to a bot.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # Build the embed
            embed = discord.Embed(
                description=message,
                color=0x5865F2,  # Discord Blurple
                timestamp=datetime.datetime.now(datetime.timezone.utc)
            )

            embed.set_author(
                name="📨 Message from PupPet",
                icon_url=interaction.guild.icon.url if interaction.guild.icon else discord.Embed.Empty
            )

            embed.set_thumbnail(
                url=interaction.guild.icon.url if interaction.guild.icon else discord.Embed.Empty
            )

            if anonymous:
                embed.set_footer(
                    text=f"Sent by PupPet Staff  •  {interaction.guild.name}"
                )
            else:
                embed.set_footer(
                    text=f"Sent by {interaction.user.display_name}  •  {interaction.guild.name}",
                    icon_url=interaction.user.display_avatar.url
                )

            await user.send(embed=embed)

            # Success feedback embed
            success_embed = discord.Embed(
                description=f"✅ Successfully sent a DM to {user.mention}.",
                color=0x57F287  # Discord Green
            )
            success_embed.add_field(name="📝 Message", value=message, inline=False)
            success_embed.add_field(name="👤 Recipient", value=f"{user} (`{user.id}`)", inline=True)
            if not anonymous:
                success_embed.add_field(name="👮 Sent by", value=interaction.user.mention, inline=True)
            else:
                success_embed.add_field(name="🎭 Mode", value="Anonymous", inline=True)

            await interaction.followup.send(embed=success_embed, ephemeral=True)

        except discord.Forbidden:
            error_embed = discord.Embed(
                description=f"❌ **{user}** has DMs disabled or has blocked the bot.",
                color=0xED4245  # Discord Red
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

        except discord.HTTPException as e:
            error_embed = discord.Embed(
                description=f"❌ Failed to send DM due to a Discord error: `{e}`",
                color=0xED4245
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)

        except Exception as e:
            error_embed = discord.Embed(
                description=f"❌ An unexpected error occurred: `{e}`",
                color=0xED4245
            )
            await interaction.followup.send(embed=error_embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(DMCog(bot))