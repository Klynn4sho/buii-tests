"""
Staff hierarchy directory: renders a PNG showing every staff-permission
role in the server, ranked by position, with member avatars and vacancy
status per role.

No interactive components are attached, so per the project's Components V2
rule (core/components.py) this stays borderless: a plain caption above the
image, no Container.
"""

import discord
from discord.ext import commands

from core.components import SimpleImageLayout, footer_line
from core.hierarchy_utils import is_staff_role, build_hierarchy_image


class HierarchyCog(commands.Cog, name="HierarchyCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="hierarchy", aliases=["staffs", "stafflist"],
        description="Render a staff directory image showing every moderation role and its members."
    )
    @commands.cooldown(rate=1, per=15.0, type=commands.BucketType.guild)
    async def hierarchy(self, ctx: commands.Context):
        # Slash-invoked commands must ack within 3s or Discord shows
        # "This interaction failed" — fetching several member/role-icon
        # images sequentially can easily take longer than that, so defer
        # immediately for interaction-based invocations. Prefix invocations
        # (ctx.interaction is None) have no such deadline.
        if ctx.interaction:
            await ctx.defer()

        # role.members relies on the member cache. The bot's member intent
        # is enabled and discord.py auto-chunks guilds at startup by
        # default when that intent is on, so this is usually a no-op — but
        # it's a cheap guard against a guild that somehow isn't chunked yet
        # (the original script's `await ctx.guild.fetch_members()` here was
        # actually a bug: fetch_members() returns an async iterator, not a
        # coroutine, so awaiting it directly raises a TypeError).
        if not ctx.guild.chunked:
            await ctx.guild.chunk()

        staff_roles = [role for role in ctx.guild.roles if is_staff_role(role)]
        staff_roles.sort(key=lambda r: r.position, reverse=True)

        if not staff_roles:
            await ctx.send("No staff roles found.")
            return

        buffer, total_staff, vacant_count = await build_hierarchy_image(staff_roles, self.bot.http_session)

        file = discord.File(fp=buffer, filename="hierarchy.png")
        # total_staff/vacant_count were computed but never actually shown
        # to the user before — surfacing them here as a caption is a real
        # improvement, not just a cosmetic port.
        header = (
            "# 🪪 SERVER HIERARCHY\n"
            f"-# {len(staff_roles)} tiers • {total_staff} staff • {vacant_count} vacant\n\n"
            + footer_line("Staff Directory")
        )
        await ctx.send(view=SimpleImageLayout(header, file), file=file)


async def setup(bot: commands.Bot):
    await bot.add_cog(HierarchyCog(bot))
