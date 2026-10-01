# Buii — cogs edition

The original single-file bot, split into a `cogs/`-based project, then
migrated from embeds to Discord's Components V2 UI system.

## UI: Components V2, not embeds

Every response in this bot is a `discord.ui.LayoutView` — nothing sends
`discord.Embed` anywhere. This requires **discord.py >= 2.6.0** (Components
V2 support; current stable is 2.7.x), pinned in `requirements.txt`.

The design rule, defined and explained in `core/components.py`: **every
response is wrapped in a `ui.Container`** (the bordered "boxed" panel), and
the container has **no accent color by default**. An accent color is set
explicitly, and only for invite- and song-related content:

- **Invites:** the join alert and `/testjoin` (red/yellow/green by risk
  tier), `/leaderboard`, `/invites`, the live dashboard panel, the graph
  range picker.
- **Songs:** the rating card (score color, or the cover art's dominant
  color before the first vote), the closed-voting replica, the song
  leaderboard, `/songratings`, `/closevoting`, `/myratings`, the duplicate
  notice, the manual-sync success message. The renumber confirm prompt
  keeps a danger-red accent as a destructive-action warning.

Everything else — help, config confirmations, errors, admin output, the
staff directory — is a plain, un-accented container.

`core/components.py` holds the reusable layout classes so most commands
don't need a custom `LayoutView` subclass:
- `Layout(*items, accent=None)` — the base: any number of `TextDisplay`/
  `Section`/`Separator`/`MediaGallery` items inside one container.
- `SimpleLayout(text, accent=None)` — one text block in a container.
- `SimpleImageLayout(text, file, accent=None)` — one text block + one image
  in a container (used by `/hierarchy`).
- `notice(text)` — a boxed, un-accented one-liner. Use it for every short
  reply (errors, "not found", ephemeral acknowledgements) instead of a bare
  string, which can't be a container.

A message sent with Components V2 can **never** combine `content=`/
`embeds=` with a `view=`. Anywhere the old code relied on a `content=`
mention to ping a user/role (song posts, duplicate-detection notices), the
mention now lives as plain text inside a `TextDisplay` — Discord still
delivers the ping from there, just not from the `content` field. The one
deliberate exception is the dashboard's CSV export, which is a raw file
delivery and stays a plain attachment message.

## Layout

```
main.py                  Process entrypoint only: bot subclass, extension
                          loading, Flask keep-alive server, signal handling
                          and graceful shutdown. No commands/events live here
                          except the two that must be global (on_message's
                          process_commands dispatch, and the error handlers,
                          which reply with notice() containers).

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
                           Layout, SimpleLayout, SimpleImageLayout, notice(),
                           and footer_line() (the "-# small text" caption
                           that replaces embed footers). See the CV2 section
                           above for the container/accent rule this file
                           documents and every cog/view follows.
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
  admin.py     AdminCog     /help (hybrid: prefix + slash, alias `h`),
                           which opens the HelpView menu, and /sync.

views/
  growth_views.py    DashboardView, GraphRangeSelect, GraphView.
  music_views.py       RatingButton, RatingView, ClosedRatingView,
                     RenumberConfirmView.
  help_views.py        HelpView (home / category / search pages, category
                     select, Home/prev/next/Search/Close buttons, Dashboard/
                     Privacy/Terms link buttons), SearchModal, and the
                     CATEGORIES table that groups commands by cog name.
```

## The help menu

`/help` (or `<prefix>help` / `<prefix>h`) opens a single container, built
section by section: a home page (greeting, how to use the menu, categories
with command counts, dashboard setup), per-category pages (6 commands per
page), and a search page fed by a modal. Only the person who ran it can use
it, and its controls disable after 3 minutes.

The command list is built live from the bot (prefix commands plus the slash
tree, de-duplicated by name) and grouped by cog via `CATEGORIES` in
`views/help_views.py`. A new command shows up automatically; a new cog
shows up under "📦 Other" until you add a `Category(...)` entry for it
there. The Dashboard/Privacy/Terms link targets are constants at the top of
the same file.

## Why it's split this way

- **`core/database.py` stays one file.** The invite-tracking and music
  tables share the same connection pool and the same Postgres instance;
  splitting the DB layer by feature would mean two modules independently
  managing (or fighting over) the same `db_pool` global. Keeping it as one
  module with clearly separated sections is simpler than the alternative.
- **Views live outside the cogs.** `RatingView`/`DashboardView`/`HelpView`
  etc. need to be constructible before any cog-specific state exists (the
  rating and dashboard views are registered as persistent views in
  `cog_load()`), and more than one cog may construct them (e.g. `music.py`
  builds a fresh `RatingView` after `/renumbersongs`). Putting them in
  their own package avoids circular imports between the cogs.
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
3. If it should show up in `/help` under its own category, add a
   `Category(key, emoji, label, "YourCog", blurb)` entry to `CATEGORIES` in
   `views/help_views.py` — otherwise its commands land under "📦 Other"
   automatically rather than disappearing.
4. Wrap its responses in containers via `core/components.py` (use `notice()`
   for short replies), and pass an accent color only if the content is
   invite- or song-related.
