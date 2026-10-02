"""
Admin utilities: the /help directory (a section-by-section Components V2
menu, see views/help_views.py) and /sync for pushing slash commands to a
guild or globally.

Help's category grouping lives in views/help_views.py (CATEGORIES), keyed
by cog name. A cog not listed there falls into "📦 Other" automatically.
"""

import discord
from discord.ext import commands

from core import database
from core.components import notice
from core.config import DEFAULT_PREFIX
from views.help_views import HelpView


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", aliases=["h"], description="Browse every command by category, with search.")
    async def help_command(self, ctx: commands.Context):
        try:
            prefix = await database.async_get_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
            view = HelpView(self.bot, ctx.author, prefix)
            view.sent_message = await ctx.send(view=view)
        except Exception as e:
            print(f"[help] failed to open help menu: {e!r}")
            try:
                await ctx.send(view=notice("❌ Couldn't open the help menu — try again in a moment."))
            except Exception:
                pass

    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.guild)
    async def sync_commands(self, ctx: commands.Context, option: str = None):
        try:
            if option == "clear":
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(view=notice("🧹 **Cleared all guild slash commands!**"))
            elif option == "global":
                synced = await self.bot.tree.sync()
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(view=notice(
                    f"🌐 **Synced {len(synced)} commands globally and removed duplicate guild copies!**"
                ))
            else:
                synced = await self.bot.tree.sync()
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                await ctx.send(view=notice(
                    f"⚡ **Synced {len(synced)} global commands and cleaned this server's duplicate copies!**"
                ))
        except discord.HTTPException as e:
            print(f"[sync] failed: {e!r}")
            await ctx.send(view=notice("❌ Discord rejected the sync request — you may be rate-limited. Try again shortly."))

    @sync_commands.error
    async def sync_commands_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(view=notice(
                f"⏱️ `sync` was just run — try again in **{error.retry_after:.0f}s**. "
                f"(This guards against tripping Discord's own command-sync rate limit.)"
            ))
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))