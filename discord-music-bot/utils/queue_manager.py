"""
Per-guild queue and playback state for the music bot.

Every guild (Discord server) gets its own GuildMusicState so that multiple
servers can play independent queues at the same time without interfering
with each other.
"""

from __future__ import annotations

import asyncio
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
