"""Discord Ops Bot — entry point.

Starts the bot, opens the shared aiohttp session and the PostgreSQL
connection pool, loads every cog, and syncs slash commands.
"""

import logging
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.database import Database
from utils.logging_setup import setup_logging

log = logging.getLogger("opsbot")

COGS = [
    "cogs.admin",
    "cogs.webmonitor",
    "cogs.bugreport",
    "cogs.webhooks",
    "cogs.suggestions",
    "cogs.steam",
    "cogs.weather",
    "cogs.breachcheck",
]

# .env fallbacks used when a guild hasn't run /setchannel
ENV_CHANNEL_FALLBACKS = {
    "alerts": config.ALERT_CHANNEL_ID,
    "bugs": config.BUG_CHANNEL_ID,
    "suggestions": config.SUGGESTIONS_CHANNEL_ID,
    "steam": config.STEAM_CHANNEL_ID,
}


class OpsBot(commands.Bot):
    def __init__(self) -> None:
        # Slash commands only — no privileged intents required.
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.db = Database(config.DATABASE_URL)
        self.session: aiohttp.ClientSession | None = None
        self.started_at = discord.utils.utcnow()

    # -- lifecycle -----------------------------------------------------------

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "discord-ops-bot/1.0"}
        )
        await self.db.connect()
        log.info("PostgreSQL pool ready (schema ensured)")

        for cog in COGS:
            await self.load_extension(cog)
            log.info("Loaded extension %s", cog)

        self.tree.on_error = self.on_app_command_error

        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to guild %d", len(synced), config.GUILD_ID)
            # Guild-only mode: wipe any global registrations so stale commands
            # from earlier global syncs don't linger in Discord's picker.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            synced = await self.tree.sync()
            log.info(
                "Synced %d global commands (Discord may take up to an hour to show them)",
                len(synced),
            )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (%s) — connected to %d guild(s)",
            self.user, self.user.id, len(self.guilds),
        )

    # -- shared helpers --------------------------------------------------------

    async def get_configured_channel(
        self, guild_id: int | None, kind: str
    ) -> discord.abc.Messageable | None:
        """Resolve a destination channel for `kind` (alerts/bugs/suggestions/steam).

        Per-guild /setchannel value wins; falls back to the .env channel ID.
        Returns None if nothing is configured or the channel is gone.
        """
        channel_id: int | None = None
        if guild_id is not None:
            value = await self.db.get_setting(guild_id, f"{kind}_channel")
            if value:
                channel_id = int(value)
        if channel_id is None:
            channel_id = ENV_CHANNEL_FALLBACKS.get(kind)
        if channel_id is None:
            return None

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning("Configured %s channel %d is not reachable", kind, channel_id)
                return None
        return channel

    # -- global slash-command error handler ----------------------------------------

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"⏳ Slow down — try again in {error.retry_after:.0f} seconds."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "🚫 You don't have permission to use this command."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command only works inside a server."
        elif isinstance(error, app_commands.CheckFailure):
            message = "🚫 You can't use this command here."
        else:
            name = interaction.command.qualified_name if interaction.command else "?"
            log.error("Unhandled error in /%s", name, exc_info=error)
            message = "⚠️ Something went wrong running that command. The error has been logged."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass  # interaction expired; nothing useful to do


def main() -> None:
    setup_logging()

    if not config.DISCORD_TOKEN:
        log.critical("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    bot = OpsBot()
    # log_handler=None: we already configured logging ourselves
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
