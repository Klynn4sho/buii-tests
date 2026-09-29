"""
Flask app factory for the dashboard. Two responsibilities: serve the
rebuilt frontend (web_static/index.html, style.css) and mount the
web/api.py blueprint's OAuth + /api/* routes.

Created in main.py via create_app(bot) — the `bot` instance is attached to
the app (app.bot) so every route in web/api.py can reach it for bridging
into live guild data via web/bridge.py.
"""

import os
from datetime import timedelta

from flask import Flask, send_from_directory

from core.config import FLASK_SECRET_KEY
from web.api import bp as api_bp

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_static")


def create_app(bot) -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
    app.bot = bot

    if not FLASK_SECRET_KEY:
        # A missing secret key would otherwise fall back to Flask's dev
        # default, which lets anyone forge a session cookie and log in as
        # any user — refuse to run with real auth exposed that way. The
        # health/static routes still work without it; only login does not.
        print("[web] WARNING: FLASK_SECRET_KEY is not set — /login will be disabled "
              "until it is. Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
        app.secret_key = os.urandom(32)  # random per-process: sessions won't survive a restart, which is fine
    else:
        app.secret_key = FLASK_SECRET_KEY

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Only force Secure in production; localhost dev over plain http
        # would otherwise silently never send the cookie back.
        SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )

    app.register_blueprint(api_bp)

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/health")
    def plain_health():
        # Kept separate from /api/health: this one has no auth and no bot
        # bridge call at all, so a host's uptime pinger always gets a fast
        # 200 even if the bot itself is mid-reconnect.
        return "Bot is alive, tracking, and rendering dynamic rating cards!"

    return app
