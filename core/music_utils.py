"""
Music-link detection, metadata fetching (oEmbed / OG tags), multi-provider
song search (Spotify -> Deezer -> iTunes), genre lookup, and Spotify
playlist sync/remove. Pure networking logic with no Discord or DB coupling,
so it's easy to test or reuse on its own.
"""

import asyncio
import re
from urllib.parse import quote

import aiohttp

from core.config import (
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_USER_TOKEN,
    SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID,
)

LINK_PATTERNS = {
    "Spotify": re.compile(r'https?://open\.spotify\.com/(?:intl-\w+/)?track/[a-zA-Z0-9]+(?:\?\S*)?'),
    "YouTube Music": re.compile(r'https?://music\.youtube\.com/watch\?v=[\w-]+\S*'),
    "YouTube": re.compile(r'https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w-]+\S*|youtu\.be/[\w-]+\S*)'),
    "SoundCloud": re.compile(r'https?://(?:www\.)?soundcloud\.com/\S+'),
    "Apple Music": re.compile(r'https?://music\.apple\.com/\S+'),
    "Deezer": re.compile(r'https?://(?:www\.)?deezer\.com/\S+'),
    "Tidal": re.compile(r'https?://(?:listen\.|www\.)?tidal\.com/\S+'),
    "Bandcamp": re.compile(r'https?://[\w-]+\.bandcamp\.com/track/\S+'),
}

OEMBED_ENDPOINTS = {
    "YouTube": "https://www.youtube.com/oembed?url={url}&format=json",
    "YouTube Music": "https://www.youtube.com/oembed?url={url}&format=json",
    "SoundCloud": "https://soundcloud.com/oembed?url={url}&format=json",
    "Spotify": "https://open.spotify.com/oembed?url={url}",
}

OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.IGNORECASE)
OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', re.IGNORECASE)
OG_DESCRIPTION_RE = re.compile(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', re.IGNORECASE)
SPOTIFY_TWITTER_ARTIST_RE = re.compile(r'<meta[^>]+name="twitter:audio:artist_name"[^>]+content="([^"]+)"', re.IGNORECASE)
SPOTIFY_JSONLD_ARTIST_RE = re.compile(r'"byArtist"\s*:\s*(?:\[\s*)?\{[^{}]*?"name"\s*:\s*"([^"]+)"', re.IGNORECASE)
YOUTUBE_CHANNEL_SUFFIX_RE = re.compile(r'\s*-\s*Topic$|\s*VEVO$', re.IGNORECASE)


def find_music_link(content: str):
    for source, pattern in LINK_PATTERNS.items():
        match = pattern.search(content)
        if match:
            return source, match.group(0).rstrip('>).,!?')
    return None, None


async def fetch_spotify_artist(session: aiohttp.ClientSession, url: str):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; MusicRatingBot/1.0)"}
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8), headers=headers) as resp:
            if resp.status != 200:
                return None
            html = await resp.text(errors="ignore")

            match = SPOTIFY_TWITTER_ARTIST_RE.search(html)
            if match:
                return match.group(1)

            match = SPOTIFY_JSONLD_ARTIST_RE.search(html)
            if match:
                return match.group(1)

            match = OG_DESCRIPTION_RE.search(html)
            if match:
                parts = [p.strip() for p in match.group(1).split("·")]
                parts = [p for p in parts if p and p.lower() not in ("song", "track")]
                if parts:
                    return parts[0]
    except Exception:
        pass
    return None


