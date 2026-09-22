import discord
from discord import app_commands
from discord.ext import commands

class WebHelp(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="webhelp",
        description="Open PupPet Help Site"
    )
    async def webhelp(self, interaction: discord.Interaction):

        url = "https://helppuppy.netlify.app"

        embed = discord.Embed(
            title="🐶 PupPet Help Panel",
            description="Click the button below to open PupPet help website.",
            color=discord.Color.purple()
        )

        embed.set_footer(text="PupPet • Help System")

        view = discord.ui.View()

        button = discord.ui.Button(
            label="Open Help Site",
            style=discord.ButtonStyle.link,
            url=url
        )

        view.add_item(button)

        await interaction.response.send_message(
            embed=embed,
            view=view
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(WebHelp(bot))