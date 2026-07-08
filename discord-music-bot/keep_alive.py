"""
Tiny Flask web server used to keep the bot process alive 24/7 on Replit.

An external uptime pinger (e.g. UptimeRobot) can hit this server's URL
periodically to prevent the workspace from going idle. It runs in a
background thread so it never blocks the Discord bot's event loop.
"""

import logging
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


def _run():
    # Port 8080 is proxied by Replit automatically.
    app.run(host="0.0.0.0", port=8080)


def keep_alive():
    """Start the Flask keep-alive server in a background thread."""
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    log.info("keep_alive server started on port 8080")
