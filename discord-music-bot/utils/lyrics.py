"""
Lyrics fetching for the /lyrics command.

Uses the free lyrics.ovh API (no API key required). Coverage is best-effort:
very new releases, live versions, or regional/Arabic tracks not indexed by
the service may come back empty — that's a limitation of the free API, not
a bug in the bot.
"""

from __future__ import annotations

import re
import urllib.parse

import aiohttp

LYRICS_API = "https://api.lyrics.ovh/v1/{artist}/{title}"

# Common YouTube title noise that should be stripped before searching, so
# "Artist - Song (Official Music Video) [HD]" becomes "Artist" / "Song".
_NOISE_PATTERNS = [
    r"\(official.*?\)", r"\[official.*?\]",
    r"\(lyrics?.*?\)", r"\[lyrics?.*?\]",
    r"\(audio.*?\)", r"\[audio.*?\]",
    r"\(video.*?\)", r"\[video.*?\]",
    r"\(hd.*?\)", r"\[hd.*?\]",
    r"\(4k.*?\)", r"\[4k.*?\]",
    r"official music video", r"official video", r"lyric video",
]


class LyricsNotFoundError(Exception):
    """Raised when lyrics couldn't be found, or the query is ambiguous."""


def _clean(text: str) -> str:
    for pattern in _NOISE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return text.strip(" -|:\u2013\u2014")


def split_artist_title(raw: str) -> tuple[str, str]:
    """Best-effort split of a track title into (artist, title).

    Handles common separators like 'Artist - Title' or 'Artist: Title'.
    Returns ("", cleaned_raw) if no separator is found, meaning the caller
    should ask the user to be explicit.
    """
    for sep in (" - ", " \u2013 ", " \u2014 ", ": "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            return _clean(left), _clean(right)
    return "", _clean(raw)


async def fetch_lyrics(artist: str, title: str) -> str:
    """Fetch lyrics text for a given artist/title pair.

    Raises LyricsNotFoundError (with a user-friendly message) if nothing is
    found or the lyrics service is unreachable.
    """
    url = LYRICS_API.format(
        artist=urllib.parse.quote(artist, safe=""),
        title=urllib.parse.quote(title, safe=""),
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 404:
                    raise LyricsNotFoundError(
                        f"No lyrics found for **{title}** by **{artist}**."
                    )
                resp.raise_for_status()
                data = await resp.json()
    except aiohttp.ClientError as exc:
        raise LyricsNotFoundError(
            "The lyrics service is unavailable right now. Try again in a bit."
        ) from exc

    lyrics_text = (data or {}).get("lyrics", "").strip()
    if not lyrics_text:
        raise LyricsNotFoundError(
            f"No lyrics found for **{title}** by **{artist}**."
        )
    return lyrics_text