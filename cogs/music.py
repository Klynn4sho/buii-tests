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

import logging

logger = logging.getLogger(__name__)
import asyncio
import re
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from core import database
from core.checks import has_mod_permission, has_admin_permission
from core.config import (
    RATING_WINDOW_HOURS,
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_USER_TOKEN, SPOTIFY_REFRESH_TOKEN,
    SPOTIFY_PLAYLIST_ID, MUSIC_GUILD_ID,
)
from core.components import SimpleLayout, Layout, footer_line, notice
from core.helpers import create_music_card, format_elapsed
from core.music_utils import (
    find_music_link, fetch_song_metadata, search_song_metadata,
    lookup_song_genre, sync_to_spotify, remove_from_spotify, spotify_playlist_tracks,
)
from views.music_views import RatingView, ClosedRatingView, RenumberConfirmView, SongLeaderboardView, MemberRatingsView, MusicProfileView


class MusicCog(commands.Cog, name="MusicCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._ready_once = False
        self.spotify_sync_lock = asyncio.Lock()

    async def cog_load(self):
        for row in await database.get_recent_songs(limit=500):
            # Persistent-view registration is purely local bookkeeping —
            # discord.py never transmits this object anywhere, it only uses
            # it to route future button clicks by custom_id. So constructing
            # RatingView here with no title/card_file (both default to
            # placeholders) is safe: the already-posted message on Discord's
            # side is untouched, this just re-wires the click handlers.
            self.bot.add_view(RatingView(
                row["guild_id"], row["id"], row["song_number"],
                url=row["url"],
                preview_used=bool(row["preview_used"]),
                playlist_visible=bool(getattr(self.bot, "spotify_premium", False)),
            ))

    def cog_unload(self):
        self.expiry_sweep_loop.cancel()

    async def refresh_song_message(self, guild_id: int, song_id: int):
        """Re-render the Pillow card so lifecycle status stays current."""
        song = await database.get_song(guild_id, song_id)
        if not song or not song.get("channel_id") or not song.get("message_id"):
            return
        channel = self.bot.get_channel(song["channel_id"])
        if channel is None:
            return
        try:
            message = await channel.fetch_message(song["message_id"])
            avg, count = await database.get_song_stats(guild_id, song_id)
            status = "playlist" if song.get("synced") else "closed" if song.get("closed") else "open"
            card_bytes, dominant_rgb = await create_music_card(
                self.bot.http_session, song["title"], song["artist"], song["cover_url"],
                avg, count, genre=song["genre"], song_number=song["song_number"], status=status,
            )
            card_file = discord.File(fp=card_bytes, filename="rating_card.png")
            if song.get("closed"):
                view = ClosedRatingView()
            else:
                view = RatingView(
                    guild_id, song["id"], song["song_number"],
                    title=song["title"], artist=song["artist"],
                    requester_name=song["requested_by_name"], avg=avg, count=count,
                    preview_url=song["preview_url"], url=song["url"], card_file=card_file,
                    accent_rgb=dominant_rgb,
                    # This is an edit of an existing card, so do not
                    # reintroduce a requester mention and notify them again.
                    ping_text=None,
                    preview_used=bool(song.get("preview_used")),
                    playlist_visible=bool(getattr(self.bot, "spotify_premium", False)),
                )
            await message.edit(view=view, attachments=[card_file])
        except Exception:
            logger.debug("Non-fatal song card refresh failure", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._ready_once:
            self._ready_once = True
            try:
                await self.reconcile_playlist_state()
            except Exception:
                logger.exception("[music] startup playlist reconciliation failed")
            try:
                await self.close_expired_songs()
            except Exception:
                logger.exception("[music] startup expiry recovery failed")
            self.expiry_sweep_loop.start()

    async def reconcile_playlist_state(self):
        """Mark database songs that are already present in the configured playlist."""
        if not SPOTIFY_PLAYLIST_ID or not getattr(self.bot, "spotify_premium", False):
            return
        rows = await database.get_unsynced_spotify_songs(MUSIC_GUILD_ID)
        playlist_tracks = await spotify_playlist_tracks(self.bot.http_session)
        if playlist_tracks is None:
            return
        playlist_ids = {track["id"] for track in playlist_tracks}
        playlist_keys = {
            re.sub(r"[^a-z0-9]+", " ", f"{track['artist']} {track['title']}".casefold()).strip()
            for track in playlist_tracks
        }
        matched = 0
        for row in rows:
            track_id = (row["url"] or "").split("spotify.com/track/", 1)[-1].split("?", 1)[0]
            song_key = re.sub(
                r"[^a-z0-9]+", " ",
                f"{row.get('artist') or ''} {row.get('title') or ''}".casefold(),
            ).strip()
            if track_id in playlist_ids or song_key in playlist_keys:
                if not row.get("synced"):
                    await database.mark_song_synced(MUSIC_GUILD_ID, row["id"])
                matched += 1
        await asyncio.to_thread(
            database._raw_set_playlist_sync_state,
            MUSIC_GUILD_ID, len(playlist_ids), matched,
        )

    async def sync_qualifying_locked_song(self, guild_id: int, song_id: int):
        """Sync only songs that finished the normal 12-hour rating window."""
        song = await database.get_song(guild_id, song_id)
        if not song or song.get("synced") or not song.get("url"):
            return False

        created_at = song.get("created_at")
        if not created_at or datetime.now(timezone.utc) - created_at < timedelta(hours=RATING_WINDOW_HOURS):
            return False

        average, votes = await database.get_song_stats(guild_id, song_id)
        if votes < 4 or average < 7.0:
            return False

        # Prevent concurrent expiry/manual syncs from adding the same track twice.
        async with self.spotify_sync_lock:
            if not getattr(self.bot, "spotify_premium", False):
                return False
            latest = await database.get_song(guild_id, song_id)
            if not latest or latest.get("synced"):
                return False
            sync_success = await sync_to_spotify(self.bot.http_session, latest["url"])
            await database.async_record_dashboard_event(guild_id, "sync", "spotify", sync_success)
            if sync_success:
                await database.mark_song_synced(guild_id, song_id)
                return True
        return False

    # ------------------------------------------------------------------
    # Background loop: auto-close any song whose rating window has elapsed
    # ------------------------------------------------------------------
    async def close_expired_songs(self):
        """Recover overdue open songs immediately after startup and on schedule."""
        expired = await database.get_expired_open_songs()
        for row in expired:
            await database.close_song(row["guild_id"], row["id"])
            await self.sync_qualifying_locked_song(row["guild_id"], row["id"])
            await self.refresh_song_message(row["guild_id"], row["id"])

    @tasks.loop(seconds=600)
    async def expiry_sweep_loop(self):
        try:
            await self.close_expired_songs()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("[music] operation failed")

    @expiry_sweep_loop.before_loop
    async def before_expiry_sweep_loop(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Song posting
    # ------------------------------------------------------------------
    async def send_duplicate_notice(self, channel, guild_id: int, requester, existing: dict):
        existing_channel = channel.guild.get_channel(existing["channel_id"]) if channel.guild else None
        jump_url = None
        if existing_channel and existing["message_id"]:
            jump_url = f"https://discord.com/channels/{guild_id}/{existing['channel_id']}/{existing['message_id']}"

        avg, count = await database.get_song_stats(guild_id, existing["id"])
        title = existing["title"] or "Unknown title"
        artist = f" — {existing['artist']}" if existing.get("artist") else ""
        text = (
            "## Already posted\n"
            f"Posted by **{requester.display_name}**\n"
            f"**{title}**{artist}\n"
            f"⭐ **{avg:.1f}/10** · {count} votes"
        )
        items = [ui.TextDisplay(text)]
        if jump_url:
            items.append(ui.ActionRow(
                ui.Button(label="View original post", style=discord.ButtonStyle.link, url=jump_url)
            ))
        items.append(ui.TextDisplay(footer_line("Duplicate detected")))
        await channel.send(
            view=Layout(*items),
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
            logger.debug("Non-fatal exception suppressed", exc_info=True)

    async def post_song(self, channel, source, url, title, artist, requester: discord.abc.User,
                         guild_id: int, channel_id: int, cover_url=None, preview_url=None):
        if not guild_id:
            return None
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
            await self.send_duplicate_notice(channel, guild_id, requester, existing)
            return None

        genre = await lookup_song_genre(self.bot.http_session, title, artist)

        song_ref = await database.add_song(guild_id, channel_id, title, artist, source, url,
                                       requester.id, str(requester.display_name), cover_url, preview_url, genre)
        if song_ref.get("existing"):
            await self.send_duplicate_notice(channel, guild_id, requester, song_ref["existing"])
            return None

        song_id = song_ref["id"]
        song_number = song_ref["song_number"]

        card_bytes, dominant_rgb = await create_music_card(self.bot.http_session, title, artist, cover_url, 0.0, 0, genre=genre, song_number=song_number, status="open")
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

        view = RatingView(guild_id, song_id, song_number, title=title, artist=artist, requester_name=requester.display_name,
                           preview_url=preview_url, url=url, card_file=file,
                           accent_rgb=dominant_rgb, ping_text=ping_text)

        msg = await channel.send(view=view, file=file, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await database.set_song_message_id(guild_id, song_id, msg.id)

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
        # Music submissions are enabled in every server. Per-server channel,
        # role, lock, and song rows are resolved from the message guild.

        source, url = find_music_link(message.content)
        if source:
            _, _, music_channel_id = await database.async_get_music_config(message.guild.id)
            if not music_channel_id or str(message.channel.id) == str(music_channel_id):
                title, artist, cover_url, preview_url = await fetch_song_metadata(self.bot.http_session, source, url)
                await self.post_song(message.channel, source, url, title, artist, message.author,
                                      message.guild.id, message.channel.id, cover_url, preview_url)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="songratings", aliases=["sr", "who-rated", "songvotes"], description="Check individual member ratings for a specific song ID.")
    @has_mod_permission()
    async def songratings(self, ctx: commands.Context, song_id: int):
        song = await database.get_song_by_number(ctx.guild.id, song_id)
        if not song:
            await ctx.send(view=notice(f"❌ Song with ID `{song_id}` was not found in the database."), ephemeral=True)
            return

        rows = await database.get_song_ratings_breakdown(ctx.guild.id, song["id"])
        if not rows:
            await ctx.send(view=notice(f"❌ No ratings have been recorded for **{song['title']}** (ID: `{song_id}`) yet."), ephemeral=True)
            return

        lines = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"]) if ctx.guild else None
            display_name = member.display_name if member else f"User {row['user_id']}"
            lines.append(f"• **{display_name}** — Score: **{row['score']}/10**")

        avg, count = await database.get_song_stats(ctx.guild.id, song["id"])
        artist_str = f" by **{song['artist']}**" if song['artist'] else ""

        text = (
            f"## 🔍 Rating Inspection for '{song['title']}'{artist_str}\n"
            f"**Song ID:** `{song_id}` | **Overall:** ⭐ **{avg:.1f}/10** ({count} votes)\n\n"
            + "\n".join(lines) + "\n" + footer_line("Admin Rating Inspector")
        )
        await ctx.send(
            view=SimpleLayout(text),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="memberratings", aliases=["mr", "userratings", "ratingsby"], description="Show every rating submitted by a member.")
    @has_mod_permission()
    @app_commands.describe(member="Member to inspect (defaults to you)", page="Page number")
    async def memberratings(self, ctx: commands.Context, member: discord.Member = None, page: int = 1):
        member = member or ctx.author
        page_size = 15
        total = await database.count_member_ratings(ctx.guild.id, member.id)
        if not total:
            await ctx.send(view=notice(f"❌ **{member.display_name}** has not rated any songs in this server."), ephemeral=True)
            return
        page = max(1, min(int(page), (total + page_size - 1) // page_size))
        rows = await database.get_member_ratings(
            ctx.guild.id, member.id, limit=page_size, offset=(page - 1) * page_size
        )
        lines = []
        for row in rows:
            artist = f" — {row['artist']}" if row["artist"] else ""
            lines.append(
                f"• **ID {row['song_number']} · {row['title']}**{artist} — **{row['score']}/10**"
            )
        text = (
            f"## Ratings by {member.display_name}\n"
            f"**Total ratings:** {total} · **Page:** {page}/{(total + page_size - 1) // page_size}\n\n"
            + "\n".join(lines)
            + "\n"
            + footer_line("Member Rating Inspector")
        )
        await ctx.send(
            view=MemberRatingsView(
                ctx.guild.id,
                member.id,
                member.display_name,
                ctx.author.id,
                total,
                rows,
                page=page,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="voteblacklist", aliases=["vbl"], description="Block or unblock a member from submitting music ratings.")
    @has_mod_permission()
    @app_commands.describe(member="Member to block/unblock, or leave empty to list blocked members", remove="Remove the member from the blacklist")
    async def voteblacklist(self, ctx: commands.Context, member: discord.Member = None, remove: bool = False):
        if member is None:
            rows = await database.get_vote_blacklist(ctx.guild.id)
            if not rows:
                await ctx.send(view=notice("✅ The vote blacklist is empty."), ephemeral=True)
                return
            lines = []
            for user_id, _created_at in rows:
                blocked_member = ctx.guild.get_member(int(user_id))
                label = blocked_member.mention if blocked_member else f"<@{user_id}>"
                lines.append(f"• {label}")
            await ctx.send(
                view=SimpleLayout("## 🚫 Vote Blacklist\n" + "\n".join(lines)),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await database.set_vote_blacklist(ctx.guild.id, member.id, blocked=not remove)
        if remove:
            text = f"✅ {member.mention} can submit music ratings again."
        else:
            text = f"🚫 {member.mention} is now blocked from submitting music ratings."
        await ctx.send(
            view=SimpleLayout(text),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.hybrid_command(name="closevoting", aliases=["cv"], description="Freeze a song's score so no new votes can be cast.")
    @has_mod_permission()
    async def closevoting(self, ctx: commands.Context, song_id: int):
        song = await database.get_song_by_number(ctx.guild.id, song_id)
        if not song:
            await ctx.send(view=notice(f"❌ Song ID `{song_id}` not found."), ephemeral=True)
            return
        if song.get("closed"):
            await ctx.send(view=notice(f"⚠️ Song ID `{song_id}` is already closed."), ephemeral=True)
            return

        await database.close_song(ctx.guild.id, song["id"])
        avg, count = await database.get_song_stats(ctx.guild.id, song["id"])
        artist_str = f" by **{song['artist']}**" if song["artist"] else ""

        await self.refresh_song_message(ctx.guild.id, song["id"])

        text = (
            "## 🔒 VOTING CLOSED\n"
            f"**{song['title']}**{artist_str}\n"
            f"Final score: ⭐ **{avg:.1f}/10** ({count} vote{'s' if count != 1 else ''})\n\n"
            "*No further votes will be accepted for this track.*\n"
            + footer_line("Admin Voting Control")
        )
        await ctx.send(view=SimpleLayout(text))

    @commands.hybrid_command(name="spotify", aliases=["sinfo", "spotify_status", "spotifyinfo"], description="Show Spotify integration status without exposing secrets.")
    @has_mod_permission()
    async def spotify_status(self, ctx: commands.Context):
        configured = {
            "Client ID": bool(SPOTIFY_CLIENT_ID),
            "Client secret": bool(SPOTIFY_CLIENT_SECRET),
            "Refresh token": bool(SPOTIFY_REFRESH_TOKEN),
            "User token": bool(SPOTIFY_USER_TOKEN),
            "Playlist ID": bool(SPOTIFY_PLAYLIST_ID),
        }
        premium = bool(getattr(self.bot, "spotify_premium", False))
        can_read = premium and bool(SPOTIFY_PLAYLIST_ID) and (
            bool(SPOTIFY_REFRESH_TOKEN or SPOTIFY_USER_TOKEN)
            or bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)
        )
        can_modify = premium and bool(SPOTIFY_PLAYLIST_ID and (SPOTIFY_REFRESH_TOKEN or SPOTIFY_USER_TOKEN))
        lines = [
            "## SPOTIFY STATUS",
            f"**Integration** · {'✅ Ready' if can_modify else '⚠️ Read-only or incomplete'}",
            f"**Authorization** · {'✅ Playlist editing enabled' if can_modify else '⚠️ Playlist editing token missing'}",
            f"**Playlist read** · {'✅ Available' if can_read else '❌ No usable Spotify credentials'}",
            f"**Premium access** · {'✅ Active' if premium else '❌ Required for playlist sync'}",
            f"**Playlist** · {'✅ Configured' if SPOTIFY_PLAYLIST_ID else '❌ Playlist ID missing'}",
            "⚠️ Spotify playlist API access may require an active Premium subscription for the app owner.",
            "",
            "**Configuration checks**",
        ]
        lines.extend(
            f"{'✅' if present else '❌'} {label}"
            for label, present in configured.items()
        )
        lines.extend([
            "",
            f"**Sync rules** · locked for {RATING_WINDOW_HOURS}h · at least 4 votes · average ≥ 7.0",
            "-# Access and refresh tokens are never displayed.",
            footer_line("Spotify Integration"),
        ])
        spotify_view = SimpleLayout("\n".join(lines))
        await ctx.send(
            view=spotify_view,
            ephemeral=bool(getattr(ctx, "interaction", None)),
        )

    @commands.hybrid_command(name="synctoplaylist", aliases=["stp", "syncsong"], description="Sync a locked, qualifying song to the Spotify playlist.")
    @has_mod_permission()
    async def synctoplaylist(self, ctx: commands.Context, song_id: int):
        if not getattr(self.bot, "spotify_premium", False):
            await ctx.send(view=notice("❌ Spotify playlist sync is unavailable because the configured account is not Premium."), ephemeral=True)
            return
        if ctx.guild.id != MUSIC_GUILD_ID:
            await ctx.send(view=notice("❌ Playlist sync is enabled only in the configured music server."), ephemeral=True)
            return
        song = await database.get_song_by_number(ctx.guild.id, song_id)
        if not song:
            await ctx.send(view=notice(f"❌ Song ID `{song_id}` not found."), ephemeral=True)
            return
        if not song["url"] or "spotify.com/track/" not in (song["url"] or ""):
            await ctx.send(view=notice("❌ This song doesn't have a Spotify track URL — only Spotify-linked tracks can be synced."), ephemeral=True)
            return
        if song["synced"]:
            await ctx.send(view=notice(f"⚠️ Song ID `{song_id}` is already in the playlist."), ephemeral=True)
            return

        created_at = song.get("created_at")
        average, votes = await database.get_song_stats(ctx.guild.id, song["id"])
        is_locked = bool(song.get("closed")) and created_at and (
            datetime.now(timezone.utc) - created_at >= timedelta(hours=RATING_WINDOW_HOURS)
        )
        if not is_locked or votes < 4 or average < 7.0:
            await ctx.send(
                view=notice(
                    f"❌ This song is not eligible yet. It must be locked after {RATING_WINDOW_HOURS} hours, "
                    f"have at least 4 votes, and average at least 7.0/10. "
                    f"Current: {average:.1f}/10 ({votes} votes)."
                ),
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)
        success = await sync_to_spotify(self.bot.http_session, song["url"])
        await database.async_record_dashboard_event(ctx.guild.id, "sync", "spotify", success)
        if success:
            await database.mark_song_synced(ctx.guild.id, song["id"])
            await self.refresh_song_message(ctx.guild.id, song["id"])
            artist_str = f" by **{song['artist']}**" if song["artist"] else ""
            text = (
                "## ✅ SYNCED TO PLAYLIST\n"
                f"**{song['title']}**{artist_str} was manually added to the server Spotify playlist.\n"
                + footer_line("Manual Spotify Sync")
            )
            await ctx.send(view=SimpleLayout(text), ephemeral=True)
        else:
            await ctx.send(
                view=notice("❌ Spotify sync failed. Check that `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, and `SPOTIFY_PLAYLIST_ID` are set and the token has the `playlist-modify` scope."),
                ephemeral=True,
            )

    @commands.hybrid_command(
        name="rebuildplaylist", aliases=["rpl"],
        description="Remove the current playlist tracks and rebuild it from qualifying server songs.",
    )
    @has_admin_permission()
    async def rebuildplaylist(self, ctx: commands.Context):
        if not getattr(self.bot, "spotify_premium", False):
            await ctx.send(view=notice("❌ Spotify playlist sync is unavailable because the configured account is not Premium."), ephemeral=True)
            return
        stage = "starting"
        try:
            if ctx.guild.id != MUSIC_GUILD_ID:
                await ctx.send(view=notice("❌ Playlist rebuild is enabled only in the configured music server."), ephemeral=True)
                return
            if not SPOTIFY_PLAYLIST_ID:
                await ctx.send(view=notice("❌ No Spotify playlist is configured."), ephemeral=True)
                return

            await ctx.defer(ephemeral=True)
            stage = "reading the Spotify playlist"
            playlist_tracks = await spotify_playlist_tracks(self.bot.http_session)
            if playlist_tracks is None:
                await ctx.send(
                    view=notice("❌ I couldn't read the configured Spotify playlist. Check the playlist access token and playlist ID."),
                    ephemeral=True,
                )
                return

            removed = 0
            remove_failures = 0
            stage = "removing existing playlist tracks"
            for track in playlist_tracks:
                track_url = f"https://open.spotify.com/track/{track['id']}"
                if await remove_from_spotify(self.bot.http_session, track_url):
                    removed += 1
                else:
                    remove_failures += 1

            stage = "loading catalog songs"
            candidates = await database.get_unsynced_spotify_songs(MUSIC_GUILD_ID)
            for row in candidates:
                await database.unmark_song_synced(MUSIC_GUILD_ID, row["id"])

            added = 0
            rejected = 0
            add_failures = 0
            stage = "adding qualifying songs"
            for row in candidates:
                song = await database.get_song(MUSIC_GUILD_ID, row["id"])
                if not song or not song.get("closed"):
                    rejected += 1
                    continue
                created_at = song.get("created_at")
                if isinstance(created_at, str):
                    created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if created_at and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                if not created_at or datetime.now(timezone.utc) - created_at < timedelta(hours=RATING_WINDOW_HOURS):
                    rejected += 1
                    continue
                average, votes = await database.get_song_stats(MUSIC_GUILD_ID, row["id"])
                if votes < 4 or average < 7.0:
                    rejected += 1
                    continue
                if await sync_to_spotify(self.bot.http_session, row["url"]):
                    await database.mark_song_synced(MUSIC_GUILD_ID, row["id"])
                    added += 1
                else:
                    add_failures += 1

            stage = "refreshing dashboard sync counts"
            await self.reconcile_playlist_state()
            await ctx.send(
                view=SimpleLayout(
                    "## ✅ Playlist rebuilt\n"
                    f"Removed existing tracks: **{removed}**\n"
                    f"Added qualifying bot tracks: **{added}**\n"
                    f"Skipped non-qualifying songs: **{rejected}**\n"
                    f"Removal failures: **{remove_failures}** · Add failures: **{add_failures}**\n"
                    + footer_line("Spotify Playlist Rebuild")
                ),
                ephemeral=True,
            )
        except Exception:
            logger.exception("[music] playlist rebuild failed during %s", stage)
            try:
                await ctx.send(
                    view=notice(f"❌ Playlist rebuild failed while {stage}. The error was logged."),
                    ephemeral=True,
                )
            except Exception:
                logger.debug("Could not send playlist rebuild failure response", exc_info=True)


    @commands.hybrid_command(
        name="musicprofile", aliases=["mp", "myratings"],
        description="View your music profile, or another member's profile.",
    )
    @app_commands.describe(member="The member whose music profile you want to view")
    async def musicprofile(self, ctx: commands.Context, member: discord.Member = None):
        target = member or ctx.author
        stats, top_rated = await database.get_user_stats(ctx.guild.id, target.id)
        requested_total = await database.count_member_requested_songs(ctx.guild.id, target.id)
        if (not stats or stats["total"] == 0) and requested_total == 0:
            owner = "You haven't" if target.id == ctx.author.id else f"{target.display_name} hasn't"
            await ctx.send(view=notice(f"❌ {owner} rated or requested any songs in this server yet!"), ephemeral=True)
            return

        avg_score = (stats["avg_given"] or 0.0) if stats else 0.0
        total_votes = stats["total"] if stats else 0
        possessive = "Your" if target.id == ctx.author.id else f"{target.display_name}'s"

        text = (
            f"## {possessive} Music Profile\n"
            f"Total Songs Rated: **{total_votes}**\n"
            f"Average Score Given: **{avg_score:.1f}/10**\n"
        )
        if top_rated:
            lines = []
            for idx, song in enumerate(top_rated, start=1):
                artist = f" — {song['artist']}" if song['artist'] else ""
                score_label = "Your Score" if target.id == ctx.author.id else "Score"
                lines.append(f"**{idx}. {song['title']}{artist}** ({score_label}: **{song['score']}/10**)")
            text += "\n**Highest Rated Tracks**\n" + "\n".join(lines) + "\n"
        text += footer_line("Music Profile")

        await ctx.send(
            view=MusicProfileView(
                ctx.guild.id,
                target.id,
                target.display_name,
                ctx.author.id,
                text,
                target.display_avatar.url,
                requested_total,
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(name="setmusicchannel", aliases=["smc"], description="Restrict music link detection to a specific channel, or 'off' to allow any channel.")
    @has_mod_permission()
    async def setmusicchannel(self, ctx: commands.Context, channel: discord.TextChannel = None, off: bool = False):
        if off:
            await database.async_set_music_config(ctx.guild.id, channel_id=None)
            await ctx.send(view=SimpleLayout("🔕 **MUSIC CHANNEL RESTRICTION CLEARED**\nMusic links will now be detected in any channel."))
            return
        target_channel = channel or ctx.channel
        await database.async_set_music_config(ctx.guild.id, channel_id=target_channel.id)
        await ctx.send(view=SimpleLayout(f"🎵 **MUSIC CHANNEL BOUND**\nMusic link detection restricted to #{target_channel.name}."))

    @commands.hybrid_command(name="setmusicrole", aliases=["smr"], description="Select a role to ping when a new song is posted. Omit the role to clear it.")
    @has_mod_permission()
    async def setmusicrole(self, ctx: commands.Context, role: discord.Role = None):
        if role is None:
            await database.async_set_music_config(ctx.guild.id, role_id=None)
            await ctx.send(view=SimpleLayout("🔕 **MUSIC ROLE CLEARED**\nNew song posts will no longer ping a role."))
        else:
            await database.async_set_music_config(ctx.guild.id, role_id=role.id)
            await ctx.send(view=SimpleLayout(f"🔔 **MUSIC ROLE UPDATED**\nWill now ping {role.name} for new songs."))

    @commands.hybrid_command(name="setmusiclock", aliases=["sml"], description="Set channel lock duration (in seconds) after a song is posted. Omit seconds to disable the lock.")
    @has_mod_permission()
    async def setmusiclock(self, ctx: commands.Context, seconds: int = 0):
        if seconds < 0:
            await ctx.send(view=notice("⚠️ Time cannot be negative."))
            return
        await database.async_set_music_config(ctx.guild.id, lock_time=seconds)
        status = f"Channel will lock for **{seconds} seconds**." if seconds > 0 else "Channel lock disabled."
        await ctx.send(view=SimpleLayout(f"⏱️ **MUSIC COOLDOWN UPDATED**\n{status}"))

    @commands.command(name="song", aliases=["s"])
    async def song_command(self, ctx: commands.Context, *, query: str):
        try:
            title, artist, cover_url, track_url, preview_url = await search_song_metadata(
                self.bot.http_session, query
            )
            if not title and not track_url:
                await ctx.send(
                    view=notice(
                        f"❌ I couldn't find **{query}**. Try \`Artist - Title\` or send a direct music link."
                    )
                )
                return

            await self.post_song(
                ctx.channel,
                "Manual Request",
                track_url,
                title or query,
                artist,
                ctx.author,
                ctx.guild.id if ctx.guild else 0,
                ctx.channel.id,
                cover_url,
                preview_url,
            )
        except Exception:
            logger.exception("[music] manual song command failed for query=%r", query)
            await ctx.send(
                view=notice(
                    "❌ I couldn't post that song. Try a more complete title and artist, "
                    "or send the original link instead."
                )
            )

    @app_commands.command(name="song", description="Nominate a song by name for rating (no link needed)")
    @app_commands.describe(query="Song name, e.g. 'Artist - Title'")
    async def song_slash(self, interaction: discord.Interaction, query: str):
        await interaction.response.send_message(view=notice(f"🔎 Searching for **{query}**..."), ephemeral=True)
        try:
            title, artist, cover_url, track_url, preview_url = await search_song_metadata(self.bot.http_session, query)
            await self.post_song(interaction.channel, "Manual Request", track_url, title or query, artist, interaction.user,
                                  interaction.guild.id if interaction.guild else 0, interaction.channel.id, cover_url, preview_url)
        except Exception as e:
            logger.exception("[music] operation failed")
            try:
                await interaction.followup.send(view=notice("❌ Couldn't post that song — try again in a moment."), ephemeral=True)
            except Exception:
                logger.debug("Non-fatal exception suppressed", exc_info=True)

    @app_commands.command(name="musicleaderboard", description="Show the top rated songs in this server")
    @app_commands.describe(min_score="Only show songs with an average rating at or above this value (0-10)")
    async def music_leaderboard(self, interaction: discord.Interaction, min_score: app_commands.Range[float, 0.0, 10.0] = 0.0):
        rows = await database.get_music_leaderboard(interaction.guild.id, limit=None, min_votes=2, min_score=min_score)
        if not rows:
            msg = ("No rated songs yet — post a link or use `/song` to get started!" if min_score == 0.0
                   else f"No songs found with an average rating of **{min_score}/10** or higher.")
            await interaction.response.send_message(view=notice(msg), ephemeral=True)
            return

        await interaction.response.send_message(
            view=SongLeaderboardView(interaction.guild.id, rows, min_score=min_score)
        )

    @app_commands.command(name="removesong", description="Removes a song and its votes from the database and Spotify playlist.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.describe(song_id="The ID of the song to delete from the database")
    async def remove_song(self, interaction: discord.Interaction, song_id: int):
        await interaction.response.defer()

        try:
            song = await database.get_song_by_number(interaction.guild.id, song_id)
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

            await database.delete_song(interaction.guild.id, song["id"])

            artist_str = f" by **{artist}**" if artist else ""
            await interaction.followup.send(view=notice(f"✅ Successfully removed **{title}**{artist_str} (ID: `{song_id}`) from the database."))
        except Exception as e:
            logger.exception("[music] operation failed")
            try:
                await interaction.followup.send(view=notice("❌ Something went wrong removing that song."))
            except Exception:
                logger.debug("Non-fatal exception suppressed", exc_info=True)

    @commands.hybrid_command(name="renumbersongs", aliases=["rs"], description="Re-sequences song IDs to close gaps left by deletions, and repairs live rating buttons.")
    @commands.has_permissions(administrator=True)
    async def renumbersongs(self, ctx: commands.Context):
        view = RenumberConfirmView(ctx.author.id)
        confirm_msg = await ctx.send(view=view)

        await view.wait()
        if not view.confirmed:
            await confirm_msg.edit(view=notice("❌ Renumber cancelled."))
            return

        await confirm_msg.edit(view=notice("⏳ Renumbering songs and repairing rating messages..."))

        try:
            mapping = await database.renumber_songs(ctx.guild.id)
        except Exception as error:
            logger.exception("[music] operation failed")
            await confirm_msg.edit(view=notice("❌ Something went wrong running that command."))
            return

        repaired, failed, unchanged = 0, 0, 0
        for row in mapping:
            if row["old_number"] == row["new_number"]:
                unchanged += 1
                continue
            channel = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            if not channel or not row["message_id"]:
                failed += 1
                continue
            try:
                message = await channel.fetch_message(row["message_id"])
                song = await database.get_song(ctx.guild.id, row["id"])
                avg, count = await database.get_song_stats(ctx.guild.id, row["id"])
                if song and song.get("closed"):
                    await message.edit(view=ClosedRatingView())
                else:
                    new_view = RatingView(
                        ctx.guild.id, row["id"], row["new_number"], title=song["title"] if song else None,
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
                        preview_used=bool(song.get("preview_used")),
                    )
                    await message.edit(view=new_view)
                repaired += 1
            except Exception as error:
                logger.exception("[music] operation failed")
                failed += 1

        result_text = (
            "## ✅ Renumber Complete\n"
            f"┣ Server-local IDs checked: **{len(mapping)}**\n"
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
