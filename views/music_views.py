"""
Components V2 layouts for the music rating system: the 1-10 rating button
grid bundled with the track's title/links/card image, its disabled/closed
replica shown once voting ends, and the confirmation prompt for the
admin-only /renumbersongs command.

All three live in a Container (as does every response in the bot — see
core/components.py). The rating views are song content, so their accent
color is the track's score color (or the cover art's dominant color
pre-rating); the renumber prompt keeps its danger-red accent as a
destructive-action warning.
"""

import logging

logger = logging.getLogger(__name__)
from datetime import datetime, timezone, timedelta
import asyncio
import base64
import math
import re
import io
import subprocess

import discord
from discord import ui

from core import database
from core.config import RATING_WINDOW_HOURS, SPOTIFY_PLAYLIST_ID
from core.components import SimpleLayout, footer_line, notice
from core.helpers import create_music_card, format_elapsed
from core.music_utils import fetch_canonical_preview, fetch_deezer_audio_features, fetch_lyrics, remove_from_spotify


def _track_text(title: str, artist: str, preview_url: str = None, url: str = None,
                 requester_name: str = None, ping_text: str = None) -> str:
    """Build the main song heading; attribution is rendered near the footer."""
    title_line = f"## {title or 'Unknown Title'}"
    if artist:
        title_line += f"\n-# {artist}"
    return title_line


def _requester_text(requester_name: str = None, ping_text: str = None) -> str:
    """Keep the notification mention and attribution together at the bottom."""
    lines = []
    if ping_text:
        lines.append(ping_text)
    if requester_name:
        lines.append(f"Requested by {requester_name}")
    return "\n".join(lines)



class VoiceMessageUnavailable(RuntimeError):
    """Raised when the runtime cannot build or send a Discord voice message."""


