"""
Flask app factory for the dashboard. Two responsibilities: serve the
rebuilt frontend (web_static/index.html, style.css) and mount the
web/api.py blueprint's OAuth + /api/* routes.

Created in main.py via create_app(bot) — the `bot` instance is attached to
the app (app.bot) so every route in web/api.py can reach it for bridging
into live guild data via web/bridge.py.
"""

import logging
import os
from datetime import timedelta

from flask import Flask, send_from_directory

from core.config import APP_ENV, FLASK_SECRET_KEY
from web.api import bp as api_bp

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web_static")


def create_app(bot) -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
    app.bot = bot

    if not FLASK_SECRET_KEY:
        # A random per-process key is safe against forged cookies, but sessions
        # will be invalidated after a restart. Production deployments should
        # always provide a stable, high-entropy FLASK_SECRET_KEY.
        logger.warning("FLASK_SECRET_KEY is not set; sessions will not survive restarts. Set a strong secret in production.")
        app.secret_key = os.urandom(32)  # random per-process: sessions won't survive a restart, which is fine
    else:
        app.secret_key = FLASK_SECRET_KEY

    app.config.update(
        # Use a dedicated cookie name/path so stale cookies from older
        # dashboard builds cannot shadow the current OAuth session.
        SESSION_COOKIE_NAME="buii_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/",
        # Only force Secure in production; localhost dev over plain http
        # would otherwise silently never send the cookie back.
        SESSION_COOKIE_SECURE=(APP_ENV == "production" or os.environ.get("FLASK_ENV") == "production"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )

    app.register_blueprint(api_bp)

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https://cdn.discordapp.com data:; font-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
            "form-action 'self' https://discord.com;"
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    @app.route("/health")
    def plain_health():
        # Kept separate from /api/health: this one has no auth and no bot
        # bridge call at all, so a host's uptime pinger always gets a fast
        # 200 even if the bot itself is mid-reconnect.
        return "Bot is alive, tracking, and rendering dynamic rating cards!"

    return app
