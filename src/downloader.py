"""YouTube-to-source-library downloader.

For one input request (Artist - Title, optional album), the flow is:

  1. Look up the song on MusicBrainz (src.musicbrainz). Gives us album,
     year, genre, track number, disc number, album artist, expected
     duration, cover-art URL.
  2. Search YouTube via yt-dlp. Pick the first candidate whose duration
     is within ±20 % of the MB duration when one is known. With no MB
     duration to compare against, take the top hit.
  3. Download the audio. yt-dlp's FFmpegExtractAudio postprocessor remuxes
     the source stream into a tag-friendly container — by default
     (cfg.download_audio_format = "passthrough") that means keeping the
     original codec (AAC m4a or Opus from YouTube). Set the config flag
     to "m4a" to force AAC re-encoding, or "flac" for the v0.1.0 lossless-
     container-around-lossy-bytes behavior.
  4. Fetch the cover art from the MB Cover Art Archive.
  5. Hand the audio file + cover bytes to tags.write_tags, which dispatches
     to the right per-format writer (FLAC vorbis comments, m4a iTunes
     atoms, opus vorbis comments via OggOpus).
  6. Move the finished file to
     <source_root>/<Album> - <Artist>/<NN> - <Title>.<ext>, matching the
     repo's existing folder convention. The extension is whatever the
     downloader produced — .m4a or .opus for passthrough, .flac for the
     flac mode.

The build command picks it up on the next run — no special-cased
"downloaded" code path downstream.

Network policy notes:
  - We never embed credentials. yt-dlp anonymous works for the
    public-search-then-download path.
  - The audio is the user's responsibility. This tool is a personal
    library helper, not a content distribution system.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from . import lyrics, musicbrainz, sanitize, tags
from .config import Config

# Duration tolerance for the YouTube match scorer. When MB returned a
# duration we expect within ±5 s; without one (rare on obscure songs) we
# fall back to ±15 s to keep the candidate pool non-empty.
_DURATION_TOLERANCE_S_WITH_MB = 5.0
_DURATION_TOLERANCE_S_NO_MB = 15.0

# Title tokens that almost always mean "wrong recording" — penalize unless
# the requested track title also has the word (the user genuinely wants
# the live/cover/remix version).
_TITLE_PENALTIES = (
    "live", "cover", "karaoke", "instrumental", "remix",
    "reaction", "tutorial", "lesson", "lyrics video", "8-bit",
)

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
            artist=meta.artist,
            title=meta.title,
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
            audio_path = _download_audio(url, tmp_root, cfg.download_audio_format)
            if audio_path is None:
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
            wrote = tags.write_tags(audio_path, source_tags, cover_bytes)
            if not wrote:
                notes.append(
                    f"warning: no tag writer for .{audio_path.suffix.lstrip('.')}"
                )

            ext = audio_path.suffix.lstrip(".") or "audio"
            target = _final_path(source_root, source_tags, cfg, ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio_path), str(target))

            # Also drop cover.jpg in the album folder so the existing
            # scan picks it up on rebuild instead of relying on the
            # embedded picture alone.
            if cover_bytes:
                cover_path = target.parent / "cover.jpg"
                if not cover_path.exists():
                    cover_path.write_bytes(cover_bytes)
                    notes.append("cover.jpg written")

            if cfg.fetch_lyrics:
                duration_s = (meta.duration_ms / 1000) if meta.duration_ms else None
                try:
                    lrc = lyrics.fetch_lrc(
                        artist=meta.artist,
                        title=meta.title,
                        album=meta.album or None,
                        duration_s=int(duration_s) if duration_s else None,
                    )
                except Exception:  # noqa: BLE001
                    lrc = None
                if lrc:
                    target.with_suffix(".lrc").write_text(lrc, encoding="utf-8")
                    notes.append("lyrics: lrc written")
                else:
                    notes.append("lyrics: no LRCLIB match")

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
    artist: str, title: str, expected_ms: int | None, notes: list[str],
) -> str | None:
    """Search YouTube and pick the best-matching audio result.

    Builds a 10-result candidate pool and ranks each entry by a composite
    score: how close its duration is to the MB-supplied target, whether
    the uploader is the artist's auto-generated "Topic" channel (YouTube
    Music's near-canonical uploads), how much its title overlaps the
    request, and a penalty for live/cover/karaoke/remix unless the user
    actually asked for that variant.

    Returns the winning URL or None on a search failure. Appends a
    "low-confidence match" note when no candidate clears the duration
    window and we fall back to the top-scored entry anyway.
    """
    query = f"{artist} {title} audio"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "default_search": "ytsearch10",
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

    target_s = (expected_ms / 1000) if expected_ms else None
    tol = (_DURATION_TOLERANCE_S_WITH_MB if expected_ms
           else _DURATION_TOLERANCE_S_NO_MB)
    requested_lc = f"{artist} {title}".lower()
    title_tokens = _tokenize(title)

    scored: list[tuple[float, bool, dict]] = []
    for e in entries:
        score, in_window = _score_entry(
            e, artist=artist, title_tokens=title_tokens,
            target_s=target_s, tol=tol, requested_lc=requested_lc,
        )
        scored.append((score, in_window, e))

    # Best candidate within the duration window wins outright. If none
    # are inside the window, fall back to the highest-scored entry and
    # flag the match as low confidence.
    in_window = [s for s in scored if s[1]]
    pool = in_window or scored
    pool.sort(key=lambda x: -x[0])
    best = pool[0][2]
    if not in_window:
        notes.append(
            f"low-confidence match: no result within ±{tol:.0f}s of "
            f"{target_s:.0f}s" if target_s else
            "low-confidence match: no MB duration to verify against"
        )
    return best.get("url") or best.get("webpage_url")


def _score_entry(
    entry: dict, artist: str, title_tokens: set[str],
    target_s: float | None, tol: float, requested_lc: str,
) -> tuple[float, bool]:
    """Return (score, in_duration_window). Higher score = better match."""
    score = 0.0
    in_window = True
    dur = entry.get("duration")
    if target_s is not None and dur:
        diff = abs(dur - target_s)
        in_window = diff <= tol
        # Duration fit: 1.0 at exact match, 0 at the tolerance edge,
        # negative beyond (still ranks, just discouraged).
        score += max(-1.0, 1.0 - (diff / max(tol, 1.0)))
    elif target_s is None:
        # No MB duration; everyone's "in window" — let the other signals
        # decide.
        in_window = True

    # Channel preference: 'Artist - Topic' uploads from YouTube Music.
    uploader = (entry.get("uploader") or entry.get("channel") or "").strip()
    if uploader and re.search(
        rf"\b{re.escape(artist)}\b.*-\s*Topic\s*$",
        uploader, flags=re.IGNORECASE,
    ):
        score += 1.0

    # Title token overlap. Each requested-title token also present in the
    # video title contributes a small positive.
    video_title_lc = (entry.get("title") or "").lower()
    video_tokens = _tokenize(video_title_lc)
    overlap = title_tokens & video_tokens
    score += 0.25 * len(overlap)

    # Penalty for live/cover/karaoke/etc. unless the user asked for it.
    for word in _TITLE_PENALTIES:
        if word in video_title_lc and word not in requested_lc:
            score -= 0.5

    return score, in_window


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, dropping stopwords that match
    everything ('the', 'a'). Single-character tokens are kept — they
    matter for things like 'U2'."""
    stop = {"the", "a", "an", "of", "and"}
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in stop}


