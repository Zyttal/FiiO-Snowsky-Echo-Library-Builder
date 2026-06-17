"""Fetch high-resolution album covers from the MusicBrainz Cover Art Archive.

When a source album folder has no cover.jpg, or one too small for the
Echo's indexer (the firmware likes ~500 px square), we look up the
release on MusicBrainz, get its CAA front-cover URL, and drop a clean
500 px JPEG into the album folder. The build pipeline picks it up
naturally on the next pass — no special-cased downstream code.

Network policy notes:
  - MusicBrainz' search endpoint enforces a 1 req/sec rate limit; we
    rely on musicbrainzngs' built-in throttle (same as src.musicbrainz).
  - CAA itself is a static CDN — no rate limit, anonymous reads are
    fine.
  - Lookups are cached on disk in `cover_art_cache/` next to the source
    root so a re-run is instant.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import musicbrainzngs

from . import musicbrainz as mb

_CAA_FRONT_URL = "https://coverartarchive.org/release/{release_id}/front-{size}"

# Size threshold: anything below this triggers a CAA re-fetch even when
# a local cover.jpg exists. Default 500 px (matches the Echo's preferred
# indexer size).
_DEFAULT_MIN_SIZE_PX = 500


def find_release_id(
    artist: str,
    album: str,
    year: str | None = None,
) -> str | None:
    """Search MusicBrainz for a release matching (artist, album[, year]).

    Reuses src.musicbrainz's release-picking logic so we land on the
    earliest studio release rather than a 2015 remaster. Returns the
    MBID of the chosen release, or None if MB had nothing usable."""
    mb._ensure_configured()
    parts = [f'artist:"{mb._escape(artist)}"', f'release:"{mb._escape(album)}"']
    if year:
        parts.append(f"date:{year}")
    query = " AND ".join(parts)
    try:
        result = musicbrainzngs.search_releases(query=query, limit=10)
    except musicbrainzngs.WebServiceError:
        return None
    releases = result.get("release-list", [])
    if not releases:
        return None
    chosen = mb._pick_release(releases, album_hint=album)
    return chosen.get("id") if chosen else None


def fetch(release_id: str, size: int = _DEFAULT_MIN_SIZE_PX) -> bytes | None:
    """Fetch the front cover from CAA for the given release. Returns the
    JPEG bytes or None on 404 / network error."""
    url = _CAA_FRONT_URL.format(release_id=release_id, size=size)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


def needs_replacement(cover_path: Path, min_size_px: int = _DEFAULT_MIN_SIZE_PX) -> bool:
    """True if cover_path is absent or smaller than `min_size_px` on its
    short edge. Uses Pillow only when the file exists."""
    if not cover_path.exists():
        return True
    try:
        from PIL import Image
        with Image.open(cover_path) as im:
            short = min(im.size)
        return short < min_size_px
    except Exception:  # noqa: BLE001
        # Corrupt file → replace.
        return True


def enrich_album_cover(
    album_dir: Path,
    artist: str,
    album: str,
    year: str | None = None,
    cache_dir: Path | None = None,
    min_size_px: int = _DEFAULT_MIN_SIZE_PX,
) -> tuple[str | None, bool]:
    """Idempotent end-to-end enrichment for one album folder.

    Returns (release_id, wrote_cover). When `wrote_cover` is True, the
    folder now has a fresh `<min_size_px>px` cover.jpg. When False,
    either an acceptable cover already existed or MB/CAA had nothing.

    `cache_dir`, if provided, caches release_id lookups by
    `artist|album|year` so repeated runs don't re-hit MB. The bytes
    themselves are not cached — CAA is fast enough and disk space matters
    more than network round-trips for a fresh build.
    """
    cover_path = album_dir / "cover.jpg"
    if not needs_replacement(cover_path, min_size_px):
        return None, False

    release_id = None
    cache_path = (cache_dir / "release_id_cache.json") if cache_dir else None
    cache: dict[str, str | None] = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}
    cache_key = f"{artist}|{album}|{year or ''}"
    if cache_key in cache:
        release_id = cache[cache_key]
    else:
        release_id = find_release_id(artist, album, year)
        cache[cache_key] = release_id
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    if not release_id:
        return None, False

    data = fetch(release_id, size=min_size_px)
    if data is None:
        return release_id, False

    album_dir.mkdir(parents=True, exist_ok=True)
    cover_path.write_bytes(data)
    return release_id, True
