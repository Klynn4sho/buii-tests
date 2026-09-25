# Buii — cogs edition

The original single-file bot, split into a `cogs/`-based project. Behavior
is unchanged; only the organization is different.

## Layout

```
main.py                  Process entrypoint only: bot subclass, extension
                          loading, Flask keep-alive server, signal handling
                          and graceful shutdown. No commands/events live here
                          except the two that must be global (on_message's
                          process_commands dispatch, and the error handlers).

core/
  config.py               Env vars + color/graph constants. Every other
                           module imports from here instead of os.environ.
  checks.py                has_mod_permission() — shared by both feature cogs.
  database.py               Postgres pool + every raw/async query pair, for
                           both the invite-tracking tables (joins/leaves/
                           config) and the music tables (songs/ratings).
                           Both feature domains share one pool, so this
                           stays one module rather than being split further.
  helpers.py                 Formatting/UI helpers: progress bars, embed
                           footers, format_elapsed(), the dashboard embed
                           builder, the matplotlib join-growth graph, and
                           the full Pillow dynamic music-card renderer.
  music_utils.py              Music-link regexes, oEmbed/OG metadata
                           fetching, the Spotify/Deezer/iTunes search
                           cascade, genre lookup, and Spotify playlist
                           sync/remove. Pure networking, no Discord/DB code.

cogs/
  growth.py    GrowthCog    Invite tracking: on_member_join/remove,
                           on_guild_join/invite_create/invite_delete, the
                           hourly dashboard-panel refresh loop, and the
                           leaderboard/invites/statspanel/graph/testjoin/
                           setlog/setalertrole/setmodrole/setprefix commands.
  music.py     MusicCog     Music rating: on_message link detection,
                           post_song/temp_lock_channel, the 10-minute
                           auto-close sweep for the 12-hour rating window,
                           and every song-related command (song,
                           musicleaderboard, removesong, closevoting,
                           songratings, synctoplaylist, myratings,
                           setmusicchannel/role/lock, renumbersongs).
  admin.py     AdminCog     /help (auto-grouped by cog) and /sync.

views/
  growth_views.py    DashboardView, GraphRangeSelect, GraphView.
  music_views.py       RatingButton, RatingView, ClosedRatingView,
                     RenumberConfirmView.
```

## Why it's split this way

- **`core/database.py` stays one file.** The invite-tracking and music
  tables share the same connection pool and the same Postgres instance;
  splitting the DB layer by feature would mean two modules independently
  managing (or fighting over) the same `db_pool` global. Keeping it as one
  module with clearly separated sections is simpler than the alternative.
- **Views live outside the cogs.** `RatingView`/`DashboardView`/etc. need to
  be constructible before any cog-specific state exists (they're registered
  as persistent views in `cog_load()`), and both cogs' commands construct
  them directly (e.g. `music.py` builds a fresh `RatingView` after
  `/renumbersongs`). Putting them in their own package avoids circular
  imports between the two cogs.
- **`core/helpers.py` is intentionally large.** The Pillow card renderer is
  ~200 lines of tightly coupled drawing code (font cache, dominant-color
  extraction, waveform, genre chip, rating gauge) that only makes sense as
  one function with private helpers underneath it — splitting it further
  would just scatter state (like `_FONT_CACHE`) across files for no benefit.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in the values, then export them or use a
                        # process manager / platform that loads .env for you
python main.py
```

## Adding a new cog

1. Create `cogs/your_feature.py` with a `class YourCog(commands.Cog,
   name="YourCog")` and an `async def setup(bot): await
   bot.add_cog(YourCog(bot))` at the bottom.
2. Add `"cogs.your_feature"` to the `EXTENSIONS` tuple in `main.py`.
3. If it should show up in `/help` under its own category, add an entry to
   `COG_DISPLAY` in `cogs/admin.py` — otherwise its commands land under
   "📦 Other" automatically rather than disappearing.
