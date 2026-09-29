"""
Thread-safe bridge from Flask's synchronous request-handling thread into
the bot's asyncio event loop.

Flask (via keep_alive() in main.py) runs its dev server in its own OS
thread, started before the bot has even logged in. discord.py's Client
does all of its work — reading bot.guilds, guild.roles, member caches,
anything on a Guild/Role/Member object — on one specific asyncio event
loop, and touching that state from a different thread without going
through the loop risks races with the gateway's own tasks. The standard,
supported way to call into a *running* loop from another thread is
asyncio.run_coroutine_threadsafe(), which is what run_on_bot() wraps.

Purely-synchronous data — the Postgres-backed stats every dashboard route
also needs — does NOT go through this bridge; core/database.py's `_raw_*`
functions are called directly from Flask routes instead, since psycopg2 is
already synchronous and has its own connection pool. This bridge exists
only for things that must be read from live discord.py objects.
"""

import asyncio
import concurrent.futures


class BotNotReady(Exception):
    """Raised when a dashboard request needs live bot data before the bot
    has finished logging in — e.g. a request that arrives in the few
    seconds between the web server starting and setup_hook() completing."""


def run_on_bot(bot, coro_func, *args, timeout: float = 8.0, **kwargs):
    """Runs `await coro_func(bot, *args, **kwargs)` on the bot's own event
    loop from any other thread, and blocks this thread until it finishes.

    `bot.web_loop` is captured once, in BuiiBot.setup_hook() (see
    main.py) — deliberately not discord.py's own `bot.loop` attribute,
    since relying on an internal attribute whose exact availability timing
    can differ across discord.py versions is more fragile than the bot
    setting its own explicit marker the moment its real loop exists.
    """
    loop = getattr(bot, "web_loop", None)
    if loop is None or not loop.is_running():
        raise BotNotReady("The bot hasn't finished starting up yet — try again in a few seconds.")

    future = asyncio.run_coroutine_threadsafe(coro_func(bot, *args, **kwargs), loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"Timed out waiting {timeout}s for the bot to respond.")


# ---------------------------------------------------------------------
# Coroutines meant to be passed to run_on_bot(). Each takes `bot` first so
# run_on_bot's `coro_func(bot, *args, **kwargs)` call shape works uniformly.
# ---------------------------------------------------------------------

async def get_manageable_guild_ids(bot, guild_ids: list) -> list:
    """Of the given Discord guild IDs (already filtered by the OAuth layer
    to ones the user can manage), returns the subset the bot is actually
    in — the dashboard only shows guilds where both are true."""
    bot_guild_ids = {g.id for g in bot.guilds}
    return [gid for gid in guild_ids if gid in bot_guild_ids]


async def get_guild_summary(bot, guild_id: int) -> dict | None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    return {
        "id": str(guild.id),
        "name": guild.name,
        "icon_url": guild.icon.url if guild.icon else None,
        "member_count": guild.member_count,
    }


async def get_guild_roles(bot, guild_id: int) -> list | None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    return [
        {"id": str(r.id), "name": r.name, "color": str(r.color), "position": r.position}
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        if not r.is_default()
    ]


async def get_guild_text_channels(bot, guild_id: int) -> list | None:
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    return [{"id": str(c.id), "name": c.name} for c in guild.text_channels]


async def get_bot_health(bot, _guild_id=None) -> dict:
    latency_ms = round(bot.latency * 1000) if bot.latency is not None else None
    return {
        "connected": bot.is_ready(),
        "latency_ms": latency_ms,
        "guild_count": len(bot.guilds),
    }
