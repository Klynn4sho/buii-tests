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

import functools
import secrets

import requests
from flask import Blueprint, current_app, jsonify, redirect, request, session

from core import database
from core.config import DASHBOARD_REQUIRED_PERMS
from web import bridge, oauth
from web.commands_manifest import COMMANDS, CATEGORY_LABELS

bp = Blueprint("api", __name__)


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
        return redirect("/?login_error=" + error)

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

    # Only keep what the dashboard actually needs — never store the raw
    # access/refresh token pair in the session cookie itself, only server-
    # visible identity and the pre-filtered guild list.
    manageable = [
        {"id": g["id"], "name": g["name"],
         "icon_url": (f"https://cdn.discordapp.com/icons/{g['id']}/{g['icon']}.png" if g.get("icon") else None)}
        for g in user_guilds
        if (int(g.get("permissions", 0)) & DASHBOARD_REQUIRED_PERMS)
    ]

    session["user"] = {
        "id": user["id"],
        "username": user.get("username"),
        "avatar_url": (f"https://cdn.discordapp.com/avatars/{user['id']}/{user['avatar']}.png"
                       if user.get("avatar") else None),
    }
    session["manageable_guilds"] = manageable
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
        allowed_ids = {g["id"] for g in session.get("manageable_guilds", [])}
        if guild_id not in allowed_ids:
            return jsonify({"error": "You don't have access to that server."}), 403
        try:
            present = bridge.run_on_bot(current_app.bot, bridge.get_manageable_guild_ids, [int(guild_id)])
        except bridge.BotNotReady as e:
            return jsonify({"error": str(e)}), 503
        if not present:
            return jsonify({"error": "The bot isn't in that server."}), 404
        return fn(guild_id, *args, **kwargs)
    return wrapper


# ======================================================================
# Identity
# ======================================================================

@bp.route("/api/me")
@login_required
def me():
    guild_ids = [int(g["id"]) for g in session.get("manageable_guilds", [])]
    try:
        present_ids = set(bridge.run_on_bot(current_app.bot, bridge.get_manageable_guild_ids, guild_ids))
    except bridge.BotNotReady:
        present_ids = set()  # bot still starting — show the user, guild list just comes back empty this once

    guilds = [g for g in session.get("manageable_guilds", []) if int(g["id"]) in present_ids]
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
    rows = database._raw_get_joins_in_range(int(guild_id))  # oldest -> newest
    recent = list(reversed(rows))[:max(1, min(limit, 25))]
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


# ======================================================================
# Config (mirrors /setlog, /setalertrole, /setmodrole, /setprefix,
# /setmusicchannel, /setmusicrole, /setmusiclock exactly)
# ======================================================================

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

    if "prefix" in body:
        prefix = (body["prefix"] or "")[:5]
        if not prefix:
            return jsonify({"error": "Prefix cannot be empty."}), 400
        database._raw_set_prefix(gid, prefix)
        database.guild_prefix_cache[gid] = prefix  # keep the bot's in-memory cache consistent immediately
        updated.append("prefix")

    if "log_channel_id" in body:
        database._raw_set_guild_log_channel(gid, body["log_channel_id"])
        updated.append("log_channel_id")

    if "alert_role_id" in body:
        database._raw_set_alert_role(gid, body["alert_role_id"])
        updated.append("alert_role_id")

    if "mod_role_id" in body:
        database._raw_set_mod_role(gid, body["mod_role_id"])
        updated.append("mod_role_id")

    music_kwargs = {}
    if "music_role_id" in body:
        music_kwargs["role_id"] = body["music_role_id"]
    if "music_lock_seconds" in body:
        music_kwargs["lock_time"] = body["music_lock_seconds"]
    if "music_channel_id" in body:
        music_kwargs["channel_id"] = body["music_channel_id"]
    if music_kwargs:
        database._raw_set_music_config(gid, **music_kwargs)
        updated.extend(music_kwargs.keys())

    if not updated:
        return jsonify({"error": "No recognized fields in request body."}), 400

    return jsonify({"updated": updated})


# ======================================================================
# Commands (static manifest — see web/commands_manifest.py)
# ======================================================================

@bp.route("/api/commands")
@login_required
def commands_list():
    return jsonify({"commands": COMMANDS, "categories": CATEGORY_LABELS})


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
