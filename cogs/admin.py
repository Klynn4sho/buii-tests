"""
The bot's real command list, for the dashboard's Commands page. Kept as a
small static manifest rather than introspected from bot.commands at request
time, since the command set is fixed in code (not per-guild configurable —
there is no disable-a-command-per-guild feature in this bot), so there's
nothing live to bridge into the bot's loop for. If that changes later (a
real per-guild command-toggle table), this becomes the seed data for it.

Keep this in sync with the category grouping in views/help_views.py
(CATEGORIES) by hand; there are few enough commands that automatic sync
isn't worth the complexity. (The in-Discord /help menu, unlike this file,
is built live from the bot, so it never needs manual updating.)
"""

COMMANDS = [
    # --- Music ---
    {"name": "song", "category": "music", "description": "Nominate a song by name for rating (no link needed)."},
    {"name": "musicleaderboard", "category": "music", "description": "Show the top rated songs, optionally filtered by minimum score."},
    {"name": "removesong", "category": "music", "description": "Remove a song and its votes from the database and playlist."},
    {"name": "closevoting", "category": "music", "description": "Freeze a song's score so no new votes can be cast."},
    {"name": "myratings", "category": "music", "description": "View your personal music rating statistics and top picks."},
    {"name": "songratings", "category": "music", "description": "Check individual member ratings for a specific song ID."},
    {"name": "renumbersongs", "category": "music", "description": "Re-sequence song IDs to close gaps left by deletions."},
    {"name": "setmusicchannel", "category": "music", "description": "Restrict music link detection to a specific channel."},
    {"name": "setmusiclock", "category": "music", "description": "Set channel lock duration after a song is posted."},
    {"name": "setmusicrole", "category": "music", "description": "Select a role to ping when a new song is posted."},
    {"name": "synctoplaylist", "category": "music", "description": "Manually add a song to the Spotify playlist."},
    # --- Growth ---
    {"name": "leaderboard", "category": "growth", "description": "Top inviters ranked by recorded join history."},
    {"name": "invites", "category": "growth", "description": "View a member's invite history and stats."},
    {"name": "statspanel", "category": "growth", "description": "Deploy an auto-refreshing live server growth dashboard."},
    {"name": "graph", "category": "growth", "description": "Display daily growth trend charts."},
    {"name": "testjoin", "category": "growth", "description": "Preview the join-risk alert layout."},
    {"name": "setlog", "category": "growth", "description": "Lock join notifications to the current channel."},
    {"name": "setalertrole", "category": "growth", "description": "Set the role pinged for extreme-risk joins."},
    {"name": "setmodrole", "category": "growth", "description": "Set a role that can manage bot config without Manage Server."},
    {"name": "setprefix", "category": "growth", "description": "Change the server's text-command prefix."},
    # --- Staff directory ---
    {"name": "hierarchy", "category": "staff", "description": "Render a staff directory image of every moderation role."},
    # --- Admin ---
    {"name": "help", "category": "admin", "description": "Browse every command by category, with search."},
    {"name": "sync", "category": "admin", "description": "Push slash commands to this server or globally."},
]

CATEGORY_LABELS = {
    "music": "Music",
    "growth": "Invites & Growth",
    "staff": "Staff Directory",
    "admin": "Admin",
}