def _download_audio(url: str, dest_dir: Path, audio_format: str) -> Path | None:
    """Download `url`'s audio into `dest_dir` and return the produced path.

    `audio_format` controls the FFmpegExtractAudio postprocessor:
      - "passthrough" → preferredcodec="best": no re-encode, remuxes
        webm/opus to .opus and leaves .m4a alone. Smallest output,
        full FiiO Echo compatibility.
      - "m4a" → re-encodes to AAC in an m4a container (codec uniformity).
      - "flac" → wraps the lossy source in a FLAC container (v0.1.0
        behavior, kept for users who want uniform .flac sources).
    Returns None on yt-dlp failure or if no file landed in dest_dir.
    """
    preferred = {
        "passthrough": "best",
        "m4a": "m4a",
        "flac": "flac",
    }.get(audio_format, "best")
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "user_agent": _USER_AGENT,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": preferred,
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([url])
        except yt_dlp.utils.DownloadError:
            return None
    # yt-dlp may leave the original alongside the postprocessed file;
    # the postprocessor renames the original to .orig.ext. Prefer the
    # actual audio file by extension, falling back to whatever remains.
    audio_exts = {".m4a", ".opus", ".flac", ".mp3", ".ogg", ".wav", ".aac"}
    candidates = [
        p for p in dest_dir.iterdir()
        if p.is_file() and p.suffix.lower() in audio_exts
        and ".orig." not in p.name
    ]
    if not candidates:
        candidates = [
            p for p in dest_dir.iterdir()
            if p.is_file() and ".orig." not in p.name
        ]
    return candidates[0] if candidates else None


def _fetch_cover(url: str, notes: list[str]) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        notes.append(f"cover fetch failed ({type(e).__name__})")
        return None


def _final_path(
    source_root: Path, t: tags.SourceTags, cfg: Config, ext: str = "flac",
) -> Path:
    """Build <source_root>/<Album> - <Artist>/<NN> - <Title>.<ext> matching
    the repo's existing folder convention. `ext` is "flac" for v0.1.0
    behavior or whatever the downloader produced ("m4a", "opus") in
    passthrough mode."""
    album_seg = sanitize.segment(t.album, cfg)
    artist_seg = sanitize.segment(t.album_artist or t.artist, cfg)
    folder = f"{album_seg} - {artist_seg}"
    filename = sanitize.track_filename(t.track_no, t.title, ext, cfg)
    return source_root / folder / filename
