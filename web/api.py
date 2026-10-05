"""
Flask blueprint for the dashboard: Discord OAuth2 login, and the /api/*
routes it calls after logging in.

Every route here reuses the exact same data the bot's cogs already use —
core/database.py's synchronous `_raw_*` functions for anything Postgres-
backed (joins, leaderboards, songs, guild config), and web/bridge.py for
anything that has to come from a live discord.py object (roles, channels,
member counts). Nothing here is a second, parallel data source: a config
change made from the dashboard calls the identical `_raw_set_*` function
`/setlog`, `/setmusicrole`, etc. already call, so the bot's commands and
the dashboard can never drift out of sync with each other.
"""

import csv
import functools
import io
import logging
import re
import secrets
from urllib.parse import quote

import requests
from flask import Blueprint, Response, current_app, jsonify, redirect, request, session

from core import database
from core.config import DASHBOARD_REQUIRED_PERMS, SPOTIFY_PLAYLIST_ID
from web import bridge, oauth
from web.commands_manifest import COMMANDS, CATEGORY_LABELS

bp = Blueprint("api", __name__)
logger = logging.getLogger(__name__)
SNOWFLAKE_RE = re.compile(r"^[0-9]{17,20}$")


PLAYLIST_SYNC_COMMANDS = frozenset({"synctoplaylist", "rebuildplaylist"})


def spotify_premium_enabled():
    return bool(getattr(current_app.bot, "spotify_premium", False))


def _parse_optional_snowflake(value, field_name):
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not SNOWFLAKE_RE.fullmatch(text):
        raise ValueError(f"{field_name} must be a valid Discord ID or null.")
    return int(text)


def _parse_lock_seconds(value):
    if value is None or value == "":
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("music_lock_seconds must be a whole number.") from exc
    if not 0 <= value <= 86400:
        raise ValueError("music_lock_seconds must be between 0 and 86400 seconds.")
    return value


# ======================================================================
# Auth
# ======================================================================

@bp.route("/login")
def login():
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    try:
        return redirect(oauth.build_authorize_url(state))
    except oauth.OAuthNotConfigured as e:
        return jsonify({"error": str(e)}), 503


@bp.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return redirect("/?login_error=" + quote(error, safe=""))

    state = request.args.get("state")
    if not state or state != session.pop("oauth_state", None):
        return jsonify({"error": "Invalid or expired login attempt — please try logging in again."}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing authorization code."}), 400

    try:
        token_data = oauth.exchange_code(code)
        user = oauth.fetch_user(token_data["access_token"])
        user_guilds = oauth.fetch_user_guilds(token_data["access_token"])
    except requests.HTTPError as e:
        return jsonify({"error": f"Discord rejected the login: {e}"}), 502
    except oauth.OAuthNotConfigured as e:
        return jsonify({"error": str(e)}), 503

    # Do not store the OAuth guild list in Flask's signed cookie. Large
    # guild lists can exceed browser cookie limits, making a successful
    # callback look like a login that never sticks. The guild list is rebuilt
    # from the bot's live cache in /api/me instead.
    oauth_manageable = [
        g for g in user_guilds
        if (int(g.get("permissions", 0)) & DASHBOARD_REQUIRED_PERMS)
    ]
    if not oauth_manageable:
        return redirect("/?login_error=" + quote(
            "No manageable servers were returned by Discord", safe=""
        ))

    # Rotate the session after successful authentication and discard the
    # one-time OAuth state.
    session.clear()
    session["user"] = {
        "id": user["id"],
        "username": user.get("username"),
        "avatar_url": (f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png"
                       if user.get("avatar") else None),
    }
    session.permanent = True

    return redirect("/")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect("/")


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Not logged in."}), 401
        return fn(*args, **kwargs)
    return wrapper


def guild_access_required(fn):
    """Checks the user is logged in, has Manage Guild/Administrator on this
    specific guild (per the permissions snapshot taken at login), AND the
    bot is actually in that guild — the same bar has_mod_permission()
    enforces for the Discord-side config commands."""
    @functools.wraps(fn)
    def wrapper(guild_id, *args, **kwargs):
        if "user" not in session:
            return jsonify({"error": "Not logged in."}), 401
        if not SNOWFLAKE_RE.fullmatch(str(guild_id)):
            return jsonify({"error": "Invalid server ID."}), 400
        try:
            gid = int(guild_id)
            user_id = int(session["user"]["id"])
            present = bridge.run_on_bot(current_app.bot, bridge.get_manageable_guild_ids, [gid])
            if not present:
                return jsonify({"error": "The bot isn't in that server."}), 404
            has_access = bridge.run_on_bot(current_app.bot, bridge.verify_guild_manager, gid, user_id)
        except bridge.BotNotReady as e:
            return jsonify({"error": str(e)}), 503
        except TimeoutError as e:
            return jsonify({"error": str(e)}), 503
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid server ID."}), 400
        if not has_access:
            return jsonify({"error": "You no longer have Manage Server permission in that server."}), 403
        return fn(guild_id, *args, **kwargs)
    return wrapper


# ======================================================================
# Identity
# ======================================================================

@bp.route("/api/me")
@login_required
def me():
    try:
        guilds = bridge.run_on_bot(
            current_app.bot,
            bridge.get_user_manageable_guilds,
            int(session["user"]["id"]),
        )
    except bridge.BotNotReady:
        guilds = []
    except TimeoutError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"user": session["user"], "guilds": guilds})


