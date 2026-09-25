"""
Persistent Views for the music rating system: the 1-10 rating button grid,
its disabled/closed replica shown once voting ends, and the confirmation
prompt for the admin-only /renumbersongs command.
"""

from datetime import datetime, timezone, timedelta

import discord

from core import database
from core.config import RATING_WINDOW_HOURS
from core.helpers import create_music_card, _score_color
from core.music_utils import sync_to_spotify, remove_from_spotify


class RatingButton(discord.ui.Button):
    def __init__(self, score: int, song_id: int):
        row = 0 if score <= 5 else 1
        super().__init__(label=str(score), style=discord.ButtonStyle.secondary,
                          custom_id=f"rate|{song_id}|{score}", row=row)
        self.score = score
        self.song_id = song_id

    async def callback(self, interaction: discord.Interaction):
        song = await database.get_song(self.song_id)

        if song and song.get("closed"):
            await interaction.response.send_message(
                "🔒 Voting on this track is closed.", ephemeral=True
            )
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

        embed_color = (discord.Color.from_str(_score_color(avg)) if count > 0
                        else discord.Color.from_rgb(*dominant_rgb))

        message = interaction.message
        new_embed = message.embeds[0].copy() if message.embeds else discord.Embed()
        new_embed.color = embed_color
        new_embed.set_image(url="attachment://rating_card.png")
        new_embed.set_footer(text=f"ID: {song['id']} • Your vote: {self.score}/10 — use buttons to change")

        await interaction.response.edit_message(embed=new_embed, attachments=[new_file], view=self.view)
        await interaction.followup.send(f"You rated this **{self.score}/10** 🎵{sync_alert}", ephemeral=True)


class RatingView(discord.ui.View):
    def __init__(self, song_id: int):
        super().__init__(timeout=None)
        self.song_id = song_id
        for i in range(1, 11):
            self.add_item(RatingButton(i, song_id))


class ClosedRatingView(discord.ui.View):
    """Read-only replica of RatingView with every button disabled — swapped
    in once a song's rating window elapses (or an admin manually closes it
    via /closevoting) so the buttons visibly grey out instead of silently
    rejecting clicks."""
    def __init__(self):
        super().__init__(timeout=None)
        for i in range(1, 11):
            row = 0 if i <= 5 else 1
            self.add_item(discord.ui.Button(label=str(i), style=discord.ButtonStyle.secondary,
                                             disabled=True, row=row, custom_id=f"closed_rating|{i}"))


class RenumberConfirmView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.confirmed = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the command invoker can confirm this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Renumber", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()
