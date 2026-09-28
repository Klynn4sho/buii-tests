"""
Admin utilities: the /help directory (auto-grouped by cog) and /sync for
pushing slash commands to a guild or globally.
"""

import discord
from discord import ui
from discord.ext import commands

from core.components import Layout, footer_line

# Emoji + display label for each cog, keyed by the cog's registered name.
# Anything not listed here (or not attached to a cog at all) falls into
# "📦 Other" automatically, so a newly added cog never silently vanishes
# from /help.
COG_DISPLAY = {
    "GrowthCog": "🔗 Invites & Growth",
    "MusicCog": "🎵 Music",
    "HierarchyCog": "🪪 Staff Directory",
    "AdminCog": "🛠️ Admin & Testing",
}


class AdminCog(commands.Cog, name="AdminCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _build_help_items(self, current_prefix: str) -> list:
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
        order = ["🎵 Music", "🏆 Leaderboards", "🔗 Invites & Growth", "🪪 Staff Directory", "⚙️ Server Configuration", "🛠️ Admin & Testing", "📦 Other"]
        ordered_labels = [label for label in order if label in by_category and by_category[label]]
        ordered_labels += sorted(by_category.keys() - set(order))

        header_text = (
            "# 📖 Help Directory\n"
            f"Server prefix: **`{current_prefix}`** — every command below also works as a `/slash` command."
        )
        items = [ui.Section(ui.TextDisplay(header_text), accessory=ui.Thumbnail(media=self.bot.user.display_avatar.url))
                 if self.bot.user else ui.TextDisplay(header_text)]
        items.append(ui.Separator())

        for i, label in enumerate(ordered_labels):
            body = "\n".join(sorted(set(by_category[label])))
            items.append(ui.TextDisplay(f"**{label}**\n{body}"))
            if i < len(ordered_labels) - 1:
                items.append(ui.Separator(spacing=discord.SeparatorSpacing.small))

        items.append(ui.Separator())
        items.append(ui.TextDisplay(footer_line("Help Directory")))
        return items

    @commands.command(name="h", aliases=["help"])
    async def help_prefix(self, ctx: commands.Context):
        from core import database
        current_prefix = await database.async_get_prefix(ctx.guild.id) if ctx.guild else "b,"
        await ctx.send(view=Layout(*self._build_help_items(current_prefix)))

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
