"""Admin & configuration commands.

/setchannel routes bot output (alerts, bugs, suggestions, steam) to a
channel per guild; /settings shows the current routing; /botstatus shows
health and statistics.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.checks import is_admin

log = logging.getLogger("opsbot.admin")

CHANNEL_KINDS = {
    "alerts": "Website down/up alerts",
    "bugs": "Bug reports",
    "suggestions": "Suggestions",
    "steam": "Steam update notifications",
}


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="setchannel",
        description="Choose where the bot posts alerts, bugs, suggestions, or Steam news (admin)",
    )
    @app_commands.guild_only()
    @app_commands.describe(kind="What kind of posts to route", channel="Destination channel")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name=f"{kind} — {label}", value=kind)
            for kind, label in CHANNEL_KINDS.items()
        ]
    )
    @is_admin()
    async def setchannel(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            await interaction.response.send_message(
                f"⚠️ I can't post embeds in {channel.mention} — give me **Send Messages** "
                "and **Embed Links** there first.",
                ephemeral=True,
            )
            return
        await self.bot.db.set_setting(
            interaction.guild_id, f"{kind.value}_channel", str(channel.id)
        )
        log.info("Guild %d routed %s to #%s", interaction.guild_id, kind.value, channel)
        await interaction.response.send_message(
            f"✅ **{kind.value}** posts will now go to {channel.mention}."
        )

    @app_commands.command(name="settings", description="Show this server's bot configuration (admin)")
    @app_commands.guild_only()
    @is_admin()
    async def settings(self, interaction: discord.Interaction) -> None:
        lines = []
        for kind, label in CHANNEL_KINDS.items():
            channel = await self.bot.get_configured_channel(interaction.guild_id, kind)
            target = channel.mention if channel is not None else "*not set*"
            lines.append(f"**{kind}** ({label}): {target}")
        embed = discord.Embed(
            title="⚙️ Server configuration",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Change destinations with /setchannel")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="botstatus", description="Bot health and statistics")
    @app_commands.guild_only()
    async def botstatus(self, interaction: discord.Interaction) -> None:
        uptime = discord.utils.utcnow() - self.bot.started_at
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60

        sites = await self.bot.db.count_sites(interaction.guild_id)
        watches = len(await self.bot.db.list_steam_watches(interaction.guild_id))
        open_bugs = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS n FROM bug_reports WHERE guild_id = $1 AND status = 'open'",
            interaction.guild_id,
        )

        embed = discord.Embed(title="🤖 Bot status", color=discord.Color.green())
        embed.add_field(name="Uptime", value=f"{hours}h {minutes}m")
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms")
        embed.add_field(name="Monitored sites", value=str(sites))
        embed.add_field(name="Steam watches", value=str(watches))
        embed.add_field(name="Open bugs", value=str(open_bugs["n"]))
        embed.add_field(
            name="Check interval", value=f"{config.WEB_CHECK_INTERVAL_MINUTES} min"
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