def _voice_waveform(pcm: bytes, points: int = 256) -> str:
    """Create Discord's base64-encoded 1-byte-per-point waveform preview."""
    if not pcm:
        raise VoiceMessageUnavailable("voice conversion returned no PCM audio")

    sample_width = 2
    sample_count = len(pcm) // sample_width
    if sample_count == 0:
        raise VoiceMessageUnavailable("voice conversion returned no samples")

    values = []
    for offset in range(0, len(pcm) - 1, sample_width):
        sample = int.from_bytes(pcm[offset:offset + sample_width], "little", signed=True)
        values.append(sample)

    waveform = bytearray()
    for index in range(points):
        start = (index * sample_count) // points
        end = max(start + 1, ((index + 1) * sample_count) // points)
        bucket = values[start:end]
        rms = math.sqrt(sum(sample * sample for sample in bucket) / len(bucket))
        waveform.append(max(0, min(255, int((rms / 32768.0) * 255 * 1.8))))

    return base64.b64encode(bytes(waveform)).decode("ascii")


def _transcode_voice_preview(data: bytes) -> tuple[bytes, float, str]:
    """Convert arbitrary preview audio into Discord's OGG/Opus voice format."""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise VoiceMessageUnavailable("FFmpeg is not available") from exc

    def run_ffmpeg(output_args: list[str]) -> bytes:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                "pipe:0",
                "-vn",
                *output_args,
                "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=45,
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            error = result.stderr.decode("utf-8", errors="replace").strip()[:240]
            raise VoiceMessageUnavailable(error or "FFmpeg could not decode the preview")
        return result.stdout

    ogg = run_ffmpeg(["-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg"])
    pcm = run_ffmpeg(["-ac", "1", "-ar", "48000", "-f", "s16le"])
    duration = len(pcm) / (48000 * 2)
    return ogg, duration, _voice_waveform(pcm)


async def _send_voice_message(
    session,
    token: str,
    channel_id: int,
    filename: str,
    audio: bytes,
    duration: float,
    waveform: str,
) -> None:
    """Use Discord's attachment upload endpoint to create a voice-message bubble."""
    api = f"https://discord.com/api/v10/channels/{channel_id}"
    auth = {"Authorization": f"Bot {token}"}

    async with session.post(
        f"{api}/attachments",
        headers={**auth, "Content-Type": "application/json"},
        json={"files": [{"filename": filename, "file_size": len(audio), "id": "0"}]},
    ) as response:
        if response.status != 200:
            raise VoiceMessageUnavailable(f"attachment reservation failed ({response.status})")
        reservation = await response.json()

    try:
        upload = reservation["attachments"][0]
        async with session.put(
            upload["upload_url"],
            data=audio,
            headers={"Content-Type": "audio/ogg; codecs=opus"},
        ) as response:
            if response.status not in {200, 201, 204}:
                raise VoiceMessageUnavailable(f"audio upload failed ({response.status})")

        async with session.post(
            f"{api}/messages",
            headers={**auth, "Content-Type": "application/json"},
            json={
                "flags": 1 << 13,
                "attachments": [{
                    "id": "0",
                    "filename": filename,
                    "uploaded_filename": upload["upload_filename"],
                    "duration_secs": round(duration, 3),
                    "waveform": waveform,
                }],
            },
        ) as response:
            if response.status not in {200, 201}:
                raise VoiceMessageUnavailable(f"voice message creation failed ({response.status})")
    except KeyError as exc:
        raise VoiceMessageUnavailable("Discord returned an incomplete upload reservation") from exc


class PreviewButton(ui.Button):
    def __init__(self, guild_id: int, song_id: int, disabled: bool = False):
        super().__init__(
            label="▶ Preview",
            style=discord.ButtonStyle.primary,
            custom_id=f"preview|{song_id}",
            disabled=disabled,
        )
        self.guild_id = guild_id
        self.song_id = song_id

    async def callback(self, interaction: discord.Interaction):
        try:
            song = await database.get_song(self.guild_id, self.song_id)
            preview_url = song.get("preview_url") if song else None
            if not preview_url:
                await interaction.response.send_message(
                    view=notice("❌ No audio preview is available for this track."),
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                view=notice("⏳ Sending audio preview…"),
                ephemeral=True,
            )

            session = getattr(interaction.client, "http_session", None)
            if session is None:
                raise RuntimeError("HTTP session is unavailable")

            canonical_preview_url = await fetch_canonical_preview(
                session,
                song.get("url") if song else None,
                song.get("title") if song else None,
                song.get("artist") if song else None,
            )
            if canonical_preview_url:
                preview_url = canonical_preview_url

            async with session.get(preview_url) as response:
                if response.status != 200:
                    raise RuntimeError(f"preview download returned HTTP {response.status}")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                if content_type in {"text/html", "application/json", "text/plain"}:
                    raise RuntimeError(f"preview URL returned {content_type}, not audio")
                data = await response.read()

            extension = {
                "audio/mpeg": "mp3",
                "audio/mp3": "mp3",
                "audio/mp4": "m4a",
                "audio/x-m4a": "m4a",
                "audio/aac": "aac",
                "audio/ogg": "ogg",
                "audio/opus": "ogg",
                "audio/wav": "wav",
            }.get(content_type, "mp3")

            if len(data) > 10 * 1024 * 1024:
                await interaction.followup.send(
                    view=notice("❌ That preview is too large to send."),
                    ephemeral=True,
                )
                return
            if not data:
                raise RuntimeError("preview download was empty")

            if not await database.claim_preview(self.guild_id, self.song_id):
                self.disabled = True
                if interaction.message is not None and self.view is not None:
                    await interaction.message.edit(view=self.view)
                await interaction.followup.send(
                    view=notice("⚠️ This preview has already been used."),
                    ephemeral=True,
                )
                return

            self.disabled = True
            if interaction.message is not None and self.view is not None:
                await interaction.message.edit(view=self.view)

            safe_title = re.sub(r"[^A-Za-z0-9._-]+", "-", song.get("title") or "preview")
            safe_title = safe_title.strip("-._")[:60] or "preview"
            fallback_name = f"{safe_title}-{song['song_number']}.{extension}"

            # Discord's voice-message flag is not exposed by discord.py's
            # high-level send() helper, so use the REST upload flow when the
            # runtime has the optional FFmpeg dependency and bot token.
            token = getattr(getattr(interaction.client, "http", None), "token", None)
            channel_id = getattr(interaction, "channel_id", None)
            voice_error = None
            if token and channel_id:
                try:
                    voice_data, duration, waveform = await asyncio.to_thread(
                        _transcode_voice_preview, data
                    )
                    voice_name = f"{safe_title}-{song['song_number']}.ogg"
                    await _send_voice_message(
                        session, token, channel_id, voice_name,
                        voice_data, duration, waveform,
                    )
                    await interaction.edit_original_response(
                        view=notice("✅ Sent as a voice message."),
                    )
                    return
                except Exception as exc:
                    voice_error = exc
                    logger.warning("[music] voice-message send unavailable; using attachment fallback: %s", exc)

            await interaction.followup.send(
                file=discord.File(io.BytesIO(data), filename=fallback_name),
            )
            await interaction.edit_original_response(
                view=notice("✅ Preview sent."),
            )
        except Exception:
            logger.exception("[music] operation failed")
            try:
                if interaction.response.is_done():
                    await interaction.edit_original_response(
                        view=notice("❌ Couldn't send the audio preview — try again later."),
                    )
                else:
                    await interaction.response.send_message(
                        view=notice("❌ Couldn't send the audio preview — try again later."),
                        ephemeral=True,
                    )
            except Exception:
                logger.debug("Non-fatal exception suppressed", exc_info=True)





class TrackInfoButton(ui.Button):
    def __init__(self, guild_id: int, song_id: int):
        super().__init__(
            label="▣ Track Info",
            style=discord.ButtonStyle.secondary,
            custom_id=f"track-info|{song_id}",
        )
        self.guild_id = guild_id
        self.song_id = song_id

    async def callback(self, interaction: discord.Interaction):
        try:
            song = await database.get_song(self.guild_id, self.song_id)
            if not song:
                await interaction.response.send_message(
                    view=notice("❌ This song is no longer available."),
                    ephemeral=True,
                )
                return

            session = getattr(interaction.client, "http_session", None)
            match = await fetch_deezer_audio_features(
                session,
                song.get("url"),
                song.get("title"),
                song.get("artist"),
            )
            if not match:
                await interaction.response.send_message(
                    view=notice("ℹ️ No track information match was found."),
                    ephemeral=True,
                )
                return

            text = (
                "## ▣ TRACK INFO\n"
                f"**Artist**  ·  {match.get('artist') or song.get('artist') or 'Unknown'}\n"
                f"**Album**  ·  {match.get('album') or 'Unknown'}\n"
                f"**Release date**  ·  {match.get('release_date') or 'Unknown'}\n"
                f"**Genre**  ·  {match.get('genre') or 'Unknown'}\n"
                f"**Provider**  ·  {match.get('provider') or 'Deezer'}"
            )
            if match.get("track_url"):
                text += f"\n\n[Open matched track]({match['track_url']})"

            await interaction.response.send_message(
                view=SimpleLayout(text),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception("[music] track info lookup failed")
            if interaction.response.is_done():
                await interaction.followup.send(
                    view=notice("❌ Couldn't fetch track info right now."),
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    view=notice("❌ Couldn't fetch track info right now."),
                    ephemeral=True,
                )


class LyricsButton(ui.Button):
    def __init__(self, guild_id: int, song_id: int):
        super().__init__(
            label="♫ Lyrics",
            style=discord.ButtonStyle.secondary,
            custom_id=f"lyrics|{song_id}",
        )
        self.guild_id = guild_id
        self.song_id = song_id

    async def callback(self, interaction: discord.Interaction):
        try:
            song = await database.get_song(self.guild_id, self.song_id)
            if not song or not song.get("title") or not song.get("artist"):
                await interaction.response.send_message(
                    view=notice("❌ Lyrics are unavailable because this track has no artist metadata."),
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            session = getattr(interaction.client, "http_session", None)
            lyrics = await fetch_lyrics(session, song["title"], song["artist"]) if session else None
            if not lyrics:
                await interaction.followup.send(
                    view=notice("❌ No lyrics were found for this track."),
                    ephemeral=True,
                )
                return

            excerpt = lyrics[:3500]
            if len(lyrics) > len(excerpt):
                excerpt += "\n…"
            text = f"## {song['title']}\n-# {song['artist']}\n\n{excerpt}"
            await interaction.followup.send(view=SimpleLayout(text), ephemeral=True,
                                           allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("[music] lyrics lookup failed")
            if interaction.response.is_done():
                await interaction.followup.send(view=notice("❌ Couldn't fetch lyrics right now."), ephemeral=True)
            else:
                await interaction.response.send_message(view=notice("❌ Couldn't fetch lyrics right now."), ephemeral=True)


class RatingButton(ui.Button):
    def __init__(self, guild_id: int, score: int, song_id: int):
        # No `row=` here: RatingView groups these into two explicit
        # ActionRows (1-5, 6-10), which already decide the layout.
        super().__init__(label=str(score), style=discord.ButtonStyle.secondary,
                          custom_id=f"rate|{song_id}|{score}")
        self.guild_id = guild_id
        self.score = score
        self.song_id = song_id

    async def callback(self, interaction: discord.Interaction):
        try:
            await self._vote(interaction)
        except Exception as e:
            logger.exception("[music] operation failed")
            try:
                message = notice("❌ Couldn't register that vote — please try again.")
                if interaction.response.is_done():
                    await interaction.followup.send(view=message, ephemeral=True)
                else:
                    await interaction.response.send_message(view=message, ephemeral=True)
            except Exception:
                logger.debug("Non-fatal exception suppressed", exc_info=True)

    async def _vote(self, interaction: discord.Interaction):
        if await database.is_vote_blacklisted(self.guild_id, interaction.user.id):
            await interaction.response.send_message(
                view=notice("🚫 You are blocked from submitting music ratings in this server."),
                ephemeral=True,
            )
            return

        song = await database.get_song(self.guild_id, self.song_id)

        if song and song.get("closed"):
            await interaction.response.send_message(view=notice("🔒 Voting on this track is closed."), ephemeral=True)
            return

        if song and song.get("created_at") and \
                (datetime.now(timezone.utc) - song["created_at"]) >= timedelta(hours=RATING_WINDOW_HOURS):
            await interaction.response.send_message(
                view=notice(f"🔒 Voting closed — this track's {RATING_WINDOW_HOURS}-hour rating window has elapsed."),
                ephemeral=True,
            )
            return

        bot = interaction.client
        await database.set_rating(self.guild_id, self.song_id, interaction.user.id, self.score)
        avg, count = await database.get_song_stats(self.guild_id, self.song_id)
        song = await database.get_song(self.guild_id, self.song_id)  # re-fetch so synced/closed reflect the latest DB state

        sync_alert = ""

        card_bytes, dominant_rgb = await create_music_card(bot.http_session, song["title"], song["artist"], song["cover_url"],
                                                             avg, count, genre=song["genre"], song_number=song["song_number"],
                                                             status=("playlist" if song.get("synced") else "closed" if song.get("closed") else "open"))
        new_file = discord.File(fp=card_bytes, filename="rating_card.png")

        # ping_text is deliberately not carried over here — it's the initial
        # notification mention, meant to fire once at post_song time; a
        # revote doesn't need to keep showing raw "<@id> <@&role>" text.
        # requester_name (the "Requested by ..." line) IS carried over,
        # since that's a permanent attribution, not a one-time notification.
        new_view = RatingView(
            self.guild_id, song["id"], song["song_number"], title=song["title"], artist=song["artist"],
            requester_name=song["requested_by_name"], avg=avg, count=count,
            preview_url=song["preview_url"], url=song["url"], card_file=new_file,
            accent_rgb=dominant_rgb, vote_note=f"Latest vote: {self.score}/10 — use buttons to change",
            # Editing an existing rating card must not ping the requester again.
            ping_text=None,
            preview_used=bool(song.get("preview_used")),
            playlist_visible=bool(getattr(bot, "spotify_premium", False)),
        )

        await interaction.response.edit_message(view=new_view, attachments=[new_file])
        await interaction.followup.send(view=notice(f"You rated this **{self.score}/10** 🎵{sync_alert}"), ephemeral=True)


class RatingView(ui.LayoutView):
    def __init__(self, guild_id: int, song_id: int, song_number: int, *, title: str = None, artist: str = None,
                 requester_name: str = None, avg: float = 0.0, count: int = 0,
                 preview_url: str = None, url: str = None, card_file: "discord.File | str | None" = None,
                 accent_rgb: tuple = (88, 101, 242), ping_text: str = None, vote_note: str = None,
                 preview_used: bool = False, playlist_visible: bool = False):
        """`card_file` accepts either a fresh discord.File (uploaded with
        this message) or an "attachment://<filename>" string pointing at an
        attachment that already exists on the message being edited — the
        latter avoids re-rendering/re-uploading the card image when only
        the buttons or text need to change (see renumbersongs)."""
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.song_id = song_id
        self.song_number = song_number

        items = [ui.TextDisplay(_track_text(title, artist, preview_url, url, requester_name, ping_text))]
        if card_file is not None:
            items.append(ui.MediaGallery(discord.MediaGalleryItem(card_file)))

        link_buttons = [
            PreviewButton(guild_id, song_id, disabled=preview_used),
            LyricsButton(guild_id, song_id),
            TrackInfoButton(guild_id, song_id),
        ]
        if SPOTIFY_PLAYLIST_ID and playlist_visible:
            link_buttons.append(
                ui.Button(
                    label="↗ Playlist",
                    style=discord.ButtonStyle.link,
                    url=f"https://open.spotify.com/playlist/{SPOTIFY_PLAYLIST_ID}",
                )
            )
        if url:
            link_buttons.append(ui.Button(label="↗ Source", style=discord.ButtonStyle.link, url=url))
        items.append(ui.ActionRow(*link_buttons))
        items.append(ui.ActionRow(*(RatingButton(guild_id, i, song_id) for i in range(1, 6))))
        items.append(ui.ActionRow(*(RatingButton(guild_id, i, song_id) for i in range(6, 11))))

        requester_text = _requester_text(requester_name, ping_text)
        if requester_text:
            items.append(ui.TextDisplay(requester_text))

        footer_text = vote_note or "Rate it using the buttons below"
        items.append(ui.TextDisplay(footer_line(f"ID: {song_number} • {footer_text}")))

        # Status is shown on the Pillow card; keep the container border neutral.
        self.container = ui.Container(*items)
        self.add_item(self.container)


class MemberRatingsView(ui.LayoutView):
    PAGE_SIZE = 15

    def __init__(
        self,
        guild_id: int,
        member_id: int,
        member_name: str,
        owner_id: int,
        total: int,
        rows: list[dict],
        page: int = 1,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.member_id = member_id
        self.member_name = member_name
        self.owner_id = owner_id
        self.total = total
        self.total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(1, min(page, self.total_pages))
        self.rows = rows

        previous = ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 1,
        )
        previous.callback = self.on_previous_page

        page_label = ui.Button(
            label=f"Page {self.page}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )

        next_page = ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages,
        )
        next_page.callback = self.on_next_page

        self.container = ui.Container(
            ui.TextDisplay(self._render_text()),
            ui.ActionRow(previous, page_label, next_page),
        )
        self.add_item(self.container)

    def _render_text(self) -> str:
        lines = [
            f"## Ratings by {self.member_name}",
            f"**Total ratings:** {self.total} · **Page:** {self.page}/{self.total_pages}",
            "",
        ]
        for row in self.rows:
            artist = f" — {row['artist']}" if row["artist"] else ""
            lines.append(
                f"• **ID {row['song_number']} · {row['title']}**{artist} — **{row['score']}/10**"
            )
        lines.append(footer_line("Member Rating Inspector"))
        return "\n".join(lines)

    async def _show_page(self, interaction: discord.Interaction, page: int):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                view=notice("❌ These pagination controls belong to the moderator who ran the command."),
                ephemeral=True,
            )
            return
        rows = await database.get_member_ratings(
            self.guild_id,
            self.member_id,
            limit=self.PAGE_SIZE,
            offset=(page - 1) * self.PAGE_SIZE,
        )
        await interaction.response.edit_message(
            view=MemberRatingsView(
                self.guild_id,
                self.member_id,
                self.member_name,
                self.owner_id,
                self.total,
                rows,
                page=page,
            )
        )

    async def on_previous_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, self.page - 1)

    async def on_next_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, self.page + 1)


class MusicProfileView(ui.LayoutView):
    def __init__(
        self,
        guild_id: int,
        member_id: int,
        member_name: str,
        owner_id: int,
        profile_text: str,
        avatar_url: str,
        requested_total: int,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.member_id = member_id
        self.member_name = member_name
        self.owner_id = owner_id
        self.profile_text = profile_text
        self.avatar_url = avatar_url
        self.requested_total = requested_total

        requested = ui.Button(
            label=f"Requested songs ({requested_total})",
            style=discord.ButtonStyle.secondary,
            disabled=requested_total == 0,
        )
        requested.callback = self.on_requested_songs

        self.add_item(ui.Container(
            ui.Section(
                ui.TextDisplay(profile_text),
                accessory=ui.Thumbnail(media=avatar_url),
            ),
            ui.ActionRow(requested),
        ))

    async def on_requested_songs(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                view=notice("❌ These controls belong to the moderator who ran the command."),
                ephemeral=True,
            )
            return
        rows = await database.get_member_requested_songs(
            self.guild_id,
            self.member_id,
            limit=MemberRequestedSongsView.PAGE_SIZE,
            offset=0,
        )
        await interaction.response.edit_message(
            view=MemberRequestedSongsView(
                self.guild_id,
                self.member_id,
                self.member_name,
                self.owner_id,
                self.profile_text,
                self.avatar_url,
                self.requested_total,
                rows,
                page=1,
            )
        )


class MemberRequestedSongsView(ui.LayoutView):
    PAGE_SIZE = 10

    def __init__(
        self,
        guild_id: int,
        member_id: int,
        member_name: str,
        owner_id: int,
        profile_text: str,
        avatar_url: str,
        total: int,
        rows: list[dict],
        page: int = 1,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.member_id = member_id
        self.member_name = member_name
        self.owner_id = owner_id
        self.profile_text = profile_text
        self.avatar_url = avatar_url
        self.total = total
        self.total_pages = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(1, min(page, self.total_pages))
        self.rows = rows

        previous = ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page <= 1)
        previous.callback = self.on_previous_page
        page_label = ui.Button(label=f"Page {self.page}/{self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True)
        next_page = ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= self.total_pages)
        next_page.callback = self.on_next_page
        back = ui.Button(label="Back to profile", style=discord.ButtonStyle.secondary)
        back.callback = self.on_back

        self.add_item(ui.Container(
            ui.TextDisplay(self._render_text()),
            ui.ActionRow(previous, page_label, next_page, back),
        ))

    def _render_text(self) -> str:
        lines = [
            f"## Requested songs by {self.member_name}",
            f"**Total requested:** {self.total} · **Page:** {self.page}/{self.total_pages}",
            "",
        ]
        for row in self.rows:
            artist = f" — {row['artist']}" if row["artist"] else ""
            status = "playlist" if row.get("synced") else "closed" if row.get("closed") else "open"
            lines.append(
                f"• **ID {row['song_number']} · {row['title']}**{artist} · **{status}**"
            )
        lines.append(footer_line("Music Profile · Requested Songs"))
        return "\n".join(lines)

    async def _show_page(self, interaction: discord.Interaction, page: int):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                view=notice("❌ These controls belong to the moderator who ran the command."),
                ephemeral=True,
            )
            return
        rows = await database.get_member_requested_songs(
            self.guild_id,
            self.member_id,
            limit=self.PAGE_SIZE,
            offset=(page - 1) * self.PAGE_SIZE,
        )
        await interaction.response.edit_message(
            view=MemberRequestedSongsView(
                self.guild_id,
                self.member_id,
                self.member_name,
                self.owner_id,
                self.profile_text,
                self.avatar_url,
                self.total,
                rows,
                page=page,
            )
        )

    async def on_previous_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, self.page - 1)

    async def on_next_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, self.page + 1)

    async def on_back(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                view=notice("❌ These controls belong to the moderator who ran the command."),
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            view=MusicProfileView(
                self.guild_id,
                self.member_id,
                self.member_name,
                self.owner_id,
                self.profile_text,
                self.avatar_url,
                self.total,
            )
        )


