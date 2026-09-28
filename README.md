# Buii — cogs edition

The original single-file bot, split into a `cogs/`-based project, then
migrated from embeds to Discord's Components V2 UI system.

## UI: Components V2, not embeds

Every response in this bot is a `discord.ui.LayoutView` — nothing sends
`discord.Embed` anywhere. This requires **discord.py >= 2.6.0** (Components
V2 support; current stable is 2.7.x), pinned in `requirements.txt`.

The design rule, defined and explained in `core/components.py`:
`ui.Container` (the accent-colored, bordered "boxed" look — visually the
same thing an embed's colored left stripe did) is used **only** where a
message actually bundles interactive buttons/selects with their
explanatory content — the dashboard panel, the graph range picker, the
rating card's 1-10 buttons, the renumber confirm/cancel prompt. Everything
purely informational — confirmations, leaderboards, stat summaries,
inspection results, the join-risk alert — is borderless `TextDisplay` /
`Section` / `MediaGallery` directly on the `LayoutView`, with no
`Container` at all. Risk/status color that a border used to carry (danger
red, success green, risk-tier) survives through emoji (🚨/⚠️/🟢, 🟥/🟨/🟩)
instead, so nothing is silently lost by going borderless.

`core/components.py` also holds three reusable layout classes so most
commands don't need a custom `LayoutView` subclass:
- `SimpleLayout(text)` — one borderless text block.
- `SimpleImageLayout(text, file)` — one text block + one image, borderless
  (used by `/graph`'s underlying image and `/hierarchy`).
- `Layout(*items)` — borderless, for anything needing more than one
  `TextDisplay`/`Section`/`Separator` (join alerts, `/invites`, `/help`).

A message sent with Components V2 can **never** combine `content=`/
`embeds=` with a `view=`. Anywhere the old code relied on a `content=`
mention to ping a user/role (song posts, duplicate-detection notices), the
mention now lives as plain text inside a `TextDisplay` — Discord still
delivers the ping from there, just not from the `content` field.

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
  helpers.py                 Formatting/UI helpers: progress bars,
                           format_elapsed(), build_dashboard_content_items()
                           (the dashboard's CV2 content, consumed by
                           DashboardView), the matplotlib join-growth graph,
                           and the full Pillow dynamic music-card renderer
                           (including score_color(), used by the rating
                           views to pick the Container's accent color).
  components.py               Shared Components V2 building blocks:
                           SimpleLayout, SimpleImageLayout, Layout, and
                           footer_line() (the "-# small text" caption that
                           replaces embed footers). See the CV2 section
                           above for the border/no-border design rule this
                           file documents and every cog/view follows.
  music_utils.py              Music-link regexes, oEmbed/OG metadata
                           fetching, the Spotify/Deezer/iTunes search
                           cascade, genre lookup, and Spotify playlist
                           sync/remove. Pure networking, no Discord/DB code.
  hierarchy_utils.py           Staff-role detection (is_staff_role) and the
                           Pillow rendering of the /hierarchy directory
                           image. Kept separate from helpers.py the same
                           way music_utils.py is — a self-contained feature
                           unrelated to growth or music.

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
  hierarchy.py HierarchyCog Staff directory: /hierarchy (aliases /staffs,
                           /stafflist) renders a PNG of every moderation-
                           permission role, ranked by position, with member
                           avatars and vacancy status per role.
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
