"""Push a full album folder from the library to the SD card.

Parallels `src.playlist.push_playlist` but with album semantics:

- Target lives under `<sd_root>/Albums/<Artist>/<Album>/`, separating
  curated playlists (already under `/Playlists/`) from full albums.
- Source filenames are preserved — albums already carry the right
  track numbering, no need to re-number sequentially.
- Per-track `.lrc` sidecars and the album's `cover.jpg` come along.
- The OSError on `.lrc` is a soft failure (mirrors the v0.1.5 hotfix
  to playlist push): a read-only SD card shouldn't drop the audio.
- Idempotent via (mtime, size) up-to-date checks. `--prune` removes
  files on the card that no longer belong.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .sanitize import segment

ALBUMS_DIR = "Albums"

# Audio files we know the Echo plays. Files outside this set under the
# source album folder are not copied (no random PDFs, scans, etc.).
_AUDIO_EXTS = {".flac", ".m4a", ".opus", ".mp3", ".ogg", ".wav"}


@dataclass
class AlbumPushReport:
    artist: str
    album: str
    target_dir: Path
    copied: list[Path] = field(default_factory=list)
    skipped_up_to_date: list[Path] = field(default_factory=list)
    pruned: list[Path] = field(default_factory=list)
    cover_written: bool = False
    lrc_failed: list[Path] = field(default_factory=list)


def push_album(
    album_dir: Path,
    artist_name: str,
    album_name: str,
    sd_root: Path,
    cfg: Config,
    prune: bool = True,
    progress_callback=None,
    cancel_check=None,
) -> AlbumPushReport:
    """Copy every audio file in `album_dir` to
    <sd_root>/Albums/<sanitized_artist>/<sanitized_album>/, preserving
    filenames.

    `progress_callback` is invoked as callback(index, total, status,
    target_name) after each track, mirroring `push_playlist`.
    `cancel_check` returns True to abort. Returns an AlbumPushReport.
    """
    album_dir = album_dir.resolve()
    sd_root = sd_root.resolve()
    artist_seg = segment(artist_name, cfg)
    album_seg = segment(album_name, cfg)
    target_dir = sd_root / ALBUMS_DIR / artist_seg / album_seg
    target_dir.mkdir(parents=True, exist_ok=True)
    report = AlbumPushReport(
        artist=artist_name, album=album_name, target_dir=target_dir,
    )

    # Audio files in the album, sorted by leading track number when the
    # filename starts with one, so progress reads in playback order.
    tracks = sorted(
        (p for p in album_dir.iterdir()
         if p.is_file() and p.suffix.lower() in _AUDIO_EXTS),
        key=_track_sort_key,
    )

    expected_targets: set[Path] = set()
    total = len(tracks)

    for index, src in enumerate(tracks, start=1):
        if cancel_check and cancel_check():
            return report
        target = target_dir / src.name
        expected_targets.add(target)

        if _up_to_date(src, target):
            report.skipped_up_to_date.append(target)
            status = "skipped"
        else:
            shutil.copy2(src, target)
            report.copied.append(target)
            status = "copied"

        # Carry the .lrc sidecar alongside (same soft-fail pattern as
        # src.playlist after the v0.1.5 hotfix).
        lrc_src = src.with_suffix(".lrc")
        lrc_dst = target.with_suffix(".lrc")
        if lrc_src.is_file():
            expected_targets.add(lrc_dst)
            if not _up_to_date(lrc_src, lrc_dst):
                try:
                    shutil.copy2(lrc_src, lrc_dst)
                except OSError:
                    report.lrc_failed.append(lrc_dst)

        if progress_callback:
            progress_callback(index, total, status, src.name)

    # Album cover: prefer cover.jpg, fall back to cover.jpeg / .png /
    # folder.jpg. Single image per album folder.
    for candidate in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg"):
        cover_src = album_dir / candidate
        if cover_src.is_file():
            cover_dst = target_dir / "cover.jpg"
            if (not cover_dst.exists()
                    or cover_dst.stat().st_size != cover_src.stat().st_size):
                try:
                    shutil.copy2(cover_src, cover_dst)
                    report.cover_written = True
                except OSError:
                    pass
            expected_targets.add(cover_dst)
            break

    if prune:
        for existing in target_dir.iterdir():
            if existing.is_file() and existing not in expected_targets:
                try:
                    existing.unlink()
                    report.pruned.append(existing)
                except OSError:
                    pass

    return report


def _up_to_date(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return False
    try:
        s = src.stat()
        d = dst.stat()
    except OSError:
        return False
    return s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) < 1.0


_TRACK_PREFIX_RE = __import__("re").compile(r"^\s*(\d+)\s*[-_.]")


def _track_sort_key(p: Path) -> tuple:
    """Sort by leading "NN - " when present, else lexicographic."""
    m = _TRACK_PREFIX_RE.match(p.stem)
    track = int(m.group(1)) if m else float("inf")
    return (track, p.name.lower())
