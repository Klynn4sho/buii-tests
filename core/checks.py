"""
Custom permission checks shared across cogs.
"""

from discord.ext import commands
from core import database


def has_mod_permission():
    """Passes for anyone with Manage Server *or* the guild's configured mod
    role (set via /setmodrole), so servers can delegate bot config without
    handing out full Manage Server. Falls back to manage_guild-only when no
    mod role is configured. Kept separate from the administrator-gated
    commands (sync, testjoin, songratings, renumbersongs, setmodrole itself),
    which stay admin-only regardless.
    """
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        perms = ctx.author.guild_permissions
        if perms.manage_guild or perms.administrator:
            return True
        mod_role_id = await database.async_get_mod_role(ctx.guild.id)
        if mod_role_id and any(role.id == mod_role_id for role in getattr(ctx.author, "roles", [])):
            return True
        raise commands.MissingPermissions(["manage_guild"])
    return commands.check(predicate)
