"""
Tiny Flask web server used to keep the bot process alive 24/7 on Replit.

An external uptime pinger (e.g. UptimeRobot) can hit this server's URL
periodically to prevent the workspace from going idle. It runs in a
background thread so it never blocks the Discord bot's event loop.
"""

import logging
import os
import threading

from flask import Flask

app = Flask(__name__)
log = logging.getLogger("keep_alive")

# Quiet down Flask's default request logging so it doesn't spam the console
# alongside the bot's own logs.
logging.getLogger("werkzeug").setLevel(logging.WARNING)


@app.route("/")
def home():
    return "Bot is alive!"


@app.route("/health")
def health():
    return {"status": "ok"}


# Configurable so this never collides with another service's port; defaults
# to 8000, which is reserved for this bot's workflow.
KEEP_ALIVE_PORT = int(os.environ.get("KEEP_ALIVE_PORT", "8000"))


def _run(port: int):
    try:
        app.run(host="0.0.0.0", port=port)
    except OSError:
        log.exception(
            "keep_alive server failed to bind to port %d (already in use?). "
            "The Discord bot will keep running regardless.",
            port,
        )


def keep_alive():
    """Start the Flask keep-alive server in a background thread. Runs as a
    daemon thread so it never blocks bot shutdown, and failures here are
    logged but never crash the bot itself."""
    thread = threading.Thread(target=_run, args=(KEEP_ALIVE_PORT,), daemon=True)
    thread.start()
    log.info("keep_alive server starting on port %d", KEEP_ALIVE_PORT)
