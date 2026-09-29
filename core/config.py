"""
Environment configuration and shared visual constants for Buii.

Every other module (core/*, cogs/*, views/*) imports from here instead of
reading os.environ directly, so there's exactly one place that defines what
env vars the bot needs.
"""

import os
import discord

# --- Environment ---
DB_CONNECTION_STRING = os.environ.get("DB_URL")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

_bypass_raw = os.environ.get("BYPASS_USER_ID")
BYPASS_USER_ID = int(_bypass_raw) if _bypass_raw else None

DEFAULT_PREFIX = os.environ.get("COMMAND_PREFIX", "b,")

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_USER_TOKEN = os.environ.get("SPOTIFY_USER_TOKEN")
SPOTIFY_PLAYLIST_ID = os.environ.get("SPOTIFY_PLAYLIST_ID")

# --- Discord embed palette ---
COLOR_BRAND = discord.Color(0x5865F2)
COLOR_SUCCESS = discord.Color(0x57F287)
COLOR_WARNING = discord.Color(0xFEE75C)
COLOR_DANGER = discord.Color(0xED4245)
COLOR_ACCENT = discord.Color(0x2BC7C4)

# --- matplotlib graph palette ---
GRAPH_BG = "#2B2D31"
GRAPH_GRID = "#3B3D44"
GRAPH_TEXT = "#DBDEE1"
GRAPH_ACCENT = "#5865F2"
GRAPH_FILL = "#5865F2"

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
