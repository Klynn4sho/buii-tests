"""
Admin utilities: the /help directory (auto-grouped by cog) and /sync for
pushing slash commands to a guild or globally.
"""

import discord
from discord.ext import commands

from core.config import COLOR_BRAND
from core.helpers import themed_footer

# Emoji + display label for each cog, keyed by the cog's registered name.
# Anything not listed here (or not attached to a cog at all) falls into
# "📦 Other" automatically, so a newly added cog never silently vanishes
# from /help.
COG_DISPLAY = {
    "GrowthCog": "🔗 Invites & Growth",
    "MusicCog": "🎵 Music",
    "AdminCog": "🛠️ Admin & Testing",
}


def _add_field_chunked(embed: discord.Embed, name: str, lines: list[str]) -> None:
    """Splits a line list across multiple embed fields to stay under Discord's
    1024-character field value limit.  The Music category easily hits this
    with 9 hybrid commands + 3 manual slash entries.  Continuation fields use
    a zero-width space as the name so the category label only renders once."""
    LIMIT = 1024
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        needed = len(line) + (1 if current else 0)  # +1 for the joining newline
        if current_len + needed > LIMIT:
            chunks.append(current)
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += needed
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        embed.add_field(name=name if i == 0 else "\u200b", value="\n".join(chunk), inline=False)


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _build_help_embed(self, current_prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title="📖 Help Directory",
            description=f"Server prefix: **`{current_prefix}`** — every command below also works as a `/slash` command.",
            color=COLOR_BRAND
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        by_category: dict[str, list[str]] = {}
        for cmd in sorted(self.bot.commands, key=lambda c: c.name):
            if cmd.hidden or cmd.name in ("help", "h"):
                continue
            label = COG_DISPLAY.get(cmd.cog_name, "📦 Other")
            desc = cmd.description or cmd.short_doc or "No description."
            by_category.setdefault(label, []).append(f"`{current_prefix}{cmd.name}` / `/{cmd.name}` — {desc}")

        # song / musicleaderboard / removesong are slash-only app_commands
        # (not commands.Command), so they don't show up via bot.commands —
        # list them by hand under the music category.
        by_category.setdefault("🎵 Music", [])
        by_category["🎵 Music"].append("`/song <query>` — Nominate a song by name for rating.")
        by_category["🎵 Music"].append("`/musicleaderboard [min_score]` — Show the top rated songs, optionally filtered by minimum score.")
        by_category["🎵 Music"].append("`/removesong <song_id>` — Remove a song and its votes from the database and playlist.")

        # Preferred display order; anything else appears after, alphabetically.
        order = ["🎵 Music", "🏆 Leaderboards", "🔗 Invites & Growth", "⚙️ Server Configuration", "🛠️ Admin & Testing", "📦 Other"]
        for label in order:
            if label in by_category and by_category[label]:
                _add_field_chunked(embed, label, sorted(set(by_category[label])))
        for label in sorted(by_category.keys() - set(order)):
            _add_field_chunked(embed, label, sorted(set(by_category[label])))

        themed_footer(embed, self.bot, "Help Directory")
        return embed

    @commands.command(name="h", aliases=["help"])
    async def help_prefix(self, ctx: commands.Context):
        from core import database
        current_prefix = await database.async_get_prefix(ctx.guild.id) if ctx.guild else "b,"
        await ctx.send(embed=self._build_help_embed(current_prefix))

    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    @commands.cooldown(rate=1, per=60.0, type=commands.BucketType.guild)
    async def sync_commands(self, ctx: commands.Context, option: str = None):
        if option == "clear":
            self.bot.tree.clear_commands(guild=ctx.guild)
            await self.bot.tree.sync(guild=ctx.guild)
            await ctx.send("🧹 **Cleared all guild slash commands!**")
        elif option == "global":
            synced = await self.bot.tree.sync()
            await ctx.send(f"🌐 **Synced {len(synced)} commands globally!**")
        else:
            self.bot.tree.clear_commands(guild=ctx.guild)
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"⚡ **Clean-synced {len(synced)} Slash Commands to this server without duplication!**")

    @sync_commands.error
    async def sync_commands_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏱️ `sync` was just run — try again in **{error.retry_after:.0f}s**. "
                            f"(This guards against tripping Discord's own command-sync rate limit.)")
            return
        raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
