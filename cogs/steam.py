"""Steam update monitoring.

Watches Steam apps via the official ISteamNews feed (no API key needed) —
update detection is news-based: when an app publishes a new news item
(patch notes are tagged and highlighted), the configured channel is
notified. /steam add|remove|list manage watches; /forceupdate checks now.
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.database import utcnow

log = logging.getLogger("opsbot.steam")

NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class SteamMonitor(commands.Cog):
    steam = app_commands.Group(
        name="steam",
        description="Steam update monitoring",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.update_loop.change_interval(minutes=config.STEAM_CHECK_INTERVAL_MINUTES)
        self.update_loop.start()

    async def cog_unload(self) -> None:
        self.update_loop.cancel()

    # -- Steam API helpers -----------------------------------------------------

    async def fetch_news(self, app_id: int) -> list[dict]:
        """Latest news items for an app, newest first. Returns [] on failure."""
        params = {"appid": app_id, "count": 5, "maxlength": 400}
        try:
            async with self.bot.session.get(
                NEWS_URL, params=params, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    log.warning("Steam news API returned %d for app %d", resp.status, app_id)
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Steam news fetch failed for app %d: %s", app_id, exc)
            return []
        return data.get("appnews", {}).get("newsitems", [])

    async def fetch_app_name(self, app_id: int) -> str | None:
        try:
            async with self.bot.session.get(
                APPDETAILS_URL, params={"appids": app_id}, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        entry = data.get(str(app_id)) or {}
        if entry.get("success") and entry.get("data"):
            return entry["data"].get("name")
        return None

    # -- background loop -----------------------------------------------------------

    @tasks.loop(minutes=15)
    async def update_loop(self) -> None:
        watches = await self.bot.db.all_steam_watches()
        for watch in watches:
            try:
                await self.check_watch(watch)
            except Exception:
                log.exception("Steam check failed for app %d", watch["app_id"])
            await asyncio.sleep(1)  # be polite to the Steam API

    @update_loop.before_loop
    async def before_update_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def check_watch(self, watch) -> list[dict]:
        """Check one watch; posts alerts for anything new. Returns the new items."""
        items = await self.fetch_news(watch["app_id"])
        if not items:
            return []

        newest_gid = str(items[0]["gid"])
        last_seen = watch["last_news_gid"]
        await self.bot.db.update_steam_watch(
            watch["id"], last_news_gid=newest_gid, last_checked=utcnow()
        )

        # First successful check only sets the baseline — no retroactive alerts.
        if last_seen is None or newest_gid == last_seen:
            return []

        new_items = []
        for item in items:
            if str(item["gid"]) == last_seen:
                break
            new_items.append(item)

        for item in reversed(new_items):  # announce oldest first
            await self._announce(watch, item)
        return new_items

    async def _announce(self, watch, item: dict) -> None:
        channel = await self.bot.get_configured_channel(watch["guild_id"], "steam")
        if channel is None:  # fall back to the general alerts channel
            channel = await self.bot.get_configured_channel(watch["guild_id"], "alerts")
        if channel is None:
            log.warning(
                "No steam/alerts channel configured for guild %s — use /setchannel",
                watch["guild_id"],
            )
            return

        is_patch = "patchnotes" in (item.get("tags") or [])
        name = watch["app_name"] or f"App {watch['app_id']}"
        embed = discord.Embed(
            title=f"{'🔧 Update detected' if is_patch else '📰 News'} — {name}",
            description=item.get("title", "(no title)"),
            url=item.get("url"),
            color=discord.Color.green() if is_patch else discord.Color.dark_blue(),
            timestamp=datetime.fromtimestamp(item.get("date", 0), tz=timezone.utc),
        )
        embed.set_footer(text=f"{item.get('feedlabel', 'Steam')} · App ID {watch['app_id']}")
        try:
            await channel.send(embed=embed)
            log.info("Announced Steam news for app %d: %s", watch["app_id"], item.get("title"))
        except discord.HTTPException:
            log.exception("Could not announce Steam news for app %d", watch["app_id"])

    # -- slash commands ----------------------------------------------------------------

    @steam.command(name="add", description="Watch a Steam app for updates/news")
    @app_commands.describe(app_id="The Steam app ID (the number in the store page URL)")
    @app_commands.checks.cooldown(3, 60.0)
    async def add_watch(
        self, interaction: discord.Interaction, app_id: app_commands.Range[int, 1]
    ) -> None:
        await interaction.response.defer()
        name = await self.fetch_app_name(app_id)
        watch_id = await self.bot.db.add_steam_watch(interaction.guild_id, app_id, name)
        if watch_id is None:
            await interaction.followup.send("ℹ️ That app is already being watched here.")
            return

        # Seed the baseline now so only future news triggers alerts.
        items = await self.fetch_news(app_id)
        if items:
            await self.bot.db.update_steam_watch(
                watch_id, last_news_gid=str(items[0]["gid"]), last_checked=utcnow()
            )

        shown = name or f"app {app_id}"
        await interaction.followup.send(
            f"✅ Now watching **{shown}** for updates "
            f"(checked every {config.STEAM_CHECK_INTERVAL_MINUTES} minutes)."
        )

    @steam.command(name="remove", description="Stop watching a Steam app")
    @app_commands.describe(app_id="The watched app ID to remove")
    async def remove_watch(
        self, interaction: discord.Interaction, app_id: app_commands.Range[int, 1]
    ) -> None:
        removed = await self.bot.db.remove_steam_watch(interaction.guild_id, app_id)
        if removed:
            await interaction.response.send_message(f"🗑️ No longer watching app {app_id}.")
        else:
            await interaction.response.send_message(
                "⚠️ That app isn't being watched here — see `/steam list`.", ephemeral=True
            )

    @remove_watch.autocomplete("app_id")
    async def remove_watch_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        watches = await self.bot.db.list_steam_watches(interaction.guild_id)
        current = current.lower()
        choices = []
        for watch in watches:
            label = f"{watch['app_name'] or 'Unknown'} ({watch['app_id']})"
            if current in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=watch["app_id"]))
        return choices[:25]

    @steam.command(name="list", description="Show watched Steam apps")
    async def list_watches(self, interaction: discord.Interaction) -> None:
        watches = await self.bot.db.list_steam_watches(interaction.guild_id)
        if not watches:
            await interaction.response.send_message(
                "No Steam apps are watched yet — add one with `/steam add`.", ephemeral=True
            )
            return
        lines = [
            f"🎮 **{watch['app_name'] or 'Unknown'}** — app ID `{watch['app_id']}`"
            f" (last checked: {watch['last_checked'] or 'never'})"
            for watch in watches
        ]
        embed = discord.Embed(
            title=f"Watched Steam apps ({len(watches)})",
            description="\n".join(lines),
            color=discord.Color.dark_blue(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="forceupdate",
        description="Check all watched Steam apps for updates right now",
    )
    @app_commands.guild_only()
    @app_commands.checks.cooldown(2, 60.0)
    async def forceupdate(self, interaction: discord.Interaction) -> None:
        watches = await self.bot.db.list_steam_watches(interaction.guild_id)
        if not watches:
            await interaction.response.send_message(
                "No Steam apps are watched yet — add one with `/steam add`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        found = 0
        for watch in watches:
            try:
                found += len(await self.check_watch(watch))
            except Exception:
                log.exception("Forced check failed for app %d", watch["app_id"])
        summary = f"Checked {len(watches)} app(s) — {found} new update/news item(s) found."
        if found:
            summary += " Alerts posted to the configured channel."
        await interaction.followup.send(summary, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamMonitor(bot))
