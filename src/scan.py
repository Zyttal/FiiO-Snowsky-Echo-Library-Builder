"""Walk a source music tree and emit per-file work items.

A WorkItem captures everything downstream needs: the source FLAC path, its
detected disc number (if any), and the source folder's cover.jpg.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DISC_DIR_RE = re.compile(r"^disc\s*(\d+)$", re.IGNORECASE)
_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape"}


@dataclass(frozen=True)
class WorkItem:
    source: Path
    source_album_dir: Path
    disc_no: int | None
    cover: Path | None

    @property
    def album_folder_name(self) -> str:
        return self.source_album_dir.name


def _disc_from_dirname(name: str) -> int | None:
    m = _DISC_DIR_RE.match(name.strip())
    return int(m.group(1)) if m else None


def _find_cover(folder: Path) -> Path | None:
    for candidate in ("cover.jpg", "cover.jpeg", "cover.png",
                      "folder.jpg", "folder.jpeg", "folder.png"):
        p = folder / candidate
        if p.is_file():
            return p
    return None


def discover(source_root: Path, only: str | None = None) -> list[WorkItem]:
    """Find all audio files under source_root.

    Two modes, auto-detected:

    1. **Library mode** (default): source_root holds multiple album
       folders. Iterate each top-level subdirectory as a separate album.
    2. **Single-album mode**: source_root *is* the album folder — it
       contains Disc N/ subdirs OR audio files directly. Treat
       source_root itself as the album. This is what happens when the
       GUI's Source field is pointed at one specific folder, e.g. a
       multi-disc compilation. Without this branch, the per-disc subdirs
       would each be misread as separate albums.

    Recognizes nested Disc N/ subfolders as one album, recording disc_no.
    Cover art comes from the album folder (one level above Disc N) when
    present; otherwise from the Disc folder itself.

    `only` is a case-insensitive substring match against the album
    folder name. In single-album mode it filters against source_root's
    own name.
    """
    items: list[WorkItem] = []
    source_root = source_root.resolve()

    direct_disc_dirs = [
        d for d in source_root.iterdir()
        if d.is_dir() and _disc_from_dirname(d.name)
    ]
    direct_audio_files = [
        f for f in source_root.iterdir()
        if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
    ]
    if direct_disc_dirs or direct_audio_files:
        # Single-album mode: source_root is the album folder.
        if only and only.lower() not in source_root.name.lower():
            return items
        _scan_album(source_root, items)
        return items

    # Library mode: iterate top-level subdirs as album folders.
    for top in sorted(p for p in source_root.iterdir() if p.is_dir()):
        # The output should never include the output dir itself if it's
        # a sibling.
        if top.name.startswith("Echo-Library"):
            continue
        if only and only.lower() not in top.name.lower():
            continue
        _scan_album(top, items)

    return items


def _scan_album(album_dir: Path, items: list[WorkItem]) -> None:
    """Append every audio file under `album_dir` to `items`, detecting
    Disc N/ subfolders for disc_no tagging."""
    album_cover = _find_cover(album_dir)
    disc_dirs = [
        d for d in album_dir.iterdir()
        if d.is_dir() and _disc_from_dirname(d.name)
    ]
    if disc_dirs:
        for disc_dir in sorted(disc_dirs,
                               key=lambda d: _disc_from_dirname(d.name) or 0):
            dno = _disc_from_dirname(disc_dir.name)
            cover = album_cover or _find_cover(disc_dir)
            for f in sorted(disc_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in _AUDIO_EXTS:
                    items.append(WorkItem(f, album_dir, dno, cover))
    else:
        for f in sorted(album_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in _AUDIO_EXTS:
                items.append(WorkItem(f, album_dir, None, album_cover))
