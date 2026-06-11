"""Suggestions.

/suggestions posts the user's idea as a numbered embed in the configured
suggestions channel and adds 👍 / 👎 reactions for voting.
"""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("opsbot.suggestions")


class Suggestions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="suggestions", description="Submit a suggestion for this server")
    @app_commands.guild_only()
    @app_commands.describe(suggestion="Your idea (5–1000 characters)")
    @app_commands.checks.cooldown(2, 120.0)
    async def suggestions(
        self,
        interaction: discord.Interaction,
        suggestion: app_commands.Range[str, 5, 1000],
    ) -> None:
        channel = await self.bot.get_configured_channel(interaction.guild_id, "suggestions")
        if channel is None:
            await interaction.response.send_message(
                "⚠️ No suggestions channel is configured yet — an admin needs to run "
                "`/setchannel kind:suggestions` first.",
                ephemeral=True,
            )
            return

        suggestion_id = await self.bot.db.add_suggestion(
            interaction.guild_id, interaction.user.id, str(interaction.user), suggestion
        )

        embed = discord.Embed(
            title=f"💡 Suggestion #{suggestion_id}",
            description=suggestion,
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(interaction.user), icon_url=interaction.user.display_avatar.url
        )
        embed.set_footer(text="Vote with the reactions below")

        try:
            posted = await channel.send(embed=embed)
            await posted.add_reaction("👍")
            await posted.add_reaction("👎")
        except discord.HTTPException:
            log.exception("Could not post suggestion #%d", suggestion_id)
            await interaction.response.send_message(
                "⚠️ Your suggestion was saved, but I couldn't post it to the "
                "suggestions channel (check my permissions there).",
                ephemeral=True,
            )
            return

        await self.bot.db.set_suggestion_message(suggestion_id, posted.id, channel.id)
        log.info("Suggestion #%d posted by %s", suggestion_id, interaction.user)
        await interaction.response.send_message(
            f"✅ Suggestion **#{suggestion_id}** posted in {channel.mention}!",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Suggestions(bot))
