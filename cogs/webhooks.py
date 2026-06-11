"""Webhook sender.

/sendwebhook posts a message and/or embed through any Discord webhook URL.
Gated behind Manage Webhooks since a webhook URL is itself a credential.
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("opsbot.webhooks")

WEBHOOK_RE = re.compile(
    r"^https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d{15,25}/[A-Za-z0-9_\-]+$"
)


class WebhookSender(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="sendwebhook",
        description="Send a message through a Discord webhook URL",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_webhooks=True)
    @app_commands.describe(
        url="The Discord webhook URL (kept private — replies are ephemeral)",
        message="Plain-text message content",
        embed_title="Optional embed title",
        embed_description="Optional embed body",
        embed_color="Optional embed color as hex, e.g. #5865F2",
        username="Optional display-name override for the webhook",
    )
    @app_commands.checks.cooldown(3, 60.0)
    async def sendwebhook(
        self,
        interaction: discord.Interaction,
        url: str,
        message: str | None = None,
        embed_title: str | None = None,
        embed_description: str | None = None,
        embed_color: str | None = None,
        username: str | None = None,
    ) -> None:
        # default_permissions only sets the default; verify at runtime too.
        if not interaction.user.guild_permissions.manage_webhooks:
            await interaction.response.send_message(
                "🚫 You need the **Manage Webhooks** permission to use this.",
                ephemeral=True,
            )
            return

        url = url.strip()
        if not WEBHOOK_RE.match(url):
            await interaction.response.send_message(
                "⚠️ That isn't a valid Discord webhook URL. It should look like\n"
                "`https://discord.com/api/webhooks/<id>/<token>`.",
                ephemeral=True,
            )
            return
        if not message and not embed_title and not embed_description:
            await interaction.response.send_message(
                "⚠️ Provide a `message`, an embed, or both.", ephemeral=True
            )
            return

        embeds: list[discord.Embed] = []
        if embed_title or embed_description:
            color = discord.Color.blurple()
            if embed_color:
                try:
                    value = int(embed_color.lstrip("#"), 16)
                    if not 0 <= value <= 0xFFFFFF:
                        raise ValueError
                    color = discord.Color(value)
                except ValueError:
                    await interaction.response.send_message(
                        "⚠️ `embed_color` must be a hex color like `#5865F2`.",
                        ephemeral=True,
                    )
                    return
            embeds.append(
                discord.Embed(title=embed_title, description=embed_description, color=color)
            )

        await interaction.response.defer(ephemeral=True)
        webhook = discord.Webhook.from_url(url, session=self.bot.session)
        try:
            await webhook.send(content=message, embeds=embeds, username=username)
        except discord.NotFound:
            await interaction.followup.send(
                "⚠️ That webhook doesn't exist anymore (it may have been deleted).",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.warning("Webhook send failed: %s", exc)
            await interaction.followup.send(
                f"⚠️ Discord rejected the webhook message: {exc.status}.", ephemeral=True
            )
            return

        log.info("Webhook message sent by %s", interaction.user)
        await interaction.followup.send("✅ Webhook message sent.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WebhookSender(bot))
