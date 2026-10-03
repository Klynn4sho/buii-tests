"""Production WSGI entry point documentation/helper.

The bot still owns the process and starts Flask from main.py. When running the
web app under a WSGI server in a separate deployment process, import
`create_app` and bind it to a bot instance that is already running.
"""

from web.app import create_app

__all__ = ["create_app"]
