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
