"""Website monitoring.

/checkweb add|remove|list|now plus a background loop that probes every
registered URL on an interval (default 5 minutes) and alerts the guild's
alerts channel on: downtime, recovery, HTTP status changes, and response
time spikes.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils.database import utcnow

log = logging.getLogger("opsbot.webmonitor")

# Consecutive failed checks before a DOWN alert fires. 2 avoids alert spam
# from a single transient timeout (worst-case detection: 2x check interval).
FAILURES_BEFORE_ALERT = 2

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
CONCURRENT_CHECKS = 10


def valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@dataclass
class CheckResult:
    ok: bool
    status: int | None = None
    elapsed_ms: float | None = None
    error: str | None = None


class WebMonitor(commands.Cog):
    """Monitors registered URLs and alerts on downtime/recovery/changes."""

    checkweb = app_commands.Group(
        name="checkweb",
        description="Website uptime monitoring",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.monitor_loop.change_interval(minutes=config.WEB_CHECK_INTERVAL_MINUTES)
        self.monitor_loop.start()

    async def cog_unload(self) -> None:
        self.monitor_loop.cancel()

    # -- probing ------------------------------------------------------------

    async def probe(self, url: str) -> CheckResult:
        """GET a URL and time the response. Never raises."""
        start = time.perf_counter()
        try:
            async with self.bot.session.get(
                url, timeout=REQUEST_TIMEOUT, allow_redirects=True
            ) as resp:
                elapsed_ms = (time.perf_counter() - start) * 1000
                # 5xx counts as down; 4xx still means the server is alive
                # (a 200 -> 404 flip is reported as a status change instead).
                return CheckResult(ok=resp.status < 500, status=resp.status, elapsed_ms=elapsed_ms)
        except asyncio.TimeoutError:
            return CheckResult(ok=False, error="request timed out after 15s")
        except aiohttp.ClientError as exc:
            return CheckResult(ok=False, error=str(exc) or type(exc).__name__)

    # -- background loop ------------------------------------------------------

    @tasks.loop(minutes=5)
    async def monitor_loop(self) -> None:
        sites = await self.bot.db.list_all_sites()
        if not sites:
            return
        log.debug("Checking %d monitored site(s)", len(sites))
        semaphore = asyncio.Semaphore(CONCURRENT_CHECKS)

        async def run_one(site) -> None:
            async with semaphore:
                try:
                    await self.check_site(site)
                except Exception:
                    log.exception("Check failed for %s", site["url"])

        await asyncio.gather(*(run_one(site) for site in sites))

    @monitor_loop.before_loop
    async def before_monitor_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def check_site(self, site) -> None:
        """Probe one site, persist the result, and alert on state transitions."""
        result = await self.probe(site["url"])
        updates: dict = {"last_checked": utcnow()}

        if result.ok:
            updates["last_response_ms"] = round(result.elapsed_ms, 1)
            updates["consecutive_failures"] = 0

            # Exponential rolling average smooths out one-off spikes.
            average = site["avg_response_ms"]
            new_average = (
                result.elapsed_ms if average is None
                else 0.8 * average + 0.2 * result.elapsed_ms
            )
            updates["avg_response_ms"] = round(new_average, 1)

            if not site["is_up"]:
                # Recovery
                updates["is_up"] = 1
                updates["down_since"] = None
                downtime = self._downtime_text(site["down_since"])
                await self._record_and_alert(
                    site, "up",
                    f"Back online with HTTP {result.status}{downtime}.",
                    discord.Color.green(), "🟢 Site is back UP",
                )
            elif (
                site["last_status_code"] is not None
                and result.status != site["last_status_code"]
            ):
                # Still up, but the status code flipped (e.g. 200 -> 404)
                await self._record_and_alert(
                    site, "status_change",
                    f"HTTP status changed: {site['last_status_code']} → {result.status}.",
                    discord.Color.gold(), "🟡 Status code changed",
                )

            # Response time spike: above the slow threshold AND 3x the average.
            if average is not None and result.elapsed_ms > max(
                config.SLOW_RESPONSE_MS, 3 * average
            ):
                if not site["was_slow"]:
                    updates["was_slow"] = 1
                    await self._record_and_alert(
                        site, "slow",
                        f"Response time spike: {result.elapsed_ms:.0f} ms "
                        f"(rolling average {average:.0f} ms).",
                        discord.Color.orange(), "🐢 Slow response",
                    )
            elif site["was_slow"]:
                updates["was_slow"] = 0

            updates["last_status_code"] = result.status
        else:
            failures = site["consecutive_failures"] + 1
            updates["consecutive_failures"] = failures
            if result.status is not None:
                updates["last_status_code"] = result.status
            reason = result.error or f"HTTP {result.status}"
            if site["is_up"] and failures >= FAILURES_BEFORE_ALERT:
                updates["is_up"] = 0
                updates["down_since"] = utcnow()
                await self._record_and_alert(
                    site, "down",
                    f"Site appears DOWN after {failures} failed checks: {reason}.",
                    discord.Color.red(), "🔴 Site DOWN",
                )

        await self.bot.db.update_site(site["id"], **updates)

    @staticmethod
    def _downtime_text(down_since: str | None) -> str:
        if not down_since:
            return ""
        try:
            started = datetime.fromisoformat(down_since)
        except ValueError:
            return ""
        minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
        if minutes < 60:
            return f" (down for ~{minutes:.0f} min)"
        return f" (down for ~{minutes / 60:.1f} h)"

    async def _record_and_alert(
        self, site, event: str, detail: str, color: discord.Color, title: str
    ) -> None:
        await self.bot.db.add_incident(site["id"], event, detail)
        log.info("[%s] %s — %s", event, site["url"], detail)

        channel = await self.bot.get_configured_channel(site["guild_id"], "alerts")
        if channel is None:
            log.warning(
                "No alerts channel configured for guild %s — use /setchannel",
                site["guild_id"],
            )
            return
        embed = discord.Embed(
            title=title, description=detail, color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="URL", value=site["url"], inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Could not send alert for %s", site["url"])

    # -- slash commands -------------------------------------------------------

    @checkweb.command(name="add", description="Start monitoring a URL (checked every few minutes)")
    @app_commands.describe(url="Full URL to monitor, including http(s)://")
    @app_commands.checks.cooldown(3, 60.0)
    async def add_site(self, interaction: discord.Interaction, url: str) -> None:
        url = url.strip().rstrip("/")
        if not valid_url(url):
            await interaction.response.send_message(
                "⚠️ That doesn't look like a valid URL — include the scheme, "
                "e.g. `https://example.com`.", ephemeral=True,
            )
            return
        if await self.bot.db.get_site(interaction.guild_id, url):
            await interaction.response.send_message(
                "ℹ️ That URL is already being monitored in this server.", ephemeral=True
            )
            return
        if await self.bot.db.count_sites(interaction.guild_id) >= config.MAX_SITES_PER_GUILD:
            await interaction.response.send_message(
                f"⚠️ This server already monitors the maximum of "
                f"{config.MAX_SITES_PER_GUILD} sites.", ephemeral=True,
            )
            return

        await interaction.response.defer()
        result = await self.probe(url)  # initial check seeds the baseline
        site_id = await self.bot.db.add_site(interaction.guild_id, url, interaction.user.id)
        if site_id is None:
            await interaction.followup.send("ℹ️ That URL is already being monitored here.")
            return

        seed: dict = {"last_checked": utcnow(), "is_up": 1 if result.ok else 0}
        if result.status is not None:
            seed["last_status_code"] = result.status
        if result.elapsed_ms is not None:
            seed["last_response_ms"] = round(result.elapsed_ms, 1)
            seed["avg_response_ms"] = round(result.elapsed_ms, 1)
        if not result.ok:
            seed["down_since"] = utcnow()
        await self.bot.db.update_site(site_id, **seed)

        embed = self._result_embed(url, result)
        embed.title = "✅ Now monitoring"
        embed.set_footer(text=f"Checked every {config.WEB_CHECK_INTERVAL_MINUTES} minutes")
        await interaction.followup.send(embed=embed)

    @checkweb.command(name="remove", description="Stop monitoring a URL")
    @app_commands.describe(url="The monitored URL to remove")
    async def remove_site(self, interaction: discord.Interaction, url: str) -> None:
        removed = await self.bot.db.remove_site(interaction.guild_id, url.strip().rstrip("/"))
        if removed:
            await interaction.response.send_message(f"🗑️ No longer monitoring {url}.")
        else:
            await interaction.response.send_message(
                "⚠️ That URL isn't being monitored here. Use `/checkweb list` to see "
                "what is.", ephemeral=True,
            )

    @remove_site.autocomplete("url")
    async def remove_site_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        sites = await self.bot.db.list_sites(interaction.guild_id)
        current = current.lower()
        return [
            app_commands.Choice(name=site["url"][:100], value=site["url"][:100])
            for site in sites
            if current in site["url"].lower()
        ][:25]

    @checkweb.command(name="list", description="Show all monitored sites and their status")
    async def list_sites(self, interaction: discord.Interaction) -> None:
        sites = await self.bot.db.list_sites(interaction.guild_id)
        if not sites:
            await interaction.response.send_message(
                "No sites are monitored yet — add one with `/checkweb add`.",
                ephemeral=True,
            )
            return
        lines = []
        for site in sites:
            emoji = "🟢" if site["is_up"] else "🔴"
            status = (
                f"HTTP {site['last_status_code']}"
                if site["last_status_code"] is not None else "no data yet"
            )
            average = (
                f" · avg {site['avg_response_ms']:.0f} ms"
                if site["avg_response_ms"] is not None else ""
            )
            lines.append(f"{emoji} **{site['url']}** — {status}{average}")
        embed = discord.Embed(
            title=f"Monitored sites ({len(sites)})",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Checked every {config.WEB_CHECK_INTERVAL_MINUTES} minutes")
        await interaction.response.send_message(embed=embed)

    @checkweb.command(name="now", description="Run a one-off check of any URL right now")
    @app_commands.describe(url="URL to check (doesn't have to be monitored)")
    @app_commands.checks.cooldown(5, 60.0)
    async def check_now(self, interaction: discord.Interaction, url: str) -> None:
        url = url.strip()
        if not valid_url(url):
            await interaction.response.send_message(
                "⚠️ That doesn't look like a valid URL — include the scheme, "
                "e.g. `https://example.com`.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        result = await self.probe(url)
        await interaction.followup.send(embed=self._result_embed(url, result))

    @staticmethod
    def _result_embed(url: str, result: CheckResult) -> discord.Embed:
        if result.ok:
            embed = discord.Embed(title="🟢 Reachable", color=discord.Color.green())
            embed.add_field(name="Status", value=f"HTTP {result.status}")
            embed.add_field(name="Response time", value=f"{result.elapsed_ms:.0f} ms")
        else:
            embed = discord.Embed(title="🔴 Unreachable", color=discord.Color.red())
            embed.add_field(
                name="Reason",
                value=result.error or f"HTTP {result.status}",
                inline=False,
            )
            if result.elapsed_ms is not None:
                embed.add_field(name="Response time", value=f"{result.elapsed_ms:.0f} ms")
        embed.add_field(name="URL", value=url, inline=False)
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WebMonitor(bot))
