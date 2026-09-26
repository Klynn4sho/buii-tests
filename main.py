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
import signal
from datetime import datetime, timezone
from threading import Thread

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

from core import database
from core.config import BOT_TOKEN, DEFAULT_PREFIX

EXTENSIONS = ("cogs.growth", "cogs.music", "cogs.hierarchy", "cogs.admin")

# --- Web server to keep the process alive on free-tier hosts ---
app = Flask('')


@app.route('/')
def home():
    return "Bot is alive, tracking, and rendering dynamic rating cards!"


def run_web_server():
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, use_reloader=False)


def keep_alive():
    Thread(target=run_web_server, daemon=True).start()


# --- Prefix resolution (delegates to the DB-backed cache in core.database) ---
async def get_prefix(bot_instance: "BuiiBot", message: discord.Message):
    if not message.guild:
        return DEFAULT_PREFIX
    return await database.async_get_prefix(message.guild.id)


class BuiiBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True
        super().__init__(command_prefix=get_prefix, intents=intents, help_command=None)
        self.http_session: aiohttp.ClientSession = None  # type: ignore

    async def setup_hook(self):
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


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use that command.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing required argument: `{error.param.name}`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"⚠️ Couldn't understand one of your arguments: {error}")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏱️ That command is on cooldown. Try again in {error.retry_after:.1f}s.")
        return

    print(f"Unhandled command error in '{ctx.command}': {error}")
    try:
        await ctx.send("❌ Something went wrong running that command.")
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
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
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
