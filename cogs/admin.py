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
from core.components import notice, SimpleLayout
from core.config import (
    DEFAULT_PREFIX, BYPASS_USER_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID, DB_POOL_MIN, DB_POOL_MAX,
)
from views.help_views import HelpView


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", aliases=["h"], description="Browse every command by category, with search.")
    async def help_command(self, ctx: commands.Context):
        try:
            if ctx.interaction and not ctx.interaction.response.is_done():
                await ctx.defer()
            prefix = await database.async_get_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
            view = HelpView(self.bot, ctx.author, prefix)
            view.sent_message = await ctx.send(view=view)
        except Exception as e:
            print(f"[help] failed to open help menu: {e!r}")
            # Keep help usable even if a newly added command has malformed
            # metadata or Discord rejects one interactive component.
            try:
                commands_list = sorted({
                    command.name for command in self.bot.commands
                    if not command.hidden
                })
                fallback = (
                    "## Buii Help\n"
                    f"Use {prefix if 'prefix' in locals() else DEFAULT_PREFIX}<command> "
                    "or /command.\n\n"
                    + " · ".join(commands_list)
                )
                await ctx.send(view=SimpleLayout(fallback))
            except Exception:
                try:
                    await ctx.send(view=notice("❌ Couldn't open the help menu — try again in a moment."))
                except Exception:
                    pass

    @commands.hybrid_command(
        name="diagnostics",
        description="Show private bot health diagnostics (owner bypass only).",
    )
    async def diagnostics(self, ctx: commands.Context):
        """Private health panel for the configured BYPASS_USER_ID only."""
        if not BYPASS_USER_ID or ctx.author.id != BYPASS_USER_ID:
            await ctx.send(
                view=notice("❌ This diagnostic command is restricted."),
                ephemeral=bool(ctx.interaction),
            )
            return

        db_ready = database.db_pool is not None
        session = getattr(self.bot, "http_session", None)
        spotify_ready = all((
            SPOTIFY_CLIENT_ID,
            SPOTIFY_CLIENT_SECRET,
            SPOTIFY_REFRESH_TOKEN,
            SPOTIFY_PLAYLIST_ID,
        ))
        latency_ms = round(self.bot.latency * 1000) if self.bot.latency != float("inf") else "offline"

        text = (
            "## BOT DIAGNOSTICS\n"
            "-# Private owner health check\n\n"
            f"**Gateway**  ·  `{latency_ms} ms`\n"
            f"**Guilds**  ·  `{len(self.bot.guilds)}`\n"
            f"**Database pool**  ·  {'✅ ready' if db_ready else '❌ unavailable'}\n"
            f"**HTTP session**  ·  {'✅ ready' if session and not session.closed else '❌ unavailable'}\n"
            f"**Spotify configuration**  ·  {'✅ complete' if spotify_ready else '⚠️ incomplete'}\n\n"
            + "Use this panel to verify deployment health without exposing secrets."
        )
        await ctx.send(view=SimpleLayout(text), ephemeral=bool(ctx.interaction))


    @commands.hybrid_command(name="storage", aliases=["dbstatus"], description="Show private persistent-storage status (owner bypass only).")
    async def storage(self, ctx: commands.Context):
        if not BYPASS_USER_ID or ctx.author.id != BYPASS_USER_ID:
            await ctx.send(view=notice("❌ This storage command is restricted."), ephemeral=bool(ctx.interaction))
            return
        healthy = await database.async_check_db_health()
        pool_state = "ready" if database.db_pool is not None else "not initialized"
        text = (
            "## PERSISTENT STORAGE\n"
            f"**Database** · {'✅ healthy' if healthy else '❌ unavailable'}\n"
            f"**Connection pool** · {pool_state} · `{DB_POOL_MIN}–{DB_POOL_MAX}` connections\n"
            "**Records** · joins, leaves, member snapshots, ratings, and Spotify sync flags\n"
            "-# This panel is visible only to the configured bypass user."
        )
        await ctx.send(view=SimpleLayout(text), ephemeral=bool(ctx.interaction))


    @commands.hybrid_command(name="sync", description="Synchronize slash commands for this server.")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.guild)
    async def sync_commands(self, ctx: commands.Context, option: str = None):
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        try:
            if option == "clear":
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                message = "🧹 **Cleared all guild slash commands!**"
            else:
                synced = await self.bot.tree.sync()
                self.bot.tree.clear_commands(guild=ctx.guild)
                self.bot.tree.copy_global_to(guild=ctx.guild)
                guild_synced = await self.bot.tree.sync(guild=ctx.guild)
                message = (
                    f"⚡ **Synced {len(synced)} global commands and "
                    f"{len(guild_synced)} commands to this server.**"
                )
            if ctx.interaction and ctx.interaction.response.is_done():
                await ctx.followup.send(view=notice(message), ephemeral=True)
            else:
                await ctx.send(view=notice(message))
        except discord.HTTPException as e:
            print(f"[sync] failed: {e!r}")
            if ctx.interaction and ctx.interaction.response.is_done():
                await ctx.followup.send(view=notice("❌ Discord rejected the sync request — try again shortly."), ephemeral=True)
            else:
                await ctx.send(view=notice("❌ Discord rejected the sync request — try again shortly."))

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