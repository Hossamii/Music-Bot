"""
Per-guild queue and playback state for the music bot.

Every guild (Discord server) gets its own GuildMusicState so that multiple
servers can play independent queues at the same time without interfering
with each other.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


@dataclass
class Track:
    """Metadata for a single queued track. Titles are kept as native Python
    str (UTF-8) so any language/script displays correctly in Discord."""

    title: str
    webpage_url: str
    stream_url: str
    duration: Optional[int]  # seconds, may be None for live streams
    uploader: Optional[str]
    thumbnail: Optional[str]
    requested_by: str
    source: str = "youtube"  # "youtube" or "soundcloud"

    def formatted_duration(self) -> str:
        if not self.duration:
            return "Live/Unknown"
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


@dataclass
class GuildMusicState:
    """Holds the queue, current track, and playback settings for one guild."""

    queue: Deque[Track] = field(default_factory=deque)
    current: Optional[Track] = None
    volume: float = 0.5  # 0.0 - 2.0 (50% - 200%)
    voice_client: Optional[object] = None
    text_channel: Optional[object] = None
    play_next_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    skip_requested: bool = False
    bass_level: str = "off"  # "off" | "low" | "medium" | "high" | "extreme"
    # Set right before an intentional disconnect (e.g. /leave) so the
    # voice_state_update listener knows NOT to try to auto-reconnect.
    expected_disconnect: bool = False

    # --- playback position tracking -------------------------------------
    # Used so things like `bassboost` can restart the current track from
    # where it left off instead of from the very beginning (FFmpeg can't
    # change its filter graph mid-stream, so a restart is unavoidable, but
    # losing the listener's place isn't).
    playback_started_at: Optional[float] = None  # time.monotonic() when the current source started
    playback_offset: float = 0.0  # seconds already elapsed before this source started (e.g. after a seek-restart)
    paused_at: Optional[float] = None  # time.monotonic() when pause() was last called, else None
    paused_duration: float = 0.0  # total seconds spent paused during the current source
    # Set just before requeuing the current track for a filter-change
    # restart; consumed (and reset to 0) the next time a source is built.
    pending_seek: float = 0.0

    def start_playback(self, offset: float = 0.0) -> None:
        """Call right after a new audio source actually starts playing.
        `offset` is how many seconds into the track this source already
        starts at (non-zero for a seek-restart)."""
        self.playback_started_at = time.monotonic()
        self.playback_offset = offset
        self.paused_at = None
        self.paused_duration = 0.0

    def mark_paused(self) -> None:
        if self.paused_at is None:
            self.paused_at = time.monotonic()

    def mark_resumed(self) -> None:
        if self.paused_at is not None:
            self.paused_duration += time.monotonic() - self.paused_at
            self.paused_at = None

    def elapsed_seconds(self) -> float:
        """Best-effort estimate of how far into the current track playback
        is right now, accounting for time spent paused."""
        if self.playback_started_at is None:
            return 0.0
        now = time.monotonic()
        paused_extra = (now - self.paused_at) if self.paused_at is not None else 0.0
        elapsed = self.playback_offset + (now - self.playback_started_at) - self.paused_duration - paused_extra
        return max(0.0, elapsed)

    def add(self, track: Track) -> int:
        """Add a track to the queue. Returns its 1-based position."""
        self.queue.append(track)
        return len(self.queue)

    def pop_next(self) -> Optional[Track]:
        if self.queue:
            return self.queue.popleft()
        return None

    def clear(self) -> None:
        self.queue.clear()
        self.current = None
        self.pending_seek = 0.0

    def is_playing(self) -> bool:
        return self.voice_client is not None and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        )


class MusicManager:
    """Registry of GuildMusicState objects, one per guild ID."""

    def __init__(self) -> None:
        self._states: dict[int, GuildMusicState] = {}

    def get(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildMusicState()
        return self._states[guild_id]

    def remove(self, guild_id: int) -> None:
        self._states.pop(guild_id, None)