# ======================================================================
# Guild overview / growth / leaderboards
# ======================================================================

@bp.route("/api/guilds/<guild_id>/overview")
@guild_access_required
def guild_overview(guild_id):
    gid = int(guild_id)
    total, joins_24h, risk_count, top_codes = database._raw_compile_dashboard_stats(gid)
    summary = bridge.run_on_bot(current_app.bot, bridge.get_guild_summary, gid)
    health = bridge.run_on_bot(current_app.bot, bridge.get_bot_health)
    return jsonify({
        "guild": summary,
        "total_joins": total,
        "joins_24h": joins_24h,
        "extreme_risk_count": risk_count,
        "top_invite_codes": [{"code": c, "uses": n} for c, n in top_codes],
        "bot_latency_ms": health["latency_ms"],
    })


@bp.route("/api/guilds/<guild_id>/growth")
@guild_access_required
def guild_growth(guild_id):
    days = request.args.get("days", default=30, type=int)
    days = max(1, min(days, 90))
    series = database._raw_get_daily_join_counts(int(guild_id), days=days)
    return jsonify({"days": [{"date": d.isoformat(), "joins": c} for d, c in series]})


@bp.route("/api/guilds/<guild_id>/analytics")
@guild_access_required
def guild_analytics(guild_id):
    data = database._raw_get_analytics_insights(int(guild_id))
    growth = data["growth"] or {}
    retention = data["retention"] or {}
    returning = data["returning"] or {}
    queue = data["queue"] or {}
    ratings = data["ratings"] or {}
    telemetry = database._raw_get_dashboard_telemetry(int(guild_id))
    sync_summary = database._raw_get_music_sync_summary(int(guild_id))
    command_totals = telemetry["commands"] or {}
    sync = telemetry["sync"] or {}
    return jsonify({
        "growth": {
            "current_joins": int(growth.get("current_joins") or 0),
            "previous_joins": int(growth.get("previous_joins") or 0),
            "tracked_invites": int(growth.get("tracked_invites") or 0),
        },
        "retention": {
            "mature_joins": int(growth.get("mature_joins") or 0),
            "retained_joins": int(retention.get("retained_joins") or 0),
            "returning_members": int(returning.get("returning_members") or 0),
        },
        "risk": {
            "new_account": int(growth.get("new_account_risk") or 0),
            "ambiguous_invites": int(growth.get("ambiguous_invites") or 0),
        },
        "queue": {
            "open_songs": int(queue.get("open_songs") or 0),
            "awaiting_preview": int(queue.get("awaiting_preview") or 0),
        },
        "ratings": {
            "low": int(ratings.get("low") or 0),
            "below_average": int(ratings.get("below_average") or 0),
            "average": int(ratings.get("average") or 0),
            "good": int(ratings.get("good") or 0),
            "excellent": int(ratings.get("excellent") or 0),
            "total": int(ratings.get("total") or 0),
        },
        "sync_summary": sync_summary if spotify_premium_enabled() else {},
        "spotify_premium": spotify_premium_enabled(),
        "telemetry": {
            "commands_today": int(command_totals.get("commands_today") or 0),
            "command_errors": int(command_totals.get("command_errors") or 0),
            "most_used_command": telemetry["most_used"].get("name") if telemetry["most_used"] else None,
            "sync_failures": int(sync.get("sync_failures") or 0),
            "sync_attempts": int(sync.get("sync_attempts") or 0),
            "admin_events": [
                {"name": row["name"], "details": row["details"], "created_at": row["created_at"].isoformat()}
                for row in telemetry["admin_events"]
            ],
            "providers": [
                {"source": row["source"], "songs": int(row["songs"]), "synced": int(row["synced"] or 0)}
                for row in telemetry["providers"]
            ],
        },
    })


@bp.route("/api/guilds/<guild_id>/leaderboard")
@guild_access_required
def guild_leaderboard(guild_id):
    rows = database._raw_get_leaderboard(int(guild_id), limit=10)
    return jsonify({"leaderboard": [{"inviter": name, "joins": count} for name, count in rows]})


