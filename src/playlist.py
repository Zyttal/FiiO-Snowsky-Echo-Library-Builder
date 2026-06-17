"""Folder-as-playlist support for the Snowsky Echo.

The Echo can't play M3U (chip limit), but its Folder browse mode happily
shows any directory of audio files. So a "playlist" here is a physical
folder at <SD-card>/Playlists/<Name>/ holding sequentially-numbered copies
of every track marked in that playlist.

A single song can appear in many playlists. On FAT32/exFAT (the only
filesystems the Echo can read) hardlinks and symlinks don't exist, so
each appearance is a real file copy. That's fine in practice — a 30-track
playlist costs ~150 MB on disk, trivial on a 256 GB card.

Sync semantics:
- `push` is idempotent. We compare (mtime, size) against what's already
  on the card and only copy what's changed, missing, or new — same trick
  as the build pipeline's manifest check.
- Stale tracks (files on the card that the manifest no longer marks as
  members) get pruned when the caller passes `prune=True`.
- The first track's cover.jpg is dropped in the playlist folder so the
  Echo's folder view shows artwork.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .sanitize import segment, track_filename

PLAYLISTS_DIR = "Playlists"


@dataclass
class PushReport:
    playlist: str
    target_dir: Path
    copied: list[Path] = field(default_factory=list)
    skipped_up_to_date: list[Path] = field(default_factory=list)
    pruned: list[Path] = field(default_factory=list)
    missing_sources: list[Path] = field(default_factory=list)
    cover_written: bool = False


def push_playlist(
    playlist_name: str,
    source_tracks: list[Path],
    sd_root: Path,
    cfg: Config,
    prune: bool = True,
    progress_callback=None,
    cancel_check=None,
) -> PushReport:
    """Copy every track in `source_tracks` to
    <sd_root>/Playlists/<sanitized_name>/NN - Title.flac, renumbering
    sequentially in the order received.

    `progress_callback`, when supplied, is invoked as
    callback(index, total, status, target_name) after each track is
    processed. `status` is one of "copied", "skipped", "missing", and
    `target_name` is the on-card filename so the GUI can show it.

    `cancel_check`, when supplied, is a callable that returns True if
    the push should abort. Checked before each track copy.

    Returns a PushReport summarising what changed."""
    sd_root = sd_root.resolve()
    folder_name = segment(playlist_name, cfg)
    target_dir = sd_root / PLAYLISTS_DIR / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    report = PushReport(playlist=playlist_name, target_dir=target_dir)

    expected_targets: set[Path] = set()
    total = len(source_tracks)

    for index, src in enumerate(source_tracks, start=1):
        if cancel_check and cancel_check():
            return report
        if not src.exists():
            report.missing_sources.append(src)
            if progress_callback:
                progress_callback(index, total, "missing", src.name)
            continue
        # Strip the leading "NN - " from the source filename so we can
        # re-number it for this playlist's order. If the source doesn't
        # follow the convention, take the stem as-is.
        title = _title_from_source(src.stem)
        ext = src.suffix.lstrip(".") or "flac"
        target_name = track_filename(index, title, ext, cfg)
        target = target_dir / target_name
        expected_targets.add(target)

        if _up_to_date(src, target):
            report.skipped_up_to_date.append(target)
            status = "skipped"
        else:
            shutil.copy2(src, target)
            report.copied.append(target)
            status = "copied"
        if progress_callback:
            progress_callback(index, total, status, target_name)

    # Drop a cover.jpg from the first source's parent if one exists.
    if source_tracks:
        cover_src = _find_first_cover(source_tracks)
        if cover_src is not None:
            cover_dst = target_dir / "cover.jpg"
            if not cover_dst.exists() or cover_dst.stat().st_size != cover_src.stat().st_size:
                shutil.copy2(cover_src, cover_dst)
                report.cover_written = True
        expected_targets.add(target_dir / "cover.jpg")

    if prune:
        for existing in target_dir.iterdir():
            if existing.is_file() and existing not in expected_targets:
                existing.unlink()
                report.pruned.append(existing)

    return report


def _title_from_source(stem: str) -> str:
    """Strip a leading `NN - ` from a filename so we can re-number cleanly
    for this playlist's order. Falls back to the whole stem when the
    pattern doesn't match."""
    parts = stem.split(" - ", 1)
    if len(parts) == 2 and parts[0].strip().isdigit():
        return parts[1].strip()
    return stem


def _up_to_date(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return False
    try:
        s = src.stat()
        d = dst.stat()
    except OSError:
        return False
    return s.st_size == d.st_size and abs(s.st_mtime - d.st_mtime) < 1.0


def _find_first_cover(tracks: list[Path]) -> Path | None:
    """Look for cover.jpg alongside the first track that has one."""
    seen: set[Path] = set()
    for t in tracks:
        parent = t.parent
        if parent in seen:
            continue
        seen.add(parent)
        cover = parent / "cover.jpg"
        if cover.is_file():
            return cover
    return None


def remove_playlist(playlist_name: str, sd_root: Path, cfg: Config) -> int:
    """Delete <sd_root>/Playlists/<name>/ and its contents. Returns the
    number of files removed."""
    sd_root = sd_root.resolve()
    folder_name = segment(playlist_name, cfg)
    target_dir = sd_root / PLAYLISTS_DIR / folder_name
    if not target_dir.is_dir():
        return 0
    n = 0
    for f in target_dir.rglob("*"):
        if f.is_file():
            f.unlink()
            n += 1
    target_dir.rmdir()
    return n
