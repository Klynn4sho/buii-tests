"""
Staff hierarchy directory: renders a PNG showing every staff-permission
role in the server, ranked by position, with member avatars and vacancy
status per role.

Per the project's Components V2 rule (core/components.py) the response is
a plain, un-accented container: a caption above the image.
"""

import discord
from discord.ext import commands

from core.components import SimpleImageLayout, footer_line, notice
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
            await ctx.send(view=notice("No staff roles found."))
            return

        try:
            buffer, total_staff, vacant_count = await build_hierarchy_image(
                ctx.guild, staff_roles, self.bot.http_session,
                requested_by=f"Requested by {ctx.author.display_name}",
            )
        except Exception as error:
            print(f"[hierarchy] render failed for guild {ctx.guild.id}: {error!r}")
            await ctx.send(view=notice("❌ Could not render the hierarchy card. Check the bot logs for details."))
            return

        file = discord.File(fp=buffer, filename="hierarchy.png")
        # The rendered image already carries its own title, stat pills and
        # footer (server name, tier/staff/vacant counts, requester, date),
        # so the message's own text stays to a one-line tag — repeating the
        # same header/stats as Discord markdown on top of the image would
        # just be redundant clutter.
        await ctx.send(view=SimpleImageLayout(footer_line("Staff Directory"), file), file=file)


async def setup(bot: commands.Bot):
    await bot.add_cog(HierarchyCog(bot))
