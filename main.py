"""
Buii — Discord invite-tracking / analytics bot, merged with the Music
Rating Bot (Pillow dynamic cards, Spotify playlist sync). Cogs edition.

This file only handles process-level concerns: intents/bot construction,
extension loading, the keep-alive web server, and graceful shutdown. All
actual behavior lives in core/, cogs/, and views/.

Dependencies:
    pip install discord.py psycopg2-binary flask matplotlib aiohttp Pillow

Environment Variables:
    BOT_TOKEN=your-bot-token
    DB_URL=postgres://...
    SPOTIFY_CLIENT_ID=your-spotify-client-id
    SPOTIFY_CLIENT_SECRET=your-spotify-client-secret
    SPOTIFY_USER_TOKEN=your-user-oauth-token-with-playlist-scope
    SPOTIFY_PLAYLIST_ID=your-playlist-id
    BYPASS_USER_ID=optional-discord-user-id
    COMMAND_PREFIX=b,
"""

import asyncio
import os
from difflib import get_close_matches
import signal
from datetime import datetime, timezone
from threading import Thread

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands

from core import database
from core.components import notice
from core.config import BOT_TOKEN, DEFAULT_PREFIX

EXTENSIONS = ("cogs.growth", "cogs.music", "cogs.hierarchy", "cogs.serverinfo", "cogs.admin")


# --- Web dashboard (also doubles as the process's keep-alive server on
# free-tier hosts, the way the old placeholder Flask app did) ---
def run_web_server():
    # Imported lazily, inside the function: by the time this actually runs
    # (in its own thread, started from keep_alive() below), the module-level
    # `bot` further down this file already exists, so web.app.create_app(bot)
    # can bind routes to it. Importing web.app at module load time instead
    # would work too, but keeping it here makes the dependency — "the web
    # app needs a constructed bot" — visible at the point it matters.
    from web.app import create_app
    app = create_app(bot)
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


def keep_alive():
    Thread(target=run_web_server, daemon=True).start()


# --- Prefix resolution (delegates to the DB-backed cache in core.database) ---
async def get_prefix(bot_instance: "BuiiBot", message: discord.Message):
    if not message.guild:
        return DEFAULT_PREFIX
    configured = await database.async_get_prefix(message.guild.id)
    # Keep the default b, prefix usable even if an older database row stores
    # a custom prefix without the comma or an outdated value.
    if configured == DEFAULT_PREFIX:
        return configured
    return (configured, DEFAULT_PREFIX)


class BuiiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True
        super().__init__(command_prefix=get_prefix, intents=intents, help_command=None)
        self.http_session: aiohttp.ClientSession = None  # type: ignore
        self.web_loop: asyncio.AbstractEventLoop = None  # type: ignore  # set in setup_hook()

    async def setup_hook(self):
        # Captured first, before anything else in here: this is the exact
        # loop discord.py runs the gateway/cog code on, and it's what lets
        # web/bridge.py safely call into live guild data (bot.guilds,
        # guild.roles, etc.) from the Flask thread via
        # asyncio.run_coroutine_threadsafe(). Deliberately not relying on
        # discord.py's own `self.loop` attribute — this way the dashboard
        # doesn't depend on an internal implementation detail whose exact
        # availability timing could differ across discord.py versions.
        self.web_loop = asyncio.get_running_loop()

        # DB pool + tables + the shared HTTP session must exist before any
        # cog's cog_load() runs, since several of them query the DB or
        # register persistent views that reference it.
        database.init_db_pool()
        await database.init_db()
        await database.init_music_db()

        if self.http_session is None:
            self.http_session = aiohttp.ClientSession()

        for extension in EXTENSIONS:
            await self.load_extension(extension)

        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash command(s).")
        except Exception as e:
            print(f"Failed to sync slash commands: {e}")

    async def close(self):
        # async with bot (in run_bot) calls this on every exit path — clean
        # up the extra resource this subclass owns before discord.py closes
        # the gateway connection.
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()


bot = BuiiBot()


@bot.event
async def on_ready():
    print(f"Bot connected as: {bot.user}")


@bot.event
async def on_disconnect():
    # Fires on every gateway drop, including ones discord.py auto-recovers
    # from — this is a log line, not necessarily an outage.
    print(f"[{datetime.now(timezone.utc).isoformat()}] Gateway disconnected — attempting to reconnect...")


@bot.event
async def on_resumed():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Gateway session resumed.")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    # Music-link detection lives in MusicCog's own on_message listener;
    # this one only needs to dispatch prefix commands, and must be the
    # only place in the whole bot that calls process_commands (calling it
    # more than once would run each prefix command's body twice).
    await bot.process_commands(message)