class SongSearchModal(ui.Modal, title="Search songs"):
    query = ui.TextInput(
        label="Song title or artist",
        placeholder="Type part of a title or artist...",
        max_length=100,
        required=True,
    )

    def __init__(self, leaderboard):
        super().__init__()
        self.leaderboard = leaderboard

    async def on_submit(self, interaction: discord.Interaction):
        rows = await database.search_songs(
            self.leaderboard.guild_id,
            str(self.query),
            limit=25,
            min_votes=2,
            min_score=self.leaderboard.min_score,
        )
        if not rows:
            await interaction.response.send_message(
                view=notice("🔎 No songs matched that search. Results still require at least 2 votes."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=SongLeaderboardView(
                self.leaderboard.guild_id,
                rows,
                min_score=self.leaderboard.min_score,
                query=str(self.query),
            ),
            ephemeral=True,
        )


class SongLeaderboardView(ui.LayoutView):
    PAGE_SIZE = 10

    def __init__(
        self,
        guild_id: int,
        rows: list[dict],
        *,
        min_score: float = 0.0,
        query: str = None,
        page: int = 1,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.rows = rows
        self.min_score = min_score
        self.query = query
        self.total_pages = max(1, (len(rows) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(1, min(page, self.total_pages))

        self.select = ui.Select(
            placeholder="Select a track to view details",
            options=[
                discord.SelectOption(
                    label=(row["title"] or "Unknown Title")[:100],
                    value=str(row["song_number"]),
                    description=f"ID \x60{row['song_number']}\x60 · {float(row['avg_score']):.1f}/10 · {row['votes']} votes"[:100],
                )
                for row in self.page_rows
            ],
        )
        self.select.callback = self.on_song_selected

        previous = ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 1,
        )
        previous.callback = self.on_previous_page

        page_label = ui.Button(
            label=f"Page {self.page}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )

        next_page = ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= self.total_pages,
        )
        next_page.callback = self.on_next_page

        search_button = ui.Button(label="Search", style=discord.ButtonStyle.secondary)
        search_button.callback = self.on_search

        self.container = ui.Container(
            ui.TextDisplay(self._render_text()),
            ui.ActionRow(self.select),
            ui.ActionRow(previous, page_label, next_page, search_button),
        )
        self.add_item(self.container)

    @property
    def page_rows(self):
        start = (self.page - 1) * self.PAGE_SIZE
        return self.rows[start:start + self.PAGE_SIZE]

    def _render_text(self, selected: dict = None) -> str:
        title = "MUSIC LEADERBOARD" if not self.query else f"SEARCH RESULTS · {self.query}"
        lines = [
            f"## {title}",
            "-# Server rankings · minimum 2 votes per track",
            "",
        ]
        if self.min_score:
            lines[1] = f"-# Server rankings · minimum 2 votes · average ≥ {self.min_score:.1f}/10"

        if selected:
            artist = f" — {selected['artist']}" if selected.get("artist") else ""
            lines.extend([
                "### SELECTED TRACK",
                f"**{selected['title'] or 'Unknown Title'}**{artist}",
                f"ID \x60{selected['song_number']}\x60  ·  ⭐ **{float(selected['avg_score']):.1f}/10**  ·  **{selected['votes']} votes**",
                f"Posted {format_elapsed(selected['created_at'])}",
                "",
            ])

        lines.append("**RANKINGS**")
        first_rank = (self.page - 1) * self.PAGE_SIZE + 1
        for index, row in enumerate(self.page_rows, start=first_rank):
            artist = f" — {row['artist']}" if row.get("artist") else ""
            title = (row['title'] or 'Unknown Title')[:70]
            rank = f"#{index:02d}"
            lines.append(
                f"\x60{rank}\x60 **{title}**{artist}  ·  ID \x60{row['song_number']}\x60  ·  "
                f"⭐ **{float(row['avg_score']):.1f}**  ·  {row['votes']} votes"
            )
        lines.extend(["", footer_line(f"Server Music Leaderboard · Page {self.page}/{self.total_pages}")])
        return "\n".join(lines)

    async def _show_page(self, interaction: discord.Interaction, page: int):
        await interaction.response.edit_message(
            view=SongLeaderboardView(
                self.guild_id,
                self.rows,
                min_score=self.min_score,
                query=self.query,
                page=page,
            )
        )

    async def on_previous_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, self.page - 1)

    async def on_next_page(self, interaction: discord.Interaction):
        await self._show_page(interaction, self.page + 1)

    async def on_song_selected(self, interaction: discord.Interaction):
        number = int(self.select.values[0])
        selected = await database.get_song_by_number(self.guild_id, number)
        if not selected:
            await interaction.response.send_message(view=notice("❌ That song is no longer available."), ephemeral=True)
            return
        avg, count = await database.get_song_stats(self.guild_id, selected["id"])
        selected["avg_score"] = avg
        selected["votes"] = count
        self.container.children[0].content = self._render_text(selected)
        await interaction.response.edit_message(view=self)

    async def on_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SongSearchModal(self))


class ClosedRatingView(ui.LayoutView):
    """Locked songs keep only their Pillow card; all controls are removed."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ui.MediaGallery(
            discord.MediaGalleryItem("attachment://rating_card.png")
        ))


class RenumberConfirmView(ui.LayoutView):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.confirmed = None

        text = (
            "## ⚠️ Renumber Song IDs\n"
            "This will reassign every song's ID to a dense sequence and update all linked ratings.\n\n"
            "The bot will then try to **re-edit every live rating message** so its buttons still work. "
            "Messages the bot can't reach (deleted, channel removed, missing permissions) will keep stale "
            "buttons that fail silently until re-posted.\n\n"
            "**This cannot be undone. Continue?**"
        )

        confirm_btn = ui.Button(label="Confirm Renumber", style=discord.ButtonStyle.danger, emoji="⚠️")
        confirm_btn.callback = self.on_confirm
        cancel_btn = ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self.on_cancel

        self.container = ui.Container(
            ui.TextDisplay(text),
            ui.ActionRow(confirm_btn, cancel_btn),
        )
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(view=notice("❌ Only the command invoker can confirm this."), ephemeral=True)
            return False
        return True

    async def on_confirm(self, interaction: discord.Interaction):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    async def on_cancel(self, interaction: discord.Interaction):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()
