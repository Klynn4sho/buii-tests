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
    {"name": "song", "aliases": ["s"], "category": "music", "description": "Nominate a song by name for rating (no link needed)."},
    {"name": "musicleaderboard", "category": "music", "description": "Show the top rated songs, optionally filtered by minimum score."},
    {"name": "removesong", "category": "music", "description": "Remove a song and its votes from the database and playlist."},
    {"name": "closevoting", "aliases": ["cv"], "category": "music", "description": "Freeze a song's score so no new votes can be cast."},
    {"name": "musicprofile", "aliases": ["mp", "myratings"], "category": "music", "description": "View your personal music rating statistics and top picks."},
    {"name": "spotify", "aliases": ["sinfo", "spotify_status", "spotifyinfo"], "category": "music", "description": "Show Spotify integration status without exposing secrets."},
    {"name": "songratings", "aliases": ["sr"], "category": "music", "description": "Check individual member ratings for a specific song ID."},
    {"name": "memberratings", "aliases": ["mr"], "category": "music", "description": "Show every rating submitted by a member."},
    {"name": "voteblacklist", "aliases": ["vbl"], "category": "music", "description": "Block or unblock a member from submitting music ratings."},
    {"name": "renumbersongs", "aliases": ["rs"], "category": "music", "description": "Re-sequence song IDs to close gaps left by deletions."},
    {"name": "setmusicchannel", "aliases": ["smc"], "category": "music", "description": "Restrict music link detection to a specific channel."},
    {"name": "setmusiclock", "aliases": ["sml"], "category": "music", "description": "Set channel lock duration after a song is posted."},
    {"name": "setmusicrole", "aliases": ["smr"], "category": "music", "description": "Select a role to ping when a new song is posted."},
    {"name": "synctoplaylist", "aliases": ["stp", "syncsong"], "category": "music", "description": "Manually add a song to the Spotify playlist."},
    {"name": "rebuildplaylist", "aliases": ["rpl"], "category": "music", "description": "Clear and rebuild the Spotify playlist from qualifying rated songs."},
    # --- Growth ---
    {"name": "analytics", "aliases": ["an"], "category": "growth", "description": "Show reliable growth, retention, risk, and inviter analytics."},
    {"name": "leaderboard", "aliases": ["lb", "top"], "category": "growth", "description": "Top inviters ranked by recorded join history."},
    {"name": "userinfo", "aliases": ["ui", "whois"], "category": "growth", "description": "View saved profile and membership information for a user."},
    {"name": "memberhistory", "aliases": ["mh", "joinhistory"], "category": "growth", "description": "Show saved joins and leaves for a member."},
    {"name": "invites", "aliases": ["iv"], "category": "growth", "description": "View a member's invite history and stats."},
    {"name": "inviteinfo", "aliases": ["ii", "invitecode", "codeinfo"], "category": "growth", "description": "Inspect recorded uses, attribution, and invitees for an invite code."},
    {"name": "statspanel", "aliases": ["stats", "sp"], "category": "growth", "description": "Deploy an auto-refreshing live server growth dashboard."},
    {"name": "graph", "aliases": ["gg", "g"], "category": "growth", "description": "Display daily growth trend charts."},
    {"name": "testjoin", "aliases": ["tj"], "category": "growth", "description": "Preview the join-risk alert layout."},
    {"name": "setlog", "aliases": ["sl"], "category": "growth", "description": "Lock join notifications to the current channel."},
    {"name": "setalertrole", "aliases": ["sar"], "category": "growth", "description": "Set the role pinged for extreme-risk joins."},
    {"name": "setmodrole", "aliases": ["smrole"], "category": "growth", "description": "Set a role that can manage bot config without Manage Server."},
    {"name": "setprefix", "aliases": ["prefix", "pfx"], "category": "growth", "description": "Change the server's text-command prefix."},
    # --- Staff directory ---
    {"name": "hierarchy", "aliases": ["hr"], "category": "staff", "description": "Render a staff directory image of every moderation role."},
    # --- Server information ---
    {"name": "serverinfo", "aliases": ["si", "server", "guildinfo"], "category": "server", "description": "Show server identity, member totals, channels, and verification details."},
    # --- Admin ---
    {"name": "help", "aliases": ["commands", "h"], "category": "admin", "description": "Browse every command by category, with search."},
    {"name": "diagnostics", "aliases": ["diag"], "category": "admin", "description": "Show private bot health diagnostics for the bypass user."},
    {"name": "storage", "aliases": ["db", "dbstatus"], "category": "admin", "description": "Show private persistent-storage status for the bypass user."},
    {"name": "sync", "aliases": ["sy"], "category": "admin", "description": "Push slash commands to this server or globally."},
]

CATEGORY_LABELS = {
    "music": "Music",
    "growth": "Invites & Growth",
    "staff": "Staff Directory",
    "server": "Server Information",
    "admin": "Admin",
}
