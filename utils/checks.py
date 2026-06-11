"""Shared permission checks for slash commands."""

import discord
from discord import app_commands

import config


def is_admin():
    """Allow members with Manage Server, or the configured ADMIN_ROLE_ID role.

    Raises the appropriate app_commands error so the global error handler
    can show a friendly message.
    """

    def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        member = interaction.user
        if isinstance(member, discord.Member):
            if member.guild_permissions.manage_guild:
                return True
            if config.ADMIN_ROLE_ID and any(
                role.id == config.ADMIN_ROLE_ID for role in member.roles
            ):
                return True
        raise app_commands.MissingPermissions(["manage_guild"])

    return app_commands.check(predicate)
