"""Email breach lookups via the LeakCheck public API (free, no key required).

/checkemail replies ephemerally — only the person who ran the command can
see the result — and the address is never written to the bot's log.
"""

import asyncio
import logging
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("opsbot.breachcheck")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
API_URL = "https://leakcheck.io/api/public"
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_SOURCES_SHOWN = 12


class BreachCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="checkemail",
        description="Check whether an email address appears in known data breaches",
    )
    @app_commands.describe(email="Email address to look up (the reply is private)")
    @app_commands.checks.cooldown(3, 60.0)
    async def checkemail(self, interaction: discord.Interaction, email: str) -> None:
        email = email.strip().lower()
        if not EMAIL_RE.fullmatch(email):
            await interaction.response.send_message(
                "⚠️ That doesn't look like a valid email address.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with self.bot.session.get(
                API_URL, params={"check": email}, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status == 429:
                    await interaction.followup.send(
                        "⏳ The breach database is rate-limiting us — try again in a minute."
                    )
                    return
                if resp.status != 200:
                    await interaction.followup.send(
                        f"⚠️ The breach database returned HTTP {resp.status} — try again later."
                    )
                    return
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await interaction.followup.send(
                "⚠️ Couldn't reach the breach database — try again in a minute."
            )
            return

        # A clean address comes back as success=false / "Not found"
        error = (data.get("error") or "").lower()
        if not data.get("success") and error != "not found":
            await interaction.followup.send(
                f"⚠️ Lookup failed: {data.get('error', 'unknown error')}."
            )
            return

        await interaction.followup.send(embed=self._build_embed(email, data))

    @staticmethod
    def _build_embed(email: str, data: dict) -> discord.Embed:
        found = data.get("found", 0) if data.get("success") else 0
        if not found:
            return discord.Embed(
                title="✅ No known breaches",
                description=(
                    f"`{email}` doesn't appear in any breach this database knows about.\n"
                    "That's no guarantee — keep using unique passwords and 2FA."
                ),
                color=discord.Color.green(),
            )

        # Newest first; sources with no date sort last
        sources = sorted(
            data.get("sources", []), key=lambda s: s.get("date") or "", reverse=True
        )
        lines = []
        for source in sources[:MAX_SOURCES_SHOWN]:
            source_name = source.get("name") or "Unknown source"
            date = source.get("date")
            lines.append(f"• **{source_name}**" + (f" ({date})" if date else ""))
        if len(sources) > MAX_SOURCES_SHOWN:
            lines.append(f"…and {len(sources) - MAX_SOURCES_SHOWN} more")

        embed = discord.Embed(
            title=f"🚨 Found in {found} breach{'es' if found != 1 else ''}",
            description=f"`{email}` appears in known data breaches.",
            color=discord.Color.red(),
        )
        if lines:
            embed.add_field(name="Breached sites", value="\n".join(lines), inline=False)
        fields = data.get("fields") or []
        if fields:
            embed.add_field(
                name="Exposed data types",
                value=", ".join(fields[:12]).replace("_", " "),
                inline=False,
            )
        embed.add_field(
            name="What to do",
            value=(
                "Change the password anywhere you used it, make every password "
                "unique (use a password manager), and turn on 2FA."
            ),
            inline=False,
        )
        embed.set_footer(text="Data from LeakCheck (leakcheck.io)")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BreachCheck(bot))
