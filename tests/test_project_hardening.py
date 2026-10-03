import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectHardeningTests(unittest.TestCase):
    def test_dashboard_has_no_inline_script_or_event_handlers(self):
        html = (ROOT / "web_static" / "index.html").read_text()
        self.assertNotRegex(html, r"<script(?![^>]*src=)[^>]*>")
        self.assertNotRegex(html, r"\son(?:click|input|change|submit|load|keydown|keyup)=")
        self.assertIn('<script src="app.js" defer></script>', html)

    def test_frontend_escapes_html_and_uses_safe_dom_for_api_values(self):
        js = (ROOT / "web_static" / "js" / "app.js").read_text()
        self.assertIn("function escapeHTML(value)", js)
        self.assertIn("textContent = join.user_name", js)
        self.assertIn("textContent = command.description", js)
        # The only remaining innerHTML assignment is the controlled helper used
        # for ranking markup whose interpolated values are escaped first.
        self.assertEqual(js.count("innerHTML = html"), 1)
        self.assertNotIn("onclick=", js)

    def test_live_permission_and_config_validation_are_wired(self):
        api = (ROOT / "web" / "api.py").read_text()
        bridge = (ROOT / "web" / "bridge.py").read_text()
        self.assertIn("verify_guild_manager", api)
        self.assertIn("validate_guild_config_ids", api)
        self.assertIn("await guild.fetch_member(user_id)", bridge)
        self.assertIn("isinstance(channel, discord.TextChannel)", bridge)

    def test_recent_joins_are_limited_in_sql(self):
        db = (ROOT / "core" / "database.py").read_text()
        self.assertIn("def _raw_get_joins_in_range(guild_id, start_dt=None, limit=None, newest_first=False)", db)
        self.assertIn('query += " LIMIT %s"', db)
        api = (ROOT / "web" / "api.py").read_text()
        self.assertIn("limit=limit, newest_first=True", api)

    def test_production_server_and_security_headers(self):
        main = (ROOT / "main.py").read_text()
        app = (ROOT / "web" / "app.py").read_text()
        self.assertIn("from waitress import serve", main)
        self.assertIn('script-src \'self\'', app)
        self.assertIn('X-Content-Type-Options', app)
        self.assertIn('frame-ancestors \'none\'', app)

    def test_oauth_does_not_force_silent_authorization(self):
        oauth = (ROOT / "web" / "oauth.py").read_text()
        self.assertNotIn('"prompt": "none"', oauth)

    def test_development_bypass_is_disabled_in_production(self):
        config = (ROOT / "core" / "config.py").read_text()
        self.assertIn('if APP_ENV != "production"', config)

    def test_requirements_are_pinned(self):
        lines = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines() if line.strip() and not line.startswith("#")]
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[^=]+$")

    def test_python_files_parse(self):
        for path in ROOT.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            ast.parse(path.read_text(), filename=str(path))

    def test_app_js_parses(self):
        result = subprocess.run(
            ["node", "--check", str(ROOT / "web_static" / "js" / "app.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
