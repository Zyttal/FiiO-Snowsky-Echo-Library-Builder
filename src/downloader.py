"""YouTube-to-source-library downloader.

For one input request (Artist - Title, optional album), the flow is:

  1. Look up the song on MusicBrainz (src.musicbrainz). Gives us album,
     year, genre, track number, disc number, album artist, expected
     duration, cover-art URL.
  2. Search YouTube via yt-dlp. Pick the first candidate whose duration
     is within ±20 % of the MB duration when one is known. With no MB
     duration to compare against, take the top hit.
  3. Download the audio and let yt-dlp's FFmpegExtractAudio postprocessor
     hand it off to ffmpeg, which encodes a FLAC. (Yes, lossless
     container around YouTube's lossy bytes — but it keeps the output
     consistent with the rest of the source tree, and the existing build
     pipeline expects FLAC.)
  4. Fetch the cover art from the MB Cover Art Archive.
  5. Hand the FLAC + cover bytes to the existing tags.write_flac so it
     gets the same clean-Vorbis treatment every other source FLAC does.
  6. Move the finished FLAC to
     <source_root>/<Album> - <Artist>/<NN> - <Title>.flac, matching the
     repo's existing folder convention.

The build command picks it up on the next run — no special-cased
"downloaded" code path downstream.

Network policy notes:
  - We never embed credentials. yt-dlp anonymous works for the
    public-search-then-download path.
  - The audio is the user's responsibility. This tool is a personal
    library helper, not a content distribution system.
"""
from __future__ import annotations

import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from . import musicbrainz, sanitize, tags
from .config import Config

# ±20% duration tolerance: catches "live cover" mis-routes without
# rejecting legit variants (clean edit vs. album version, etc.).
_DURATION_TOLERANCE = 0.20

# yt-dlp's user-agent is sometimes blocked; bots-via-Chrome reads more
# honest than the default and avoids the occasional 429.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class DownloadResult:
    ok: bool
    artist: str
    title: str
    line_no: int
    target: Path | None = None
    youtube_url: str | None = None
    error: str | None = None
    notes: list[str] | None = None


def download_song(
    artist: str,
    title: str,
    album_hint: str | None,
    source_root: Path,
    cfg: Config,
    line_no: int = 0,
) -> DownloadResult:
    """End-to-end download + enrich + tag + place. Never raises; failures
    come back as DownloadResult(ok=False, error=...)."""
    notes: list[str] = []
    try:
        meta = musicbrainz.enrich(artist, title, album_hint)
        if meta is None:
            notes.append("MusicBrainz: no match; using request fields as-is")
            meta = _fallback_meta(artist, title, album_hint, cfg)
        else:
            notes.append(f"MB: {meta.album} ({meta.date or '?'})")

        url = _pick_youtube_result(
            query=f"{meta.artist} {meta.title} audio",
            expected_ms=meta.duration_ms,
            notes=notes,
        )
        if url is None:
            return DownloadResult(
                ok=False, artist=artist, title=title, line_no=line_no,
                error="YouTube: no acceptable result", notes=notes,
            )

        with tempfile.TemporaryDirectory(prefix="echo-dl-") as tmpdir:
            tmp_root = Path(tmpdir)
            flac_path = _download_to_flac(url, tmp_root)
            if flac_path is None:
                return DownloadResult(
                    ok=False, artist=artist, title=title, line_no=line_no,
                    youtube_url=url,
                    error="yt-dlp: no audio file produced",
                    notes=notes,
                )

            cover_bytes = _fetch_cover(meta.cover_art_url, notes) \
                if meta.cover_art_url else None

            source_tags = tags.SourceTags(
                artist=meta.artist,
                album=meta.album or "Unknown Album",
                title=meta.title,
                track_no=meta.track_no,
                disc_no=meta.disc_no,
                date=meta.date,
                genre=meta.genre or cfg.default_genre,
                album_artist=meta.album_artist,
            )
            tags.write_flac(flac_path, source_tags, cover_bytes)

            target = _final_path(source_root, source_tags, cfg)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(flac_path), str(target))

            # Also drop cover.jpg in the album folder so the existing
            # scan picks it up on rebuild instead of relying on the
            # embedded picture alone.
            if cover_bytes:
                cover_path = target.parent / "cover.jpg"
                if not cover_path.exists():
                    cover_path.write_bytes(cover_bytes)
                    notes.append("cover.jpg written")

        return DownloadResult(
            ok=True, artist=artist, title=title, line_no=line_no,
            target=target, youtube_url=url, notes=notes,
        )
    except Exception as e:  # noqa: BLE001
        return DownloadResult(
            ok=False, artist=artist, title=title, line_no=line_no,
            error=f"{type(e).__name__}: {e}", notes=notes,
        )


def _fallback_meta(
    artist: str, title: str, album_hint: str | None, cfg: Config,
) -> musicbrainz.Enriched:
    return musicbrainz.Enriched(
        artist=artist,
        album=album_hint or "Unknown Album",
        title=title,
        album_artist=artist,
        track_no=None,
        disc_no=None,
        date=None,
        genre=cfg.default_genre,
        cover_art_url=None,
        duration_ms=None,
        musicbrainz_recording_id="",
        musicbrainz_release_id="",
    )


def _pick_youtube_result(
    query: str, expected_ms: int | None, notes: list[str],
) -> str | None:
    """Search YouTube via yt-dlp, optionally filter by duration similarity.
    Returns a youtube URL or None."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch5",
        "user_agent": _USER_AGENT,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
        except yt_dlp.utils.DownloadError as e:
            notes.append(f"YouTube search error: {e}")
            return None
    entries = (info or {}).get("entries") or []
    if not entries:
        return None

    if expected_ms is None:
        return entries[0].get("url") or entries[0].get("webpage_url")

    target_s = expected_ms / 1000
    low = target_s * (1 - _DURATION_TOLERANCE)
    high = target_s * (1 + _DURATION_TOLERANCE)
    for entry in entries:
        dur = entry.get("duration")
        if dur and low <= dur <= high:
            return entry.get("url") or entry.get("webpage_url")
    notes.append(
        f"no result within ±{int(_DURATION_TOLERANCE * 100)}% of "
        f"{target_s:.0f}s; taking top hit ({entries[0].get('duration')}s)"
    )
    return entries[0].get("url") or entries[0].get("webpage_url")


def _download_to_flac(url: str, dest_dir: Path) -> Path | None:
    """Download `url`'s audio and extract to FLAC in `dest_dir`. Returns
    the path of the produced FLAC or None on failure."""
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "user_agent": _USER_AGENT,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "flac",
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([url])
        except yt_dlp.utils.DownloadError:
            return None
    flacs = list(dest_dir.glob("*.flac"))
    return flacs[0] if flacs else None


def _fetch_cover(url: str, notes: list[str]) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        notes.append(f"cover fetch failed ({type(e).__name__})")
        return None


def _final_path(source_root: Path, t: tags.SourceTags, cfg: Config) -> Path:
    """Build <source_root>/<Album> - <Artist>/<NN> - <Title>.flac matching
    the repo's existing folder convention."""
    album_seg = sanitize.segment(t.album, cfg)
    artist_seg = sanitize.segment(t.album_artist or t.artist, cfg)
    folder = f"{album_seg} - {artist_seg}"
    filename = sanitize.track_filename(t.track_no, t.title, "flac", cfg)
    return source_root / folder / filename
