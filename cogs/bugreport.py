"""Bug reporting.

/bugreport opens a modal (title / description / steps). Reports are stored
in SQLite and posted as an embed to the guild's bugs channel. Admins can
list and resolve them with /buglist and /bugresolve.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.checks import is_admin

log = logging.getLogger("opsbot.bugreport")

SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_COLORS = {
    "low": discord.Color.blue(),
    "medium": discord.Color.gold(),
    "high": discord.Color.orange(),
    "critical": discord.Color.red(),
}


class BugReportModal(discord.ui.Modal, title="Submit a bug report"):
    bug_title = discord.ui.TextInput(
        label="Title", max_length=100, placeholder="Short summary of the bug"
    )
    description = discord.ui.TextInput(
        label="What happened?",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        placeholder="What you did, what you expected, what actually happened",
    )
    steps = discord.ui.TextInput(
        label="Steps to reproduce (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, cog: "BugReports", severity: str):
        super().__init__()
        self.cog = cog
        self.severity = severity

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_report(
            interaction,
            title=str(self.bug_title),
            description=str(self.description),
            steps=str(self.steps),
            severity=self.severity,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Bug report modal failed", exc_info=error)
        message = "⚠️ Could not submit your report — please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class BugReports(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="bugreport", description="Report a bug to the server admins")
    @app_commands.guild_only()
    @app_commands.describe(severity="How bad is it? (default: medium)")
    @app_commands.choices(
        severity=[app_commands.Choice(name=s.title(), value=s) for s in SEVERITIES]
    )
    @app_commands.checks.cooldown(3, 300.0)
    async def bugreport(
        self,
        interaction: discord.Interaction,
        severity: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        chosen = severity.value if severity else "medium"
        await interaction.response.send_modal(BugReportModal(self, chosen))

    async def submit_report(
        self, interaction: discord.Interaction,
        title: str, description: str, steps: str, severity: str,
    ) -> None:
        report_id = await self.bot.db.add_bug(
            interaction.guild_id, interaction.user.id, str(interaction.user),
            title, description, steps, severity,
        )
        log.info("Bug #%d (%s) filed by %s", report_id, severity, interaction.user)

        embed = discord.Embed(
            title=f"🐛 Bug #{report_id}: {title}",
            description=description,
            color=SEVERITY_COLORS[severity],
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=str(interaction.user), icon_url=interaction.user.display_avatar.url
        )
        embed.add_field(name="Severity", value=severity.title())
        if steps:
            embed.add_field(name="Steps to reproduce", value=steps, inline=False)
        embed.set_footer(text=f"/bugresolve report_id:{report_id} to close")

        channel = await self.bot.get_configured_channel(interaction.guild_id, "bugs")
        delivered = False
        if channel is not None:
            try:
                await channel.send(embed=embed)
                delivered = True
            except discord.HTTPException:
                log.exception("Could not post bug #%d to the bugs channel", report_id)

        message = f"✅ Bug report **#{report_id}** submitted — thanks!"
        if not delivered:
            message += (
                "\n⚠️ It was saved to the database, but no admin bug channel is "
                "configured (an admin can set one with `/setchannel`)."
            )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="buglist", description="List open bug reports (admin)")
    @app_commands.guild_only()
    @is_admin()
    async def buglist(self, interaction: discord.Interaction) -> None:
        bugs = await self.bot.db.list_bugs(interaction.guild_id, status="open", limit=10)
        if not bugs:
            await interaction.response.send_message("No open bug reports. 🎉", ephemeral=True)
            return
        lines = [
            f"**#{bug['id']}** [{bug['severity']}] {bug['title']} — "
            f"<@{bug['user_id']}>, {bug['created_at'][:10]}"
            for bug in bugs
        ]
        embed = discord.Embed(
            title=f"Open bug reports (latest {len(bugs)})",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bugresolve", description="Mark a bug report as resolved (admin)")
    @app_commands.guild_only()
    @app_commands.describe(report_id="The bug number shown in /buglist or the report embed")
    @is_admin()
    async def bugresolve(
        self, interaction: discord.Interaction, report_id: app_commands.Range[int, 1]
    ) -> None:
        changed = await self.bot.db.set_bug_status(interaction.guild_id, report_id, "resolved")
        if changed:
            await interaction.response.send_message(f"✅ Bug **#{report_id}** marked resolved.")
        else:
            await interaction.response.send_message(
                f"⚠️ No bug **#{report_id}** found for this server.", ephemeral=True
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BugReports(bot))
