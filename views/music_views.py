"""
Components V2 layouts for the music rating system: the 1-10 rating button
grid bundled with the track's title/links/card image, its disabled/closed
replica shown once voting ends, and the confirmation prompt for the
admin-only /renumbersongs command.

All three bundle interactive buttons with their explanatory content, so
all three use a bordered Container — per the project's rule (see
core/components.py), that's exactly the case a border earns its keep.
Rating buttons' accent color also does real work here: it's the track's
score color (or the cover art's dominant color pre-rating), not decoration.
"""

from datetime import datetime, timezone, timedelta

import discord
from discord import ui

from core import database
from core.config import RATING_WINDOW_HOURS, COLOR_DANGER
from core.components import footer_line
from core.helpers import create_music_card, score_color
from core.music_utils import sync_to_spotify, remove_from_spotify


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
    title_line = f"**{title or 'Unknown Title'}**"
    if artist:
        title_line += f" — {artist}"
    lines.append(title_line)
    link_parts = []
    if preview_url:
        link_parts.append(f"[▶️ Preview]({preview_url})")
    if url:
        link_parts.append(f"[🔗 Source]({url})")
    if link_parts:
        lines.append(" • ".join(link_parts))
    return "\n".join(lines)


class RatingButton(ui.Button):
    def __init__(self, score: int, song_id: int):
        row = 0 if score <= 5 else 1
        super().__init__(label=str(score), style=discord.ButtonStyle.secondary,
                          custom_id=f"rate|{song_id}|{score}", row=row)
        self.score = score
        self.song_id = song_id

    async def callback(self, interaction: discord.Interaction):
        song = await database.get_song(self.song_id)

        if song and song.get("closed"):
            await interaction.response.send_message("🔒 Voting on this track is closed.", ephemeral=True)
            return

        if song and song.get("created_at") and \
                (datetime.now(timezone.utc) - song["created_at"]) >= timedelta(hours=RATING_WINDOW_HOURS):
            await interaction.response.send_message(
                f"🔒 Voting closed — this track's {RATING_WINDOW_HOURS}-hour rating window has elapsed.", ephemeral=True
            )
            return

        bot = interaction.client
        await database.set_rating(self.song_id, interaction.user.id, self.score)
        avg, count = await database.get_song_stats(self.song_id)
        song = await database.get_song(self.song_id)  # re-fetch so synced/closed reflect the latest DB state

        sync_alert = ""
        if song["url"]:
            if avg >= 6.0 and not song["synced"]:
                if await sync_to_spotify(bot.http_session, song["url"]):
                    await database.mark_song_synced(self.song_id)
                    sync_alert = "\n✅ *Track crossed 6.0 average and was added to the server Spotify playlist!*"
            elif avg < 6.0 and song["synced"]:
                if await remove_from_spotify(bot.http_session, song["url"]):
                    await database.unmark_song_synced(self.song_id)
                    sync_alert = "\n⚠️ *Track dropped below 6.0 average and was removed from the server Spotify playlist.*"

        card_bytes, dominant_rgb = await create_music_card(bot.http_session, song["title"], song["artist"], song["cover_url"],
                                                             avg, count, genre=song["genre"])
        new_file = discord.File(fp=card_bytes, filename="rating_card.png")

        # ping_text is deliberately not carried over here — it's the initial
        # notification mention, meant to fire once at post_song time; a
        # revote doesn't need to keep showing raw "<@id> <@&role>" text.
        # requester_name (the "Requested by ..." line) IS carried over,
        # since that's a permanent attribution, not a one-time notification.
        new_view = RatingView(
            song["id"], title=song["title"], artist=song["artist"],
            requester_name=song["requested_by_name"], avg=avg, count=count,
            preview_url=song["preview_url"], url=song["url"], card_file=new_file,
            accent_rgb=dominant_rgb, vote_note=f"Your vote: {self.score}/10 — use buttons to change",
        )

        await interaction.response.edit_message(view=new_view, attachments=[new_file])
        await interaction.followup.send(f"You rated this **{self.score}/10** 🎵{sync_alert}", ephemeral=True)


class RatingView(ui.LayoutView):
    def __init__(self, song_id: int, *, title: str = None, artist: str = None,
                 requester_name: str = None, avg: float = 0.0, count: int = 0,
                 preview_url: str = None, url: str = None, card_file: "discord.File | str | None" = None,
                 accent_rgb: tuple = (88, 101, 242), ping_text: str = None, vote_note: str = None):
        """`card_file` accepts either a fresh discord.File (uploaded with
        this message) or an "attachment://<filename>" string pointing at an
        attachment that already exists on the message being edited — the
        latter avoids re-rendering/re-uploading the card image when only
        the buttons or text need to change (see renumbersongs)."""
        super().__init__(timeout=None)
        self.song_id = song_id

        items = [ui.TextDisplay(_track_text(title, artist, preview_url, url, requester_name, ping_text))]
        if card_file is not None:
            items.append(ui.MediaGallery(ui.MediaGalleryItem(card_file)))

        items.append(ui.ActionRow(*(RatingButton(i, song_id) for i in range(1, 6))))
        items.append(ui.ActionRow(*(RatingButton(i, song_id) for i in range(6, 11))))

        footer_text = vote_note or "Rate it using the buttons below"
        items.append(ui.TextDisplay(footer_line(f"ID: {song_id} • {footer_text}")))

        color = discord.Color.from_str(score_color(avg)) if count > 0 else discord.Color.from_rgb(*accent_rgb)
        self.container = ui.Container(*items, accent_color=color)
        self.add_item(self.container)


class ClosedRatingView(ui.LayoutView):
    """Read-only replica of RatingView with every button disabled — swapped
    in once a song's rating window elapses (or an admin manually closes it
    via /closevoting) so the buttons visibly grey out instead of silently
    rejecting clicks. Reuses the message's *existing* attachment via the
    `attachment://<filename>` reference instead of re-uploading the card
    image — the file is already on the message, so no new upload or
    re-render is needed just to disable the buttons."""
    def __init__(self, song: dict, avg: float, count: int):
        super().__init__(timeout=None)

        items = [ui.TextDisplay(_track_text(song.get("title"), song.get("artist"),
                                             song.get("preview_url"), song.get("url")))]
        items.append(ui.MediaGallery(ui.MediaGalleryItem("attachment://rating_card.png")))

        for row_range in (range(1, 6), range(6, 11)):
            items.append(ui.ActionRow(*(
                ui.Button(label=str(i), style=discord.ButtonStyle.secondary, disabled=True,
                          custom_id=f"closed_rating|{song['id']}|{i}")
                for i in row_range
            )))

        items.append(ui.TextDisplay(footer_line(
            f"ID: {song['id']} • 🔒 Voting closed after {RATING_WINDOW_HOURS} hours • Final: {avg:.1f}/10 ({count} votes)"
        )))

        color = discord.Color.from_str(score_color(avg)) if count > 0 else discord.Color.dark_grey()
        self.container = ui.Container(*items, accent_color=color)
        self.add_item(self.container)


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
            accent_color=COLOR_DANGER,
        )
        self.add_item(self.container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the command invoker can confirm this.", ephemeral=True)
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
