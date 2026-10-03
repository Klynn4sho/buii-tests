"""
Discord OAuth2 for the dashboard's login flow: build the authorize URL,
exchange an auth code for a token, and fetch the logged-in user's identity
and guild list. Plain synchronous requests calls — this talks to Discord's
REST API (discord.com/api), not the bot's gateway connection, so there's no
need to route it through the bot's asyncio loop the way web/bridge.py does
for live guild data.
"""

from urllib.parse import urlencode

import requests

from core.config import DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI

DISCORD_API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
SCOPES = "identify guilds"


class OAuthNotConfigured(Exception):
    """Raised when DISCORD_CLIENT_ID/SECRET/REDIRECT_URI aren't set —
    surfaced as a clear error rather than a confusing downstream failure."""


def _require_config():
    if not (DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI):
        raise OAuthNotConfigured(
            "DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, and DISCORD_REDIRECT_URI "
            "must all be set to enable dashboard login."
        )


def build_authorize_url(state: str) -> str:
    _require_config()
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Trades a one-time auth code for an access/refresh token pair.
    Raises requests.HTTPError on failure (e.g. an expired or reused code)."""
    _require_config()
    resp = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_token(refresh_token_value: str) -> dict:
    _require_config()
    resp = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_user(access_token: str) -> dict:
    resp = requests.get(f"{DISCORD_API}/users/@me",
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_user_guilds(access_token: str) -> list:
    """Every guild the user is in, each with a `permissions` bitfield string
    — the dashboard filters this down to guilds where the user has
    Manage Guild/Administrator AND the bot is also present."""
    resp = requests.get(f"{DISCORD_API}/users/@me/guilds",
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    resp.raise_for_status()
    return resp.json()
