"""
Admin utilities: the /help directory (a section-by-section Components V2
menu, see views/help_views.py) and /sync for pushing slash commands to a
guild or globally.

Help's category grouping lives in views/help_views.py (CATEGORIES), keyed
by cog name. A cog not listed there falls into "📦 Other" automatically.
"""

import logging

logger = logging.getLogger(__name__)
import discord
from discord.ext import commands

from core import database
from core.components import notice, SimpleLayout
from core.config import (
    DEFAULT_PREFIX, BYPASS_USER_ID, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID, DB_POOL_MIN, DB_POOL_MAX,
)
from views.help_views import HelpView
from core.music_utils import get_music_cache_stats


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", aliases=["commands", "h"], description="Browse every command by category, with search.")
    async def help_command(self, ctx: commands.Context):
        try:
            if ctx.interaction and not ctx.interaction.response.is_done():
                await ctx.defer()
            prefix = await database.async_get_prefix(ctx.guild.id) if ctx.guild else DEFAULT_PREFIX
            view = HelpView(self.bot, ctx.author, prefix)
            view.sent_message = await ctx.send(view=view)
        except Exception as e:
            logger.exception("[help] operation failed")
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
                    logger.debug("Non-fatal exception suppressed", exc_info=True)

    @commands.hybrid_command(
        name="diagnostics", aliases=["diag"],
        description="Show private bot health diagnostics (owner bypass only).",
    )
    async def diagnostics(self, ctx: commands.Context):
        """Private health panel for the configured BYPASS_USER_ID only."""
        is_server_admin = bool(ctx.guild and ctx.author.guild_permissions.administrator)
        if (not BYPASS_USER_ID or ctx.author.id != BYPASS_USER_ID) and not is_server_admin:
            await ctx.send(
                view=notice("❌ This diagnostic command is restricted to the bot owner or server administrators."),
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
        cache = get_music_cache_stats()
        providers = "Deezer · iTunes · Spotify"

        text = (
            "## BOT DIAGNOSTICS\n"
            "-# Private owner health check\n\n"
            f"**Gateway**  ·  `{latency_ms} ms`\n"
            f"**Guilds**  ·  `{len(self.bot.guilds)}`\n"
            f"**Database pool**  ·  {'✅ ready' if db_ready else '❌ unavailable'}\n"
            f"**HTTP session**  ·  {'✅ ready' if session and not session.closed else '❌ unavailable'}\n"
            f"**Spotify configuration**  ·  {'✅ complete' if spotify_ready else '⚠️ incomplete'}\n"
            f"**Music providers**  ·  `{providers}`\n"
            f"**Music cache**  ·  `{cache['entries']}` entries · `{cache['hits']}` hits · `{cache['misses']}` misses · `{cache['ttl_seconds']}s TTL`\n\n"
            + "Use this panel to verify deployment health without exposing secrets."
        )
        await ctx.send(view=SimpleLayout(text), ephemeral=bool(ctx.interaction))


    @commands.hybrid_command(name="storage", aliases=["db", "dbstatus"], description="Show private persistent-storage status (owner bypass only).")
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


    @commands.hybrid_command(name="sync", aliases=["sy"], description="Synchronize slash commands for this server.")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.guild)
    async def sync_commands(self, ctx: commands.Context, option: str = None):
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)
        try:
            normalized = (option or "").strip().lower()
            if normalized not in {"", "global", "clear"}:
                message = "❌ Use \`/sync\`, \`/sync global\`, or \`/sync clear\`."
            elif normalized == "clear":
                if not ctx.guild:
                    message = "❌ Guild command cleanup can only run inside a server."
                else:
                    guild = discord.Object(id=ctx.guild.id)
                    self.bot.tree.clear_commands(guild=guild)
                    await self.bot.tree.sync(guild=guild)
                    message = "🧹 **Cleared this server's slash commands.**"
            elif normalized == "global" or not ctx.guild:
                synced = await self.bot.tree.sync()
                message = f"🌐 **Synced {len(synced)} global slash commands.**"
            else:
                # Guild sync replaces stale command IDs immediately, which is
                # important while Discord's global command cache propagates.
                guild = discord.Object(id=ctx.guild.id)
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                message = f"⚡ **Synced {len(synced)} slash commands for this server.**"

            if ctx.interaction and ctx.interaction.response.is_done():
                await ctx.followup.send(view=notice(message), ephemeral=True)
            else:
                await ctx.send(view=notice(message))
        except discord.HTTPException:
            logger.exception("[sync] operation failed")
            message = "❌ Discord rejected the sync request — check the bot's command permissions and try again."
            if ctx.interaction and ctx.interaction.response.is_done():
                await ctx.followup.send(view=notice(message), ephemeral=True)
            else:
                await ctx.send(view=notice(message))


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
