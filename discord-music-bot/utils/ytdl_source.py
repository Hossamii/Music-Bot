"""
yt-dlp / FFmpeg audio source wrapper.

Handles searching YouTube and SoundCloud, resolving direct URLs, and building
a discord.py PCM audio source with FFmpeg. All text (titles, uploaders, etc.)
flows through as native UTF-8 Python strings, so non-Latin scripts (Arabic,
Japanese, Hindi, Korean, etc.) display correctly in Discord without extra
handling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Optional

import discord
import yt_dlp

from .queue_manager import Track

log = logging.getLogger("music.ytdl")

# --- YouTube cookies setup -------------------------------------------------
#
# YouTube frequently throws "Sign in to confirm you're not a bot" at
# datacenter/cloud IPs (Railway, Replit, etc.) without a logged-in session's
# cookies attached to the request.
#
# Two ways to supply cookies, checked in this order:
#   1. YTDLP_COOKIES env var — the *contents* of a Netscape-format
#      cookies.txt file, pasted directly into a Railway/host environment
#      variable. This is the preferred method: env vars are never touched
#      by git pushes/pulls, so cookies can't get wiped by a code update.
#      Written out to a temp file at startup since yt-dlp needs a file path.
#   2. cookies.txt file on disk next to bot.py (COOKIES_FILE env var can
#      override the path). Useful for local/manual setups, but note this
#      file is git-ignored — it will NOT be present after a fresh deploy
#      from GitHub unless you upload it directly to the host each time.
_COOKIES_ENV = os.environ.get("YTDLP_COOKIES")
COOKIES_FILE = os.environ.get(
    "COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cookies.txt"),
)


def _normalize_netscape_cookies(raw: str) -> str:
    """Repair common damage that happens when a cookies.txt file is pasted
    into a single-line env var UI (e.g. Railway's variable editor):
      - literal backslash-n sequences instead of real newlines
      - missing/garbled '# Netscape HTTP Cookie File' header, which
        Python's http.cookiejar parser requires to accept the file
    """
    text = raw.strip()
    # If there are no real newlines but there are literal "\n" sequences,
    # the paste flattened the file onto one line — unflatten it. Tabs
    # (the column separator in this format) can get the same treatment.
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    if "\t" not in text and "\\t" in text:
        text = text.replace("\\t", "\t")
    text = text.replace("\r\n", "\n").strip()
    if not text.startswith("# Netscape HTTP Cookie File") and not text.startswith("# HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text
    return text + "\n"


if _COOKIES_ENV:
    _tmp_cookies_path = os.path.join(tempfile.gettempdir(), "yt_dlp_cookies.txt")
    with open(_tmp_cookies_path, "w", encoding="utf-8") as _f:
        _f.write(_normalize_netscape_cookies(_COOKIES_ENV))
    COOKIES_FILE = _tmp_cookies_path
    log.info("Wrote YouTube cookies from YTDLP_COOKIES env var to %s", COOKIES_FILE)

# Force IPv4 (avoids some geo/ISP IPv6 resolution issues) and UTF-8 everywhere.
YTDL_FORMAT_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "encoding": "utf-8",
    "extract_flat": False,
    "geo_bypass": True,
    # Keep original metadata (titles, artist names) untouched/untransliterated.
    "writesubtitles": False,
}

if os.path.isfile(COOKIES_FILE):
    YTDL_FORMAT_OPTIONS["cookiefile"] = COOKIES_FILE
    log.info("Using YouTube cookies from %s", COOKIES_FILE)
else:
    log.warning(
        "No YouTube cookies found (checked YTDLP_COOKIES env var and %s) — "
        "YouTube may block playback with 'Sign in to confirm you're not a bot'.",
        COOKIES_FILE,
    )

FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"


class TrackUnavailableError(Exception):
    """Raised when a track can't be played (age-restricted, geo-blocked, etc.)."""


class YTDLSource:
    """Static helpers for resolving search queries / URLs into Track objects
    and building playable discord.py audio sources."""

    _ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

    @classmethod
    async def resolve(
        cls,
        query: str,
        *,
        requested_by: str,
        source: str = "youtube",
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> Track:
        """Resolve a search query or direct URL into a Track (metadata only;
        the actual stream URL is re-resolved right before playback since
        stream URLs expire)."""
        loop = loop or asyncio.get_event_loop()

        is_url = query.startswith("http://") or query.startswith("https://")
        if is_url:
            search_query = query
        elif source == "soundcloud":
            search_query = f"scsearch1:{query}"
        else:
            search_query = f"ytsearch1:{query}"

        try:
            data = await loop.run_in_executor(
                None, lambda: cls._ytdl.extract_info(search_query, download=False)
            )
        except yt_dlp.utils.DownloadError as exc:
            raise cls._translate_error(exc) from exc

        if data is None:
            raise TrackUnavailableError(
                "Nothing was found for that search or URL."
            )

        # Search results come back wrapped in an "entries" list.
        if "entries" in data:
            entries = [e for e in data["entries"] if e is not None]
            if not entries:
                raise TrackUnavailableError(
                    "No playable results were found for that search."
                )
            data = entries[0]

        stream_url = data.get("url")
        if not stream_url:
            # Some extractors require format selection; fall back to formats list.
            formats = data.get("formats") or []
            audio_formats = [f for f in formats if f.get("acodec") != "none"]
            if audio_formats:
                stream_url = audio_formats[-1]["url"]

        if not stream_url:
            raise TrackUnavailableError(
                "This track has no playable audio stream (it may be region-locked)."
            )

        detected_source = "soundcloud" if "soundcloud" in (data.get("extractor") or "") else "youtube"

        return Track(
            title=data.get("title") or "Unknown title",
            webpage_url=data.get("webpage_url") or query,
            stream_url=stream_url,
            duration=data.get("duration"),
            uploader=data.get("uploader"),
            thumbnail=data.get("thumbnail"),
            requested_by=requested_by,
            source=detected_source,
        )

    @classmethod
    async def refresh_stream_url(cls, track: Track, loop: Optional[asyncio.AbstractEventLoop] = None) -> str:
        """Stream URLs from yt-dlp expire quickly; re-resolve right before
        playback to avoid 403s on tracks that sat in the queue a while."""
        loop = loop or asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None, lambda: cls._ytdl.extract_info(track.webpage_url, download=False)
            )
        except yt_dlp.utils.DownloadError as exc:
            raise cls._translate_error(exc) from exc

        stream_url = data.get("url") if data else None
        if not stream_url:
            raise TrackUnavailableError(
                f'"{track.title}" is no longer available for playback.'
            )
        return stream_url

    @staticmethod
    def _translate_error(exc: yt_dlp.utils.DownloadError) -> TrackUnavailableError:
        message = str(exc).lower()
        if "sign in" in message or "age" in message:
            return TrackUnavailableError(
                "That video is age-restricted and can't be played by the bot."
            )
        if "not available" in message or "geo" in message or "blocked in your country" in message:
            return TrackUnavailableError(
                "That track is geo-blocked or unavailable in this region. Try a different source or search term."
            )
        if "private video" in message:
            return TrackUnavailableError("That video is private and can't be played.")
        if "unable to extract" in message or "unsupported url" in message:
            return TrackUnavailableError("That URL isn't supported. Try a search term instead.")
        return TrackUnavailableError(f"Couldn't load that track: {exc}")

    @staticmethod
    def build_audio_source(
        stream_url: str, volume: float, audio_filter: str = ""
    ) -> discord.PCMVolumeTransformer:
        """Build a discord.py audio source from a resolved stream URL,
        piping through FFmpeg with auto-reconnect on connection drops.

        `audio_filter` is an optional FFmpeg `-af` filter graph string (used
        for the bass boost feature); pass "" for no extra filtering.
        """
        options = FFMPEG_OPTIONS
        if audio_filter:
            options = f'{FFMPEG_OPTIONS} -af "{audio_filter}"'
        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=options,
        )
        return discord.PCMVolumeTransformer(source, volume=volume)