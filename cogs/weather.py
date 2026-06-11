"""Weather lookups via wttr.in (no API key required).

/weather defaults to Myerstown, PA (ZIP 17067) and accepts any city,
ZIP code, or airport code.
"""

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("opsbot.weather")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

CONDITION_EMOJI = (
    ("thunder", "⛈️"),
    ("snow", "❄️"),
    ("sleet", "🌨️"),
    ("rain", "🌧️"),
    ("drizzle", "🌦️"),
    ("shower", "🌧️"),
    ("fog", "🌫️"),
    ("mist", "🌫️"),
    ("overcast", "☁️"),
    ("cloud", "⛅"),
    ("sun", "☀️"),
    ("clear", "🌙"),
)


def emoji_for(description: str) -> str:
    lowered = description.lower()
    for keyword, emoji in CONDITION_EMOJI:
        if keyword in lowered:
            return emoji
    return "🌡️"


class Weather(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="weather",
        description=f"Current weather (default: {config.WEATHER_DEFAULT_LOCATION})",
    )
    @app_commands.describe(location="City, ZIP code, or airport code (optional)")
    @app_commands.checks.cooldown(4, 60.0)
    async def weather(
        self, interaction: discord.Interaction, location: str | None = None
    ) -> None:
        place = (location or config.WEATHER_DEFAULT_LOCATION).strip()
        await interaction.response.defer()

        url = f"https://wttr.in/{quote(place)}"
        try:
            async with self.bot.session.get(
                url, params={"format": "j1"}, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    await interaction.followup.send(
                        f"⚠️ The weather service returned HTTP {resp.status} for "
                        f"`{place}` — try a different location."
                    )
                    return
                # wttr.in sometimes labels JSON as text/plain
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await interaction.followup.send(
                "⚠️ Couldn't reach the weather service — try again in a minute."
            )
            return

        try:
            embed = self._build_embed(data)
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("Unexpected wttr.in payload for %r", place)
            await interaction.followup.send(
                f"⚠️ Got an unexpected response for `{place}` — is that a real place?"
            )
            return

        await interaction.followup.send(embed=embed)

    @staticmethod
    def _build_embed(data: dict) -> discord.Embed:
        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        today = data["weather"][0]

        place_name = area["areaName"][0]["value"]
        region = area["region"][0]["value"]
        description = current["weatherDesc"][0]["value"]
        rain_chance = max(
            (int(hour.get("chanceofrain", 0)) for hour in today.get("hourly", [])),
            default=0,
        )

        embed = discord.Embed(
            title=f"{emoji_for(description)} Weather — {place_name}, {region}",
            description=f"**{description}**",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Temperature",
            value=f"{current['temp_F']}°F (feels like {current['FeelsLikeF']}°F)",
        )
        embed.add_field(name="Humidity", value=f"{current['humidity']}%")
        embed.add_field(
            name="Wind",
            value=f"{current['winddir16Point']} {current['windspeedMiles']} mph",
        )
        embed.add_field(
            name="Today",
            value=f"High {today['maxtempF']}°F / Low {today['mintempF']}°F",
        )
        embed.add_field(name="Chance of rain", value=f"{rain_chance}%")
        if current.get("visibilityMiles"):
            embed.add_field(name="Visibility", value=f"{current['visibilityMiles']} mi")
        embed.set_footer(text="Data from wttr.in")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Weather(bot))
