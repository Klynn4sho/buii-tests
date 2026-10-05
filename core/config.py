"""
Environment configuration and shared visual constants for Buii.

Every other module (core/*, cogs/*, views/*) imports from here instead of
reading os.environ directly, so there's exactly one place that defines what
env vars the bot needs.
"""

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord

# --- Environment ---
def _build_db_connection_string(raw: str | None) -> str | None:
    if not raw:
        return None
    # Supabase requires TLS in production. Keep any user-supplied options,
    # while adding safe defaults for SSL and slow/dead connection attempts.
    if "://" not in raw:
        return raw
    parts = urlsplit(raw)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("sslmode", "require")
    query.setdefault("connect_timeout", "10")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DB_CONNECTION_STRING = _build_db_connection_string(os.environ.get("DB_URL"))
DB_POOL_MIN = max(1, int(os.environ.get("DB_POOL_MIN", "1")))
DB_POOL_MAX = max(DB_POOL_MIN, int(os.environ.get("DB_POOL_MAX", "8")))
BOT_TOKEN = os.environ.get("BOT_TOKEN")

APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
_bypass_raw = os.environ.get("BYPASS_USER_ID")
try:
    BYPASS_USER_ID = int(_bypass_raw) if _bypass_raw else None
except ValueError as exc:
    raise RuntimeError("BYPASS_USER_ID must be a numeric Discord user ID.") from exc

DEFAULT_PREFIX = os.environ.get("COMMAND_PREFIX", "b,")
MUSIC_GUILD_ID = 1508845079639625789

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_USER_TOKEN = os.environ.get("SPOTIFY_USER_TOKEN")
SPOTIFY_REFRESH_TOKEN = os.environ.get("SPOTIFY_REFRESH_TOKEN")
SPOTIFY_PLAYLIST_ID = "3sMQCk4G17NiUnVYj11Rbj"

# --- Discord embed palette ---
COLOR_BRAND = discord.Color(0x5865F2)
COLOR_SUCCESS = discord.Color(0x57F287)
COLOR_WARNING = discord.Color(0xFEE75C)
COLOR_DANGER = discord.Color(0xED4245)
COLOR_ACCENT = discord.Color(0x2BC7C4)

# --- matplotlib graph palette ---
GRAPH_BG = "#2B2D31"
GRAPH_GRID = "#24545A"
GRAPH_TEXT = "#DBDEE1"
GRAPH_ACCENT = "#2DD4BF"
GRAPH_FILL = "#2DD4BF"

# --- Behavior constants ---
RATING_WINDOW_HOURS = 12  # how long a song stays open for voting before auto-closing

# --- Staff hierarchy feature ---
# Permission names (must match attributes on discord.Permissions) that mark
# a role as "staff" for the /hierarchy directory image.
MODERATION_PERMISSIONS = [
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_messages",
]

# Role names or IDs (as strings) excluded from the hierarchy image even if
# they carry a moderation permission (e.g. a mute role that has
# manage_messages revoked per-channel, or an internal bot-handling role).
HIERARCHY_IGNORED_ROLE_NAMES = ["Muted", "Bot Handler"]

# --- Web dashboard (Discord OAuth2 + Flask session) ---
# A Discord application's Client ID/Secret (developer portal -> OAuth2),
# and the exact redirect URI registered there — must match byte-for-byte,
# scheme included (http vs https matters).
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI")

# Signs the Flask session cookie. MUST be set to a long random value in any
# real deployment — Flask falls back to an insecure dev default otherwise,
# which would let anyone forge a logged-in session. Generate one with:
#   python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY")

# Manage Guild (0x20) or Administrator (0x8) — the same bar /setlog,
# /setalertrole, etc. already require via has_mod_permission(). A user only
# sees a guild in the dashboard's server switcher if their permissions
# bitfield (from Discord's /users/@me/guilds) has one of these bits set.
DASHBOARD_REQUIRED_PERMS = 0x20 | 0x8