@bp.route("/api/guilds/<guild_id>/recent-joins")
@guild_access_required
def guild_recent_joins(guild_id):
    """Backs both the notification dropdown and the home page's activity
    feed — real join records instead of a fabricated event/activity log,
    since Buii doesn't track a general activity feed (no AutoMod, no
    scheduled messages, no reaction roles: those aren't real features
    here, so there's nothing honest to show for them)."""
    limit = request.args.get("limit", default=8, type=int)
    limit = max(1, min(limit, 25))
    recent = database._raw_get_joins_in_range(int(guild_id), limit=limit, newest_first=True)
    return jsonify({"joins": [
        {"user_id": r[0], "user_name": r[1], "inviter_name": r[2],
         "invite_code": r[3], "join_date": r[4], "account_age_days": r[5]}
        for r in recent
    ]})


@bp.route("/api/guilds/<guild_id>/music-leaderboard")
@guild_access_required
def guild_music_leaderboard(guild_id):
    min_score = request.args.get("min_score", default=0.0, type=float)
    rows = database._raw_get_music_leaderboard(int(guild_id), limit=10, min_votes=2, min_score=min_score)
    return jsonify({"songs": [
        {"id": r["id"], "title": r["title"], "artist": r["artist"],
         "avg_score": float(r["avg_score"]), "votes": r["votes"],
         "posted_at": r["created_at"].isoformat()}
        for r in rows
    ]})


@bp.route("/api/guilds/<guild_id>/music-log")
@guild_access_required
def guild_music_log(guild_id):
    limit = request.args.get("limit", default=25, type=int)
    limit = max(1, min(limit, 50))
    rows = database._raw_get_music_log(int(guild_id), limit=limit)
    return jsonify({"songs": [
        {"id": r["id"], "song_number": r["song_number"], "title": r["title"],
         "artist": r["artist"], "source": r["source"], "url": r["url"],
         "requested_by": r["requested_by_name"], "created_at": r["created_at"].isoformat(),
         "status": "added to playlist" if r["synced"] else "closed" if r["closed"] else "open"}
        for r in rows
    ]})


# ======================================================================
# Config (mirrors /setlog, /setalertrole, /setmodrole, /setprefix,
# /setmusicchannel, /setmusicrole, /setmusiclock exactly)
# ======================================================================

@bp.route("/api/guilds/<guild_id>/music-insights")
@guild_access_required
def guild_music_insights(guild_id):
    data = database._raw_get_music_insights(int(guild_id), limit=5)
    sync = data["sync"] or {}
    return jsonify({
        "requesters": [
            {"name": r["requester"], "songs": int(r["songs"]),
             "last_requested": r["last_requested"].isoformat() if r["last_requested"] else None}
            for r in data["requesters"]
        ],
        "spotify_premium": spotify_premium_enabled(),
        "sync": ({
            "playlist_configured": bool(SPOTIFY_PLAYLIST_ID),
            "total": int(sync.get("total") or 0),
            "synced": int(sync.get("synced") or 0),
            "pending": int(sync.get("pending") or 0),
            "last_added": sync.get("last_added").isoformat() if sync.get("last_added") else None,
        } if spotify_premium_enabled() else {}),
    })


