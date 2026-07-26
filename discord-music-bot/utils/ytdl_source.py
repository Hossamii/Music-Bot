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
# Three ways to supply cookies, checked in this order:
#   1. YTDLP_COOKIES_B64 env var (recommended) — the *entire contents* of a
#      Netscape-format cookies.txt file, base64-encoded. This is the most
#      reliable option for pasting into a host's env var UI: base64 output
#      is plain ASCII with no tabs/newlines, so it can't get silently
#      mangled the way a raw multi-line paste can (some web UIs — Railway's
#      variable editor included — convert literal tab characters to spaces
#      when a long value is pasted into a text field, which corrupts the
#      Netscape format since fields are tab-separated; yt-dlp then silently
#      drops any cookie line that gets mangled this way, which is enough to
#      break authentication entirely even though most of the file still
#      "looks" fine).
#   2. YTDLP_COOKIES env var — the raw *contents* of a cookies.txt file
#      pasted directly. Kept for backwards compatibility; prefer
#      YTDLP_COOKIES_B64 above if you're hitting mysterious auth failures
#      with this one, since a corrupted paste is very hard to spot by eye.
#   3. cookies.txt file on disk next to bot.py (COOKIES_FILE env var can
#      override the path). Useful for local/manual setups, but note this
#      file is git-ignored — it will NOT be present after a fresh deploy
#      from GitHub unless you upload it directly to the host each time.
_COOKIES_B64_ENV = os.environ.get("YTDLP_COOKIES_B64")
_COOKIES_ENV = os.environ.get("YTDLP_COOKIES")
COOKIES_FILE = os.environ.get(
    "COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cookies.txt"),
)

# Optional override for which YouTube "player client" yt-dlp impersonates.
# Cloud/datacenter IPs (Railway, Render, etc.) are increasingly asked for a
# PO Token by YouTube's default clients even when cookies are valid; some
# alternate clients avoid that requirement (at the cost of not being able to
# resolve some age-restricted/members-only content). This is a fast-moving
# target as YouTube adjusts bot-detection, so it's an env var you can change
# from Railway's dashboard and restart — no redeploy needed — instead of a
# hardcoded value. Comma-separated, e.g. "tv,web_embedded,android". Leave
# unset to use yt-dlp's own default client selection.
# See: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
_PLAYER_CLIENT = os.environ.get("YTDLP_PLAYER_CLIENT")


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


if _COOKIES_B64_ENV:
    import base64
    _tmp_cookies_path = os.path.join(tempfile.gettempdir(), "yt_dlp_cookies.txt")
    try:
        _decoded = base64.b64decode(_COOKIES_B64_ENV.strip()).decode("utf-8")
        with open(_tmp_cookies_path, "w", encoding="utf-8") as _f:
            _f.write(_decoded if _decoded.endswith("\n") else _decoded + "\n")
        COOKIES_FILE = _tmp_cookies_path
        log.info("Wrote YouTube cookies from YTDLP_COOKIES_B64 env var to %s", COOKIES_FILE)
    except Exception:
        log.exception(
            "Failed to decode YTDLP_COOKIES_B64 — make sure it's the base64 encoding of the "
            "*whole* cookies.txt file (e.g. `base64 -w0 cookies.txt` on Linux/Mac, or "
            "`[Convert]::ToBase64String([IO.File]::ReadAllBytes('cookies.txt'))` in PowerShell)."
        )
elif _COOKIES_ENV:
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

    _ytdl: yt_dlp.YoutubeDL = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)
    _ytdl_created_at: float = 0.0
    _YTDL_TTL: float = 3600.0  # rebuild the session every 1 hour

    @classmethod
    def _get_ytdl(cls) -> yt_dlp.YoutubeDL:
        """Return a fresh YoutubeDL instance, rebuilding it if the TTL has
        elapsed. YouTube blocks long-lived sessions from cloud IPs; cycling
        the instance resets the session and avoids 'Sign in' / age-restriction
        errors that appear hours after the bot starts even when cookies are
        valid."""
        import time
        now = time.monotonic()
        if now - cls._ytdl_created_at >= cls._YTDL_TTL:
            cls._ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)
            cls._ytdl_created_at = now
            log.info("Rebuilt yt-dlp YoutubeDL instance (TTL elapsed).")
        return cls._ytdl

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
            ytdl = cls._get_ytdl()
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(search_query, download=False)
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
            ytdl = cls._get_ytdl()
            data = await loop.run_in_executor(
                None, lambda: ytdl.extract_info(track.webpage_url, download=False)
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
        # IMPORTANT: check the bot-detection message BEFORE the generic
        # "sign in" check below. YouTube uses "sign in" in two very
        # different messages that must NOT be collapsed into one:
        #   - "Sign in to confirm you're not a bot" -> NOT age-restriction.
        #     This is YouTube's anti-scraping check on datacenter/cloud IPs
        #     (Railway, Replit, etc.) and fires when the cookies YTDLSource
        #     is using are missing, expired, or invalid — cookies expire on
        #     their own over time even if the YTDLP_COOKIES env var on the
        #     host was never touched. Needs a *fresh* cookie export, not a
        #     code fix.
        #   - "Sign in to confirm your age" -> genuine age-restriction.
        # The old version matched both under one "age-restricted" message,
        # which made an expired-cookies problem look identical to (and get
        # mistaken for) a real age-restricted video.
        if "not a bot" in message or "confirm you" in message and "bot" in message:
            return TrackUnavailableError(
                "YouTube is blocking this server's requests until it can verify it's not a bot — this "
                "usually means the bot's YouTube cookies have expired and need to be re-exported/refreshed "
                "(this is separate from age-restriction; the cookie value doesn't need to have changed on "
                "your end for it to expire)."
            )
        if "confirm your age" in message or "age" in message:
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
        stream_url: str, volume: float, audio_filter: str = "", start_at: float = 0.0
    ) -> discord.PCMVolumeTransformer:
        """Build a discord.py audio source from a resolved stream URL,
        piping through FFmpeg with auto-reconnect on connection drops.

        `audio_filter` is an optional FFmpeg `-af` filter graph string (used
        for the bass boost feature); pass "" for no extra filtering.

        `start_at` seeks the stream to that many seconds in before playback
        starts (used to resume a track from where it was after a filter
        change, instead of restarting from 0:00). `-ss` is placed in
        `before_options` (i.e. before `-i`) so FFmpeg seeks the input
        directly instead of decoding and discarding everything up to that
        point — fast even for a seek several minutes in.
        """
        before_options = FFMPEG_BEFORE_OPTIONS
        if start_at and start_at > 0:
            before_options = f"-ss {start_at:.2f} {before_options}"
        options = FFMPEG_OPTIONS
        if audio_filter:
            options = f'{FFMPEG_OPTIONS} -af "{audio_filter}"'
        source = discord.FFmpegPCMAudio(
            stream_url,
            before_options=before_options,
            options=options,
        )
        return discord.PCMVolumeTransformer(source, volume=volume)
