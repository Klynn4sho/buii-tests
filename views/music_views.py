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
import re
import io

import discord
from discord import ui

from core import database
from core.config import RATING_WINDOW_HOURS, COLOR_DANGER
from core.components import footer_line, notice
from core.helpers import create_music_card, score_color, format_elapsed
from core.music_utils import remove_from_spotify


def _track_text(title: str, artist: str, preview_url: str = None, url: str = None,
                 requester_name: str = None, ping_text: str = None) -> str:
    """Builds the TextDisplay content shared by RatingView and
    ClosedRatingView: an optional ping line (mentions still notify from
    inside a TextDisplay, even though `content=` can't be combined with a
    Components V2 view), the title/artist, and preview/source links."""
    lines = []
    if ping_text:
        lines.append(ping_text)
    if requester_name:
        lines.append(footer_line(f"Requested by {requester_name}"))
    title_line = f"## {title or 'Unknown Title'}"
    if artist:
        title_line += f"\n-# {artist}"
    lines.append(title_line)
    return "\n".join(lines)


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

            await interaction.response.defer()

            session = getattr(interaction.client, "http_session", None)
            if session is None:
                raise RuntimeError("HTTP session is unavailable")

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
            await interaction.followup.send(
                file=discord.File(io.BytesIO(data), filename=f"{safe_title}-{song['song_number']}.{extension}"),
            )
        except Exception as e:
            logger.exception("[music] operation failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        view=notice("❌ Couldn't send the audio preview — try again later."),
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        view=notice("❌ Couldn't send the audio preview — try again later."),
                        ephemeral=True,
                    )
            except Exception:
                logger.debug("Non-fatal exception suppressed", exc_info=True)


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
                                                             avg, count, genre=song["genre"], song_number=song["song_number"])
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
            accent_rgb=dominant_rgb, vote_note=f"Your vote: {self.score}/10 — use buttons to change",
            ping_text=(f"<@{song['requested_by_id']}>" if song.get("requested_by_id") else None),
            preview_used=bool(song.get("preview_used")),
        )

        await interaction.response.edit_message(view=new_view, attachments=[new_file])
        await interaction.followup.send(view=notice(f"You rated this **{self.score}/10** 🎵{sync_alert}"), ephemeral=True)


class RatingView(ui.LayoutView):
    def __init__(self, guild_id: int, song_id: int, song_number: int, *, title: str = None, artist: str = None,
                 requester_name: str = None, avg: float = 0.0, count: int = 0,
                 preview_url: str = None, url: str = None, card_file: "discord.File | str | None" = None,
                 accent_rgb: tuple = (88, 101, 242), ping_text: str = None, vote_note: str = None,
                 preview_used: bool = False):
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

        link_buttons = [PreviewButton(guild_id, song_id, disabled=preview_used)]
        if url:
            link_buttons.append(ui.Button(label="↗ Source", style=discord.ButtonStyle.link, url=url))
        items.append(ui.ActionRow(*link_buttons))
        items.append(ui.ActionRow(*(RatingButton(guild_id, i, song_id) for i in range(1, 6))))
        items.append(ui.ActionRow(*(RatingButton(guild_id, i, song_id) for i in range(6, 11))))

        footer_text = vote_note or "Rate it using the buttons below"
        items.append(ui.TextDisplay(footer_line(f"ID: {song_number} • {footer_text}")))

        color = discord.Color.from_str(score_color(avg)) if count > 0 else discord.Color.from_rgb(*accent_rgb)
        self.container = ui.Container(*items, accent_color=color)
        self.add_item(self.container)


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
    def __init__(self, guild_id: int, rows: list[dict], *, min_score: float = 0.0, query: str = None):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.rows = rows[:25]
        self.min_score = min_score
        self.query = query
        self.select = ui.Select(
            placeholder="Select a track to view details",
            options=[
                discord.SelectOption(
                    label=(row["title"] or "Unknown Title")[:100],
                    value=str(row["song_number"]),
                    description=f"ID `{row['song_number']}` · {float(row['avg_score']):.1f}/10 · {row['votes']} votes"[:100],
                )
                for row in self.rows
            ],
        )
        self.select.callback = self.on_song_selected

        search_button = ui.Button(label="Search", style=discord.ButtonStyle.secondary)
        search_button.callback = self.on_search
        self.container = ui.Container(
            ui.TextDisplay(self._render_text()),
            ui.ActionRow(self.select),
            ui.ActionRow(search_button),
        )
        self.add_item(self.container)

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
                f"ID `{selected['song_number']}`  ·  ⭐ **{float(selected['avg_score']):.1f}/10**  ·  **{selected['votes']} votes**",
                f"Posted {format_elapsed(selected['created_at'])}",
                "",
            ])

        lines.append("**RANKINGS**")
        for index, row in enumerate(self.rows, start=1):
            artist = f" — {row['artist']}" if row.get("artist") else ""
            title = (row['title'] or 'Unknown Title')[:70]
            rank = f"#{index:02d}"
            lines.append(
                f"`{rank}` **{title}**{artist}  ·  ID `{row['song_number']}`  ·  "
                f"⭐ **{float(row['avg_score']):.1f}**  ·  {row['votes']} votes"
            )
        lines.extend(["", footer_line("Server Music Leaderboard")])
        return "\n".join(lines)

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