@bp.route("/api/guilds/<guild_id>/exports/<kind>")
@guild_access_required
def guild_export(guild_id, kind):
    if kind not in {"joins", "songs", "activity"}:
        return jsonify({"error": "Unknown export type"}), 404
    gid = int(guild_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if kind == "joins":
        rows = database._raw_get_joins_in_range(gid, limit=10000, newest_first=False)
        writer.writerow(["user_id", "user_name", "inviter_name", "invite_code", "join_date", "account_age_days"])
        writer.writerows(rows)
    elif kind == "songs":
        rows = database._raw_get_music_log(gid, limit=10000)
        writer.writerow(["id", "song_number", "title", "artist", "source", "requested_by", "created_at", "synced", "closed"])
        for row in rows:
            writer.writerow([row["id"], row["song_number"], row["title"], row["artist"], row["source"],
                             row["requested_by_name"], row["created_at"], row["synced"], row["closed"]])
    else:
        rows = database._raw_get_dashboard_telemetry(gid)["admin_events"]
        writer.writerow(["event_type", "name", "success", "details", "created_at"])
        for row in rows:
            writer.writerow([row["event_type"], row["name"], row["success"], row["details"], row["created_at"]])
    response = Response(buffer.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=buii-{kind}-{gid}.csv"
    return response


@bp.route("/api/guilds/<guild_id>/config")
@guild_access_required
def get_config(guild_id):
    gid = int(guild_id)
    log_channel_id = database._raw_get_guild_log_channel(gid)
    alert_role_id = database._raw_get_alert_role(gid)
    mod_role_id = database._raw_get_mod_role(gid)
    prefix = database._raw_get_prefix(gid) or "b,"
    music_role_id, music_lock_time, music_channel_id = database._raw_get_music_config(gid)

    roles = bridge.run_on_bot(current_app.bot, bridge.get_guild_roles, gid) or []
    channels = bridge.run_on_bot(current_app.bot, bridge.get_guild_text_channels, gid) or []
    role_names = {r["id"]: r["name"] for r in roles}
    channel_names = {c["id"]: c["name"] for c in channels}

    return jsonify({
        "prefix": prefix,
        "log_channel": {"id": str(log_channel_id), "name": channel_names.get(str(log_channel_id))} if log_channel_id else None,
        "alert_role": {"id": str(alert_role_id), "name": role_names.get(str(alert_role_id))} if alert_role_id else None,
        "mod_role": {"id": str(mod_role_id), "name": role_names.get(str(mod_role_id))} if mod_role_id else None,
        "music_role": {"id": str(music_role_id), "name": role_names.get(str(music_role_id))} if music_role_id else None,
        "music_lock_seconds": music_lock_time or 0,
        "music_channel": {"id": str(music_channel_id), "name": channel_names.get(str(music_channel_id))} if music_channel_id else None,
        "roles": roles,
        "channels": channels,
    })


@bp.route("/api/guilds/<guild_id>/config", methods=["PATCH"])
@guild_access_required
def patch_config(guild_id):
    gid = int(guild_id)
    body = request.get_json(silent=True) or {}
    updated = []

    try:
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")

        prefix = None
        if "prefix" in body:
            prefix = str(body["prefix"] or "")[:5]
            if not prefix or "\n" in prefix or "\r" in prefix:
                raise ValueError("Prefix must be 1–5 characters and cannot contain line breaks.")

        config_ids = {}
        for field in ("log_channel_id", "alert_role_id", "mod_role_id", "music_role_id", "music_channel_id"):
            if field in body:
                config_ids[field] = _parse_optional_snowflake(body[field], field)

        lock_seconds = _parse_lock_seconds(body["music_lock_seconds"]) if "music_lock_seconds" in body else None
        if "music_lock_seconds" in body:
            config_ids["music_lock_seconds"] = lock_seconds

        if config_ids:
            ok, error = bridge.run_on_bot(current_app.bot, bridge.validate_guild_config_ids, gid, config_ids)
            if not ok:
                return jsonify({"error": error}), 400

        # Validate everything first so a bad role/channel cannot leave the
        # request half-applied. Only after validation do we touch PostgreSQL.
        if "prefix" in body:
            database._raw_set_prefix(gid, prefix)
            database.guild_prefix_cache[gid] = prefix
            updated.append("prefix")

        for field in ("log_channel_id", "alert_role_id", "mod_role_id"):
            if field in config_ids:
                setter = {
                    "log_channel_id": database._raw_set_guild_log_channel,
                    "alert_role_id": database._raw_set_alert_role,
                    "mod_role_id": database._raw_set_mod_role,
                }[field]
                setter(gid, config_ids[field])
                updated.append(field)

        music_kwargs = {}
        if "music_role_id" in config_ids:
            music_kwargs["role_id"] = config_ids["music_role_id"]
        if "music_lock_seconds" in config_ids:
            music_kwargs["lock_time"] = config_ids["music_lock_seconds"]
        if "music_channel_id" in config_ids:
            music_kwargs["channel_id"] = config_ids["music_channel_id"]
        if music_kwargs:
            database._raw_set_music_config(gid, **music_kwargs)
            updated.extend(field for field in ("music_role_id", "music_lock_seconds", "music_channel_id") if field in config_ids)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (bridge.BotNotReady, TimeoutError) as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception:
        logger.exception("Dashboard config update failed for guild %s", guild_id)
        return jsonify({"error": "Could not save that configuration. Please try again."}), 500

    if not updated:
        return jsonify({"error": "No recognized fields in request body."}), 400

    actor = (session.get("user") or {}).get("username", "dashboard user")
    for field in updated:
        database._raw_record_dashboard_event(
            gid, "admin", field, True, details=f"{actor} changed {field}"
        )
    return jsonify({"updated": updated})


# ======================================================================
# Commands (static manifest — see web/commands_manifest.py)
# ======================================================================

@bp.route("/api/commands")
@login_required
def commands_list():
    commands = [
        command for command in COMMANDS
        if spotify_premium_enabled() or command["name"] not in PLAYLIST_SYNC_COMMANDS
    ]
    return jsonify({"commands": commands, "categories": CATEGORY_LABELS})


# ======================================================================
# Health (no auth — used for an unauthenticated status check)
# ======================================================================

@bp.route("/api/health")
def health():
    try:
        h = bridge.run_on_bot(current_app.bot, bridge.get_bot_health)
    except bridge.BotNotReady:
        return jsonify({"connected": False, "starting": True})
    return jsonify(h)