async def _command_prefix_for(message: discord.Message) -> str:
    value = await bot.get_prefix(message)
    if isinstance(value, (tuple, list)):
        return value[0]
    return value


def _command_hint_view(title: str, body: str, run_label: str | None = None,
                       callback=None) -> ui.LayoutView:
    view = ui.LayoutView(timeout=90)
    items = [ui.TextDisplay(f"## {title}\\n{body}")]
    if run_label and callback:
        button = ui.Button(label=run_label, style=discord.ButtonStyle.secondary)
        button.callback = callback
        items.append(ui.ActionRow(button))
        view.run_button = button
    view.add_item(ui.Container(*items))
    return view


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        attempted = getattr(error, "command_name", None) or ctx.invoked_with or "that command"
        names = [command.name for command in bot.commands if not command.hidden]
        suggestion = get_close_matches(attempted, names, n=1, cutoff=0.45)
        if suggestion:
            target = bot.get_command(suggestion[0])
            prefix = await _command_prefix_for(ctx.message)
            label = f"Run {prefix}{suggestion[0]}"

            async def run_suggestion(interaction: discord.Interaction):
                if interaction.user.id != ctx.author.id:
                    await interaction.response.send_message(view=notice("❌ This suggestion belongs to the person who ran the command."), ephemeral=True)
                    return
                await interaction.response.defer()
                try:
                    await ctx.invoke(target)
                except Exception as invoke_error:
                    await on_command_error(ctx, invoke_error)
                view.run_button.disabled = True
                try:
                    await interaction.edit_original_response(view=view)
                except Exception:
                    pass

            view = _command_hint_view(
                f"Command `{attempted}` does not exist",
                f"Did you mean `{prefix}{suggestion[0]}`? · Try `{prefix}help` to see everything.",
                label,
                run_suggestion,
            )
        else:
            prefix = await _command_prefix_for(ctx.message)
            view = _command_hint_view(
                f"Command `{attempted}` does not exist",
                f"Try `{prefix}help` to see every available command.",
            )
        await ctx.send(view=view)
        return

    if isinstance(error, commands.MissingRequiredArgument):
        command = ctx.command
        prefix = await _command_prefix_for(ctx.message)
        signature = command.signature or f"<{error.param.name}>"
        body = (
            f"{command.description or command.short_doc or 'View command details.'}\\n"
            f"-# Syntax: `{prefix}{command.qualified_name} {signature}` | `/{command.qualified_name} {signature}`"
        )
        await ctx.send(view=_command_hint_view(command.qualified_name, body))
        return

    if isinstance(error, commands.MissingPermissions):
        text = "❌ You don't have permission to use that command."
    elif isinstance(error, commands.BadArgument):
        text = f"⚠️ Couldn't understand one of your arguments: {error}"
    elif isinstance(error, commands.CommandOnCooldown):
        text = f"⏱️ That command is on cooldown. Try again in {error.retry_after:.1f}s."
    else:
        print(f"Unhandled command error in '{ctx.command}': {error}")
        text = "❌ Something went wrong running that command."

    try:
        await ctx.send(view=notice(text))
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ You don't have permission to use that command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏱️ That command is on cooldown. Try again in {error.retry_after:.1f}s."
    else:
        print(f"Unhandled app command error: {error}")
        message = "❌ Something went wrong running that command."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(view=notice(message), ephemeral=True)
        else:
            await interaction.response.send_message(view=notice(message), ephemeral=True)
    except Exception:
        pass


async def shutdown_cleanup():
    """Closes the Postgres pool — bot.close() (overridden above) already
    handles the aiohttp session — so a SIGTERM-based redeploy (most
    container platforms) doesn't leak sockets/connections."""
    print(f"[{datetime.now(timezone.utc).isoformat()}] Shutting down — closing DB pool...")
    if database.db_pool is not None:
        database.db_pool.closeall()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Shutdown complete.")


async def run_bot():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal(sig_name):
        print(f"[{datetime.now(timezone.utc).isoformat()}] Received {sig_name} — starting graceful shutdown...")
        stop_event.set()

    # asyncio.run() only turns SIGINT into a KeyboardInterrupt automatically;
    # SIGTERM (what most container platforms send on redeploy/scale-down) is
    # otherwise ignored and the process gets hard-killed after the
    # platform's grace period with sockets/connections still open.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig.name)
        except (NotImplementedError, AttributeError):
            pass  # add_signal_handler isn't available on Windows

    async with bot:
        start_task = asyncio.create_task(bot.start(BOT_TOKEN))
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

        if stop_task in done:
            await bot.close()
        for task in pending:
            task.cancel()

        if start_task in done:
            start_task.result()  # re-raise if bot.start() itself failed


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    keep_alive()
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        asyncio.run(shutdown_cleanup())