async def fetch_song_metadata(session: aiohttp.ClientSession, source: str, url: str):
    try:
        if source in OEMBED_ENDPOINTS:
            endpoint = OEMBED_ENDPOINTS[source].format(url=quote(url, safe=""))
            async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    title = data.get("title")
                    artist = data.get("author_name")
                    thumbnail = data.get("thumbnail_url")
                    if source == "Spotify" and not artist:
                        artist = await fetch_spotify_artist(session, url)
                    elif source in ("YouTube", "YouTube Music") and artist:
                        artist = YOUTUBE_CHANNEL_SUFFIX_RE.sub('', artist).strip() or artist
                    return title, artist, thumbnail, None
        elif source == "Deezer":
            match = re.search(r"/track/(\\d+)", url)
            if match:
                endpoint = f"https://api.deezer.com/track/{match.group(1)}"
                async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        artist_data = data.get("artist") or {}
                        album_data = data.get("album") or {}
                        return (
                            data.get("title"),
                            artist_data.get("name"),
                            album_data.get("cover_big") or album_data.get("cover"),
                            data.get("preview"),
                        )

            headers = {"User-Agent": "Mozilla/5.0 (compatible; MusicRatingBot/1.0)"}
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8), headers=headers) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="ignore")
                    title_match = OG_TITLE_RE.search(html)
                    image_match = OG_IMAGE_RE.search(html)
                    return (
                        title_match.group(1) if title_match else None,
                        None,
                        image_match.group(1) if image_match else None,
                        None
                    )
    except Exception:
        pass
    return None, None, None, None


async def search_itunes(session: aiohttp.ClientSession, query: str):
    try:
        endpoint = f"https://itunes.apple.com/search?term={quote(query, safe='')}&media=music&entity=song&limit=1"
        async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("results") or []
                if results:
                    top = results[0]
                    title = top.get("trackName")
                    artist = top.get("artistName")
                    artwork = top.get("artworkUrl100")
                    cover_url = artwork.replace("100x100", "600x600") if artwork else None
                    track_url = top.get("trackViewUrl")
                    preview_url = top.get("previewUrl")
                    if title:
                        return title, artist, cover_url, track_url, preview_url
    except Exception:
        pass
    return None


async def lookup_song_genre(session: aiohttp.ClientSession, title: str, artist: str) -> str:
    """One cheap iTunes lookup for a genre label — iTunes is the only one of
    the three search providers that returns `primaryGenreName` directly."""
    if not title:
        return None
    query = f"{artist} {title}" if artist else title
    try:
        endpoint = f"https://itunes.apple.com/search?term={quote(query, safe='')}&media=music&entity=song&limit=1"
        async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("results") or []
                if results:
                    return results[0].get("primaryGenreName")
    except Exception:
        pass
    return None


async def search_deezer(session: aiohttp.ClientSession, query: str):
    try:
        endpoint = f"https://api.deezer.com/search?q={quote(query, safe='')}&limit=1"
        async with session.get(endpoint, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                results = data.get("data") or []
                if results:
                    top = results[0]
                    title = top.get("title")
                    artist = (top.get("artist") or {}).get("name")
                    album = top.get("album") or {}
                    cover_url = album.get("cover_big") or album.get("cover")
                    track_url = top.get("link")
                    preview_url = top.get("preview")
                    if title:
                        return title, artist, cover_url, track_url, preview_url
    except Exception:
        pass
    return None


_spotify_token_cache = {"token": None, "expires_at": 0.0}

_spotify_user_token_cache = {"token": SPOTIFY_USER_TOKEN, "expires_at": 0.0}


async def _refresh_spotify_user_token(session: aiohttp.ClientSession):
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET and SPOTIFY_REFRESH_TOKEN):
        return None
    try:
        auth_header = aiohttp.helpers.encode_basic_auth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": SPOTIFY_REFRESH_TOKEN,
            },
            headers={"Authorization": auth_header},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            token = data.get("access_token")
            if not token:
                return None
            now = asyncio.get_event_loop().time()
            _spotify_user_token_cache["token"] = token
            _spotify_user_token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
            return token
    except Exception:
        return None


