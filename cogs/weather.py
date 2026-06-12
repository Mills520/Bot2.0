"""Weather lookups straight from the National Weather Service (api.weather.gov).

/weather defaults to Myerstown, PA (ZIP 17067) and accepts any US city or
ZIP code. The place is geocoded once via OpenStreetMap Nominatim, then the
NWS gridpoint URLs are cached so repeat lookups skip the extra round-trips.
No API key required; NWS only covers the United States.
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("opsbot.weather")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NWS_POINTS_URL = "https://api.weather.gov/points/{lat:.4f},{lon:.4f}"

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

COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def emoji_for(description: str) -> str:
    lowered = description.lower()
    for keyword, emoji in CONDITION_EMOJI:
        if keyword in lowered:
            return emoji
    return "🌡️"


def c_to_f(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def compass(degrees: float | None) -> str:
    if degrees is None:
        return ""
    return COMPASS_POINTS[round(degrees / 22.5) % 16]


class LocationError(Exception):
    """Raised with a user-facing message when a place can't be resolved."""


class Weather(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # place string -> (display name, forecast URL, latest-observation URL)
        self._grid_cache: dict[str, tuple[str, str, str | None]] = {}

    async def _fetch_json(self, url: str, params: dict | None = None):
        async with self.bot.session.get(
            url, params=params, timeout=REQUEST_TIMEOUT
        ) as resp:
            resp.raise_for_status()
            # some endpoints label JSON as application/geo+json
            return await resp.json(content_type=None)

    async def _resolve(self, place: str) -> tuple[str, str, str | None]:
        """Geocode `place` and look up its NWS gridpoint URLs (cached)."""
        key = place.lower()
        if key in self._grid_cache:
            return self._grid_cache[key]

        results = await self._fetch_json(
            NOMINATIM_URL,
            params={"q": place, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
        )
        if not results:
            raise LocationError(f"Couldn't find `{place}` — is that a real US place?")
        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])

        try:
            points = await self._fetch_json(NWS_POINTS_URL.format(lat=lat, lon=lon))
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                raise LocationError(
                    f"`{place}` is outside National Weather Service coverage (US only)."
                ) from exc
            raise
        props = points["properties"]
        rel = props["relativeLocation"]["properties"]
        name = f"{rel['city']}, {rel['state']}"

        # Nearest observation station for current conditions; the forecast
        # still works without one, so a failure here is non-fatal.
        obs_url = None
        try:
            stations = await self._fetch_json(
                props["observationStations"], params={"limit": 1}
            )
            station = stations["features"][0]["properties"]["stationIdentifier"]
            obs_url = f"https://api.weather.gov/stations/{station}/observations/latest"
        except (aiohttp.ClientError, asyncio.TimeoutError, LookupError):
            log.warning("No observation station found near %s", name)

        resolved = (name, props["forecast"], obs_url)
        self._grid_cache[key] = resolved
        return resolved

    @app_commands.command(
        name="weather",
        description=f"US weather from the NWS (default: {config.WEATHER_DEFAULT_LOCATION})",
    )
    @app_commands.describe(location="US city or ZIP code (optional)")
    @app_commands.checks.cooldown(4, 60.0)
    async def weather(
        self, interaction: discord.Interaction, location: str | None = None
    ) -> None:
        place = (location or config.WEATHER_DEFAULT_LOCATION).strip()
        await interaction.response.defer()

        try:
            name, forecast_url, obs_url = await self._resolve(place)
            forecast = await self._fetch_json(forecast_url)
            observation = None
            if obs_url:
                try:
                    observation = await self._fetch_json(obs_url)
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass  # station offline; embed falls back to forecast data
        except LocationError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await interaction.followup.send(
                "⚠️ Couldn't reach the weather service — try again in a minute."
            )
            return

        try:
            embed = self._build_embed(name, forecast, observation)
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("Unexpected NWS payload for %r", place)
            await interaction.followup.send(
                f"⚠️ Got an unexpected response for `{place}` — try again later."
            )
            return

        await interaction.followup.send(embed=embed)

    @staticmethod
    def _build_embed(
        name: str, forecast: dict, observation: dict | None
    ) -> discord.Embed:
        periods = forecast["properties"]["periods"]
        now = periods[0]

        # First two periods cover today + tonight (or tonight + tomorrow)
        high = next((p["temperature"] for p in periods[:2] if p["isDaytime"]), None)
        low = next((p["temperature"] for p in periods[:2] if not p["isDaytime"]), None)
        rain_chance = max(
            (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
            for p in periods[:2]
        )

        obs = (observation or {}).get("properties") or {}
        description = obs.get("textDescription") or now["shortForecast"]

        embed = discord.Embed(
            title=f"{emoji_for(description)} Weather — {name}",
            description=f"**{description}**",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        # Observations are SI (°C, km/h, m) and any value can be null on a
        # stale station, so fall back to the forecast's first period.
        temp_c = (obs.get("temperature") or {}).get("value")
        if temp_c is not None:
            feels_c = (obs.get("heatIndex") or {}).get("value")
            if feels_c is None:
                feels_c = (obs.get("windChill") or {}).get("value")
            feels_f = c_to_f(feels_c) if feels_c is not None else c_to_f(temp_c)
            embed.add_field(
                name="Temperature",
                value=f"{c_to_f(temp_c):.0f}°F (feels like {feels_f:.0f}°F)",
            )
        else:
            embed.add_field(name="Temperature", value=f"{now['temperature']}°F")

        humidity = (obs.get("relativeHumidity") or {}).get("value")
        if humidity is not None:
            embed.add_field(name="Humidity", value=f"{humidity:.0f}%")

        wind_kmh = (obs.get("windSpeed") or {}).get("value")
        if wind_kmh is not None:
            direction = compass((obs.get("windDirection") or {}).get("value"))
            embed.add_field(
                name="Wind", value=f"{direction} {wind_kmh / 1.609344:.0f} mph".strip()
            )
        else:
            embed.add_field(name="Wind", value=f"{now['windDirection']} {now['windSpeed']}")

        if high is not None and low is not None:
            embed.add_field(name="Today", value=f"High {high}°F / Low {low}°F")
        elif low is not None:
            embed.add_field(name="Tonight", value=f"Low {low}°F")

        embed.add_field(name="Chance of rain", value=f"{rain_chance}%")

        visibility_m = (obs.get("visibility") or {}).get("value")
        if visibility_m is not None:
            embed.add_field(name="Visibility", value=f"{visibility_m / 1609.344:.0f} mi")

        embed.set_footer(text="Data from the National Weather Service (weather.gov)")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Weather(bot))
