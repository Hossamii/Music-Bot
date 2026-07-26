"""
Tiny JSON-file-backed store for the bot's custom presence (status + activity).

Why this exists: bot.py's on_ready used to always re-apply a hardcoded
default activity and never passed `status=`, so discord.py silently reset
the status back to `online` every single time on_ready fired. Since
on_ready can fire more than once per process (Discord sometimes forces a
fresh READY instead of resuming the existing session), any custom
status/activity set via `setstatus` / `setpresence` could randomly get
wiped out mid-session with no action from the user — and, because nothing
was ever saved to disk, a real process restart lost it for good too.

This module gives on_ready something to reload and re-apply instead of the
hardcoded default, and gives setstatus/setpresence a place to persist
whatever was last chosen.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional, TypedDict

log = logging.getLogger("music.presence_store")

# Lives next to bot.py (discord-music-bot/presence_state.json), same
# convention as utils/ytdl_source.py's cookies.txt lookup.
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "presence_state.json"
)


class PresenceState(TypedDict):
    status: str          # "online" | "idle" | "dnd" | "offline"
    activity_type: str   # "playing" | "listening" | "watching" | "competing"
    activity_text: str


DEFAULT_STATE: PresenceState = {
    "status": "online",
    "activity_type": "listening",
    "activity_text": "play <song>",
}


def load() -> PresenceState:
    """Read the last-saved presence, falling back to defaults if the file
    is missing, unreadable, or this is the very first run."""
    if not os.path.isfile(_STORE_PATH):
        return dict(DEFAULT_STATE)  # type: ignore[return-value]
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        log.warning("Couldn't read presence_state.json — using defaults.")
        return dict(DEFAULT_STATE)  # type: ignore[return-value]

    merged: PresenceState = dict(DEFAULT_STATE)  # type: ignore[assignment]
    merged.update({k: v for k, v in data.items() if k in DEFAULT_STATE and v})
    return merged


def save(
    *,
    status: Optional[str] = None,
    activity_type: Optional[str] = None,
    activity_text: Optional[str] = None,
) -> None:
    """Update only the given field(s) on top of whatever's already saved,
    then persist the merged result to disk. Called after every successful
    setstatus/setpresence so the change survives reconnects and restarts."""
    current = load()
    if status is not None:
        current["status"] = status
    if activity_type is not None:
        current["activity_type"] = activity_type
    if activity_text is not None:
        current["activity_text"] = activity_text

    try:
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(current, f)
    except OSError:
        log.warning(
            "Couldn't write presence_state.json — the current presence won't survive a restart."
        )
