"""
Music rating system: link detection & manual song search, the dynamic
Pillow rating card, the 1-10 voting flow, Spotify auto-sync, the 12-hour
auto-close sweep, the music leaderboard (with min-score filter + elapsed
time), and admin song-management commands.

Every response here is a Components V2 layout inside a Container (see
core/components.py). Containers carry NO accent by default; an accent is
set only for song content: the rating card (score / cover color), song
listings, rating inspection, the duplicate notice. Config confirmations,
errors and one-line replies are plain, un-accented containers.
"""

from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from core import database
from core.checks import has_mod_permission
from core.config import RATING_WINDOW_HOURS, COLOR_ACCENT, COLOR_SUCCESS
from core.components import SimpleLayout, Layout, footer_line, notice
from core.helpers import create_music_card, format_elapsed, score_color
from core.music_utils import (
    find_music_link, fetch_song_metadata, search_song_metadata,
    lookup_song_genre, sync_to_spotify, remove_from_spotify,
)
from views.music_views import RatingView, ClosedRatingView, RenumberConfirmView


class MusicCog(commands.Cog, name="MusicCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ready_once = False

    async def cog_load(self):
        for row in await database.get_recent_songs(limit=500):
            # Persistent-view registration is purely local bookkeeping —
            # discord.py never transmits this object anywhere, it only uses
            # it to route future button clicks by custom_id. So constructing
            # RatingView here with no title/card_file (both default to
            # placeholders) is safe: the already-posted message on Discord's
            # side is untouched, this just re-wires the click handlers.
            self.bot.add_view(RatingView(row["id"]))

    def cog_unload(self):
        self.expiry_sweep_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._ready_once:
            self._ready_once = True
            self.expiry_sweep_loop.start()

    # ------------------------------------------------------------------
    # Background loop: auto-close any song whose rating window has elapsed
    # ------------------------------------------------------------------
    @tasks.loop(seconds=600)
    async def expiry_sweep_loop(self):
        try:
            expired = await database.get_expired_open_songs()
            for row in expired:
                await database.close_song(row["id"])
                channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
                if channel and row["message_id"]:
                    try:
                        message = await channel.fetch_message(row["message_id"])
                        song = await database.get_song(row["id"])
                        avg, count = await database.get_song_stats(row["id"])
                        await message.edit(view=ClosedRatingView(song, avg, count))
                    except Exception:
                        pass
        except Exception:
            pass

    @expiry_sweep_loop.before_loop
    async def before_expiry_sweep_loop(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Song posting
    # ------------------------------------------------------------------
    async def temp_lock_channel(self, channel: discord.TextChannel, duration: int):
        try:
            overwrite = channel.overwrites_for(channel.guild.default_role)
            overwrite.send_messages = False
            await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason="Music rating cooldown")
            await discord.utils.sleep_until(discord.utils.utcnow() + timedelta(seconds=duration))
            overwrite.send_messages = None
            await channel.set_permissions(channel.guild.default_role, overwrite=overwrite, reason="Music cooldown expired")
            await channel.send(
                view=SimpleLayout("🔓 **Channel unlocked** — you can now post the next track!"),
                delete_after=30,
            )
        except Exception:
            pass

    async def post_song(self, channel, source, url, title, artist, requester: discord.abc.User,
                         guild_id: int, channel_id: int, cover_url=None, preview_url=None):
        if not preview_url and title:
            q = f"{artist} {title}" if artist else title
            _, _, fallback_cover, _, fallback_preview = await search_song_metadata(self.bot.http_session, q)
            if fallback_preview:
                preview_url = fallback_preview
            if not cover_url and fallback_cover:
                cover_url = fallback_cover

        # Duplicate detection — point back at the existing card instead of
        # creating a second entry that would split votes across two rows.
        existing = await database.find_duplicate_song(guild_id, url, title, artist)
        if existing:
            existing_channel = channel.guild.get_channel(existing["channel_id"]) if channel.guild else None
            jump_text = None
            if existing_channel and existing["message_id"]:
                jump_text = f"https://discord.com/channels/{guild_id}/{existing['channel_id']}/{existing['message_id']}"
            avg, count = await database.get_song_stats(existing["id"])
            desc = f"🔁 **Already Posted**\n{requester.mention}\n\n"
            desc += f"**{existing['title']}**" + (f" by **{existing['artist']}**" if existing['artist'] else "")
            desc += f"\nCurrent rating: ⭐ **{avg:.1f}/10** ({count} votes)"
            if jump_text:
                desc += f"\n\n[Jump to the original post]({jump_text})"
            desc += "\n" + footer_line("Duplicate Detection")
            await channel.send(view=SimpleLayout(desc, accent=COLOR_ACCENT), allowed_mentions=discord.AllowedMentions(users=True))
            return None

        genre = await lookup_song_genre(self.bot.http_session, title, artist)

        song_id = await database.add_song(guild_id, channel_id, title, artist, source, url,
                                           requester.id, str(requester.display_name), cover_url, preview_url, genre)

        card_bytes, dominant_rgb = await create_music_card(self.bot.http_session, title, artist, cover_url, 0.0, 0, genre=genre)
        file = discord.File(fp=card_bytes, filename="rating_card.png")

        role_id_str, lock_time, _ = await database.async_get_music_config(guild_id)
        mentions = [requester.mention]
        if role_id_str:
            mentions.append(f"<@&{role_id_str}>")
        # Mentions have to live inside the TextDisplay content instead of a
        # message-level `content=` field — Components V2 messages can't
        # combine `content`/`embeds` with a view, but a mention placed
        # inside TextDisplay text still pings normally.
        ping_text = " ".join(mentions)

        view = RatingView(song_id, title=title, artist=artist, requester_name=requester.display_name,
                           preview_url=preview_url, url=url, card_file=file,
                           accent_rgb=dominant_rgb, ping_text=ping_text)

        msg = await channel.send(view=view, file=file, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await database.set_song_message_id(song_id, msg.id)

        if lock_time and int(lock_time) > 0 and isinstance(channel, discord.TextChannel):
            self.bot.loop.create_task(self.temp_lock_channel(channel, int(lock_time)))

        return msg

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        source, url = find_music_link(message.content)
        if source:
            _, _, music_channel_id = await database.async_get_music_config(message.guild.id)
            if music_channel_id and str(message.channel.id) == str(music_channel_id):
                title, artist, cover_url, preview_url = await fetch_song_metadata(self.bot.http_session, source, url)
                await self.post_song(message.channel, source, url, title, artist, message.author,
                                      message.guild.id, message.channel.id, cover_url, preview_url)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="songratings", aliases=["who-rated", "songvotes"], description="Check individual member ratings for a specific song ID.")
    @has_mod_permission()
    async def songratings(self, ctx: commands.Context, song_id: int):
        song = await database.get_song(song_id)
        if not song:
            await ctx.send(view=notice(f"❌ Song with ID `{song_id}` was not found in the database."), ephemeral=True)
            return

        rows = await database.get_song_ratings_breakdown(song_id)
        if not rows:
            await ctx.send(view=notice(f"❌ No ratings have been recorded for **{song['title']}** (ID: `{song_id}`) yet."), ephemeral=True)
            return

        lines = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"]) if ctx.guild else None
            user_mention = member.mention if member else f"<@{row['user_id']}>"
            lines.append(f"• {user_mention} — Score: **{row['score']}/10**")

        avg, count = await database.get_song_stats(song_id)
        artist_str = f" by **{song['artist']}**" if song['artist'] else ""

        text = (
            f"## 🔍 Rating Inspection for '{song['title']}'{artist_str}\n"
            f"**Song ID:** `{song_id}` | **Overall:** ⭐ **{avg:.1f}/10** ({count} votes)\n\n"
            + "\n".join(lines) + "\n" + footer_line("Admin Rating Inspector")
        )
        await ctx.send(view=SimpleLayout(text, accent=discord.Color.from_str(score_color(avg))), ephemeral=True)

    @commands.hybrid_command(name="closevoting", description="Freeze a song's score so no new votes can be cast.")
    @has_mod_permission()
    async def closevoting(self, ctx: commands.Context, song_id: int):
        song = await database.get_song(song_id)
        if not song:
            await ctx.send(view=notice(f"❌ Song ID `{song_id}` not found."), ephemeral=True)
            return
        if song.get("closed"):
            await ctx.send(view=notice(f"⚠️ Song ID `{song_id}` is already closed."), ephemeral=True)
            return

        await database.close_song(song_id)
        avg, count = await database.get_song_stats(song_id)
        artist_str = f" by **{song['artist']}**" if song["artist"] else ""

        # Also grey out the live rating buttons on the original message, not
        # just announce closure here — matches what the 12h auto-close sweep
        # already does, so a manual close and a timed close look the same.
        if song["channel_id"] and song["message_id"]:
            channel = self.bot.get_channel(song["channel_id"])
            if channel:
                try:
                    message = await channel.fetch_message(song["message_id"])
                    await message.edit(view=ClosedRatingView(song, avg, count))
                except Exception:
                    pass

        text = (
            "## 🔒 VOTING CLOSED\n"
            f"**{song['title']}**{artist_str}\n"
            f"Final score: ⭐ **{avg:.1f}/10** ({count} vote{'s' if count != 1 else ''})\n\n"
            "*No further votes will be accepted for this track.*\n"
            + footer_line("Admin Voting Control")
        )
        accent = discord.Color.from_str(score_color(avg)) if count > 0 else COLOR_ACCENT
        await ctx.send(view=SimpleLayout(text, accent=accent))

    @commands.hybrid_command(name="synctoplaylist", description="Manually add a song to the Spotify playlist, regardless of its score.")
    @has_mod_permission()
    async def synctoplaylist(self, ctx: commands.Context, song_id: int):
        song = await database.get_song(song_id)
        if not song:
            await ctx.send(view=notice(f"❌ Song ID `{song_id}` not found."), ephemeral=True)
            return
        if not song["url"] or "spotify.com/track/" not in (song["url"] or ""):
            await ctx.send(view=notice("❌ This song doesn't have a Spotify track URL — only Spotify-linked tracks can be synced."), ephemeral=True)
            return
        if song["synced"]:
            await ctx.send(view=notice(f"⚠️ Song ID `{song_id}` is already in the playlist."), ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success = await sync_to_spotify(self.bot.http_session, song["url"])
        if success:
            await database.mark_song_synced(song_id)
            artist_str = f" by **{song['artist']}**" if song["artist"] else ""
            text = (
                "## ✅ SYNCED TO PLAYLIST\n"
                f"**{song['title']}**{artist_str} was manually added to the server Spotify playlist.\n"
                + footer_line("Manual Spotify Sync")
            )
            await ctx.send(view=SimpleLayout(text, accent=COLOR_SUCCESS), ephemeral=True)
        else:
            await ctx.send(
                view=notice("❌ Spotify sync failed. Check that `SPOTIFY_USER_TOKEN` and `SPOTIFY_PLAYLIST_ID` are set and the token has the `playlist-modify` scope."),
                ephemeral=True,
            )

    @commands.hybrid_command(name="myratings", description="View your personal music rating statistics and top picks.")
    async def myratings(self, ctx: commands.Context):
        stats, top_rated = await database.get_user_stats(ctx.guild.id, ctx.author.id)
        if not stats or stats["total"] == 0:
            await ctx.send(view=notice("❌ You haven't rated any songs in this server yet!"), ephemeral=True)
            return

        avg_score = stats["avg_given"] or 0.0
        total_votes = stats["total"]

        text = (
            f"## 📊 {ctx.author.display_name}'s Music Profile\n"
            f"Total Songs Rated: **{total_votes}**\n"
            f"Average Score Given: **⭐ {avg_score:.1f}/10**\n"
        )
        if top_rated:
            lines = []
            for idx, song in enumerate(top_rated, start=1):
                artist = f" — {song['artist']}" if song['artist'] else ""
                lines.append(f"**{idx}. {song['title']}{artist}** (Your Score: **{song['score']}/10**)")
            text += "\n**🔝 Your Highest Rated Tracks**\n" + "\n".join(lines) + "\n"
        text += footer_line("Personal Music Taste Profile")

        items = [ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=ctx.author.display_avatar.url))]
        await ctx.send(view=Layout(*items, accent=COLOR_ACCENT), ephemeral=True)

    @commands.hybrid_command(name="setmusicchannel", aliases=["smc"], description="Restrict music link detection to a specific channel, or 'off' to allow any channel.")
    @has_mod_permission()
    async def setmusicchannel(self, ctx: commands.Context, channel: discord.TextChannel = None, off: bool = False):
        if off:
            await database.async_set_music_config(ctx.guild.id, channel_id=None)
            await ctx.send(view=SimpleLayout("🔕 **MUSIC CHANNEL RESTRICTION CLEARED**\nMusic links will now be detected in any channel."))
            return
        target_channel = channel or ctx.channel
        await database.async_set_music_config(ctx.guild.id, channel_id=target_channel.id)
        await ctx.send(view=SimpleLayout(f"🎵 **MUSIC CHANNEL BOUND**\nMusic link detection restricted to {target_channel.mention}."))

    @commands.hybrid_command(name="setmusicrole", description="Select a role to ping when a new song is posted. Omit the role to clear it.")
    @has_mod_permission()
    async def setmusicrole(self, ctx: commands.Context, role: discord.Role = None):
        if role is None:
            await database.async_set_music_config(ctx.guild.id, role_id=None)
            await ctx.send(view=SimpleLayout("🔕 **MUSIC ROLE CLEARED**\nNew song posts will no longer ping a role."))
        else:
            await database.async_set_music_config(ctx.guild.id, role_id=role.id)
            await ctx.send(view=SimpleLayout(f"🔔 **MUSIC ROLE UPDATED**\nWill now ping {role.mention} for new songs."))

    @commands.hybrid_command(name="setmusiclock", description="Set channel lock duration (in seconds) after a song is posted. Omit seconds to disable the lock.")
    @has_mod_permission()
    async def setmusiclock(self, ctx: commands.Context, seconds: int = 0):
        if seconds < 0:
            await ctx.send(view=notice("⚠️ Time cannot be negative."))
            return
        await database.async_set_music_config(ctx.guild.id, lock_time=seconds)
        status = f"Channel will lock for **{seconds} seconds**." if seconds > 0 else "Channel lock disabled."
        await ctx.send(view=SimpleLayout(f"⏱️ **MUSIC COOLDOWN UPDATED**\n{status}"))

    @commands.command(name="song")
    async def song_command(self, ctx: commands.Context, *, query: str):
        title, artist, cover_url, track_url, preview_url = await search_song_metadata(self.bot.http_session, query)
        await self.post_song(ctx.channel, "Manual Request", track_url, title or query, artist, ctx.author,
                              ctx.guild.id if ctx.guild else 0, ctx.channel.id, cover_url, preview_url)

    @app_commands.command(name="song", description="Nominate a song by name for rating (no link needed)")
    @app_commands.describe(query="Song name, e.g. 'Artist - Title'")
    async def song_slash(self, interaction: discord.Interaction, query: str):
        await interaction.response.send_message(view=notice(f"🔎 Searching for **{query}**..."), ephemeral=True)
        try:
            title, artist, cover_url, track_url, preview_url = await search_song_metadata(self.bot.http_session, query)
            await self.post_song(interaction.channel, "Manual Request", track_url, title or query, artist, interaction.user,
                                  interaction.guild.id if interaction.guild else 0, interaction.channel.id, cover_url, preview_url)
        except Exception as e:
            print(f"[music] /song failed for query {query!r}: {e!r}")
            try:
                await interaction.followup.send(view=notice("❌ Couldn't post that song — try again in a moment."), ephemeral=True)
            except Exception:
                pass

    @app_commands.command(name="musicleaderboard", description="Show the top rated songs in this server")
    @app_commands.describe(min_score="Only show songs with an average rating at or above this value (0-10)")
    async def music_leaderboard(self, interaction: discord.Interaction, min_score: app_commands.Range[float, 0.0, 10.0] = 0.0):
        rows = await database.get_music_leaderboard(interaction.guild.id, limit=10, min_votes=2, min_score=min_score)
        if not rows:
            msg = ("No rated songs yet — post a link or use `/song` to get started!" if min_score == 0.0
                   else f"No songs found with an average rating of **{min_score}/10** or higher.")
            await interaction.response.send_message(view=notice(msg), ephemeral=True)
            return

        lines = []
        for i, row in enumerate(rows, start=1):
            title = row["title"] or "Unknown"
            artist = f" — {row['artist']}" if row["artist"] else ""
            elapsed = format_elapsed(row["created_at"])
            lines.append(f"**{i}. {title}{artist}** (ID: `{row['id']}`) — ⭐ {row['avg_score']:.1f}/10 ({row['votes']} votes) • 🕒 {elapsed}")

        title_text = "🏆 Top Rated Songs" if min_score == 0.0 else f"🏆 Top Rated Songs (≥ {min_score}/10)"
        text = f"## {title_text}\n" + "\n".join(lines)
        await interaction.response.send_message(view=SimpleLayout(text, accent=COLOR_ACCENT))

    @app_commands.command(name="removesong", description="Removes a song and its votes from the database and Spotify playlist.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(song_id="The ID of the song to delete from the database")
    async def remove_song(self, interaction: discord.Interaction, song_id: int):
        await interaction.response.defer()

        try:
            song = await database.get_song(song_id)
            if not song:
                await interaction.followup.send(view=notice(f"❌ Song with ID `{song_id}` was not found in the database."))
                return

            title = song["title"] or "Unknown Title"
            artist = song["artist"]

            if song["synced"] and song["url"]:
                removed = await remove_from_spotify(self.bot.http_session, song["url"])
                if not removed:
                    await interaction.followup.send(
                        view=notice(f"⚠️ Failed to remove **{title}** from the Spotify playlist — continuing with database deletion.")
                    )

            await database.delete_song(song_id)

            artist_str = f" by **{artist}**" if artist else ""
            await interaction.followup.send(view=notice(f"✅ Successfully removed **{title}**{artist_str} (ID: `{song_id}`) from the database."))
        except Exception as e:
            print(f"[music] /removesong failed for id {song_id}: {e!r}")
            try:
                await interaction.followup.send(view=notice("❌ Something went wrong removing that song."))
            except Exception:
                pass

    @commands.hybrid_command(name="renumbersongs", description="Re-sequences song IDs to close gaps left by deletions, and repairs live rating buttons.")
    @commands.has_permissions(administrator=True)
    async def renumbersongs(self, ctx: commands.Context):
        view = RenumberConfirmView(ctx.author.id)
        confirm_msg = await ctx.send(view=view)

        await view.wait()
        if not view.confirmed:
            await confirm_msg.edit(view=notice("❌ Renumber cancelled."))
            return

        await confirm_msg.edit(view=notice("⏳ Renumbering songs and repairing rating messages..."))

        mapping = await database.renumber_songs()

        repaired, failed, unchanged = 0, 0, 0
        for row in mapping:
            if row["old_id"] == row["new_id"]:
                unchanged += 1
                continue
            channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            if not channel or not row["message_id"]:
                failed += 1
                continue
            try:
                message = await channel.fetch_message(row["message_id"])
                song = await database.get_song(row["new_id"])
                avg, count = await database.get_song_stats(row["new_id"])
                if song and song.get("closed"):
                    await message.edit(view=ClosedRatingView(song, avg, count))
                else:
                    new_view = RatingView(
                        row["new_id"], title=song["title"] if song else None,
                        artist=song["artist"] if song else None,
                        requester_name=song["requested_by_name"] if song else None,
                        avg=avg, count=count,
                        preview_url=song["preview_url"] if song else None,
                        url=song["url"] if song else None,
                        # A string in the attachment://<filename> format
                        # references the message's *existing* attachment
                        # instead of uploading a new one — we're only
                        # repointing the buttons' custom_ids to the new song
                        # id, not touching the image, so nothing needs
                        # re-rendering or re-uploading here.
                        card_file="attachment://rating_card.png",
                    )
                    await message.edit(view=new_view)
                repaired += 1
            except Exception:
                failed += 1

        result_text = (
            "## ✅ Renumber Complete\n"
            f"┣ IDs reassigned: **{len(mapping)}**\n"
            f"┣ Unchanged (already sequential): **{unchanged}**\n"
            f"┣ Rating messages repaired: **{repaired}**\n"
            f"┗ Messages that couldn't be repaired: **{failed}**"
        )
        if failed:
            result_text += "\n\n*Songs with unreachable messages still work via commands (`/songratings`, `/closevoting`, etc.) — only their Discord buttons are stale.*"
        result_text += "\n" + footer_line("Admin Database Maintenance")
        await ctx.send(view=SimpleLayout(result_text))


async def setup(bot: commands.Bot):
    # song_slash / music_leaderboard / remove_song are plain app_commands
    # (not hybrid) defined on the cog — discord.py's Cog machinery detects
    # and registers them to bot.tree automatically as part of add_cog(),
    # so no manual bot.tree.add_command() call is needed (and calling it
    # here would raise CommandAlreadyRegistered).
    await bot.add_cog(MusicCog(bot))
