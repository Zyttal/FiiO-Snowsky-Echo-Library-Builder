"""LRCLIB-backed lyrics fetcher.

LRCLIB (lrclib.net) is a public, free, no-API-key lyrics database with
both synchronised (.lrc-style timestamped) and plain-text lyrics. Their
/api/get endpoint matches on (artist, track, album, duration) — duration
in particular helps disambiguate covers and remasters with the same
artist/title.

We always prefer the synced payload (`syncedLyrics`); for tracks that
only have plain lyrics in LRCLIB, we wrap them in a minimal [00:00.00]
sidecar so the on-disk file is uniformly `.lrc`-shaped.

Network policy notes:
  - No API key required.
  - LRCLIB's terms ask for a friendly User-Agent identifying the
    application; we set one (matches what musicbrainz module does).
  - The DAP firmware reads the on-disk sidecar — we don't embed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

_LRCLIB_GET = "https://lrclib.net/api/get"
_LRCLIB_SEARCH = "https://lrclib.net/api/search"
_USER_AGENT = (
    "echo-library-builder/0.2 "
    "(https://github.com/local/echo-library-builder)"
)


def fetch_lrc(
    artist: str,
    title: str,
    album: str | None = None,
    duration_s: int | None = None,
) -> str | None:
    """Look up a track on LRCLIB and return its lyrics as an LRC-formatted
    string. Returns None on no match anywhere, network error, or empty
    lyrics payload.

    Tries the strict /api/get endpoint first (matches on exact
    artist + title + duration); on miss, falls back to /api/search and
    picks the closest-duration result that has lyrics. This second pass
    matters for compilation tracks whose ALBUM tag is the compilation
    name rather than the original studio album.

    LRC format: zero or more `[mm:ss.xx] lyric line` lines for synced
    versions; for plain lyrics we prepend a single `[00:00.00]` so the
    file is parseable by basic LRC readers, even if they ignore the
    timestamps.
    """
    data = _try_strict_get(artist, title, album, duration_s)
    if data is None:
        data = _try_search(artist, title, duration_s)
    if data is None:
        return None
    return _format_lrc(data)


def _try_strict_get(
    artist: str, title: str, album: str | None, duration_s: int | None,
) -> dict | None:
    params: dict[str, str] = {
        "artist_name": artist,
        "track_name": title,
    }
    if album:
        params["album_name"] = album
    if duration_s:
        params["duration"] = str(int(round(duration_s)))
    return _http_get_json(f"{_LRCLIB_GET}?{urllib.parse.urlencode(params)}")


def _try_search(
    artist: str, title: str, duration_s: int | None,
) -> dict | None:
    params = {"artist_name": artist, "track_name": title}
    results = _http_get_json(
        f"{_LRCLIB_SEARCH}?{urllib.parse.urlencode(params)}"
    )
    if not isinstance(results, list) or not results:
        return None
    # Prefer entries that have synced lyrics, then nearest duration.
    def score(r: dict) -> tuple[int, float]:
        has_synced = 0 if r.get("syncedLyrics") else 1
        if duration_s and r.get("duration"):
            diff = abs(r["duration"] - duration_s)
        else:
            diff = 0.0
        return (has_synced, diff)
    return min(results, key=score)


def _http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _format_lrc(entry: dict) -> str | None:
    synced = (entry.get("syncedLyrics") or "").strip()
    if synced:
        return synced
    plain = (entry.get("plainLyrics") or "").strip()
    if plain:
        # Stamp every line at 00:00 so a strict LRC parser still walks
        # the file. Players that don't sync the lyrics still display
        # them sequentially.
        return "\n".join(f"[00:00.00] {line}" for line in plain.splitlines())
    return None