async def _get_spotify_user_token(session: aiohttp.ClientSession, force_refresh: bool = False):
    now = asyncio.get_event_loop().time()
    cached = _spotify_user_token_cache["token"]
    expires_at = _spotify_user_token_cache["expires_at"]

    if not force_refresh and cached and (not expires_at or now < expires_at):
        return cached

    refreshed = await _refresh_spotify_user_token(session)
    if refreshed:
        return refreshed

    if not force_refresh and SPOTIFY_USER_TOKEN:
        _spotify_user_token_cache["token"] = SPOTIFY_USER_TOKEN
        _spotify_user_token_cache["expires_at"] = 0.0
        return SPOTIFY_USER_TOKEN
    return None




async def _get_spotify_token(session: aiohttp.ClientSession):
    if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    now = asyncio.get_event_loop().time()
    if _spotify_token_cache["token"] and now < _spotify_token_cache["expires_at"]:
        return _spotify_token_cache["token"]
    try:
        auth_header = aiohttp.helpers.encode_basic_auth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": auth_header},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                token = data.get("access_token")
                _spotify_token_cache["token"] = token
                _spotify_token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
                return token
    except Exception:
        pass
    return None


async def search_spotify(session: aiohttp.ClientSession, query: str):
    token = await _get_spotify_token(session)
    if not token:
        return None
    try:
        endpoint = f"https://api.spotify.com/v1/search?q={quote(query, safe='')}&type=track&limit=1"
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(endpoint, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                items = (data.get("tracks") or {}).get("items") or []
                if items:
                    top = items[0]
                    title = top.get("name")
                    artist = ", ".join(a["name"] for a in top.get("artists", []) if a.get("name")) or None
                    images = (top.get("album") or {}).get("images") or []
                    cover_url = images[0]["url"] if images else None
                    track_url = (top.get("external_urls") or {}).get("spotify")
                    preview_url = top.get("preview_url")
                    if title:
                        return title, artist, cover_url, track_url, preview_url
    except Exception:
        pass
    return None


async def search_song_metadata(session: aiohttp.ClientSession, query: str):
    first_result = None
    for searcher in (search_spotify, search_deezer, search_itunes):
        result = await searcher(session, query)
        if not result:
            continue
        if first_result is None:
            first_result = result
        if result[4]:
            return result
    return first_result or (None, None, None, None, None)


async def sync_to_spotify(session: aiohttp.ClientSession, track_url: str) -> bool:
    """Add a Spotify track using the user's playlist-scoped token.

    The access token is refreshed lazily when it is missing/expired, and once
    more after a 401 so an expired token never prevents a qualifying song
    from being added.
    """
    if not SPOTIFY_PLAYLIST_ID or not track_url or "spotify.com/track/" not in track_url:
        return False

    try:
        track_id = track_url.split("track/")[1].split("?")[0]
        uri = f"spotify:track:{track_id}"
        token = await _get_spotify_user_token(session)
        for attempt in range(2):
            if not token:
                return False
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            async with session.post(
                f"https://api.spotify.com/v1/playlists/{SPOTIFY_PLAYLIST_ID}/tracks",
                json={"uris": [uri]},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 401 and attempt == 0 and SPOTIFY_REFRESH_TOKEN:
                    token = await _get_spotify_user_token(session, force_refresh=True)
                    continue
                return resp.status in (200, 201)
    except Exception:
        return False
    return False


async def remove_from_spotify(session: aiohttp.ClientSession, track_url: str) -> bool:
    """Remove a Spotify track using the playlist-scoped user token."""
    if not SPOTIFY_PLAYLIST_ID or not track_url or "spotify.com/track/" not in track_url:
        return False
    try:
        track_id = track_url.split("track/")[1].split("?")[0]
        uri = f"spotify:track:{track_id}"
        token = await _get_spotify_user_token(session)
        for attempt in range(2):
            if not token:
                return False
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            async with session.delete(
                f"https://api.spotify.com/v1/playlists/{SPOTIFY_PLAYLIST_ID}/tracks",
                json={"tracks": [{"uri": uri}]},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 401 and attempt == 0 and SPOTIFY_REFRESH_TOKEN:
                    token = await _get_spotify_user_token(session, force_refresh=True)
                    continue
                return resp.status == 200
    except Exception:
        return False
    return False
