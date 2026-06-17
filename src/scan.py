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

    Recognizes nested Disc N/ subfolders as the same album, recording disc_no.
    Cover art is taken from the album folder (one level above Disc N) when
    present; otherwise from the Disc folder itself.

    `only` is a case-insensitive substring match against the album folder name.
    """
    items: list[WorkItem] = []
    source_root = source_root.resolve()

    for top in sorted(p for p in source_root.iterdir() if p.is_dir()):
        # The output should never include the output dir itself if it's a sibling
        if top.name.startswith("Echo-Library"):
            continue
        if only and only.lower() not in top.name.lower():
            continue

        # Scan disc subdirs and direct children
        album_cover = _find_cover(top)
        disc_dirs = [d for d in top.iterdir() if d.is_dir() and _disc_from_dirname(d.name)]

        if disc_dirs:
            for disc_dir in sorted(disc_dirs, key=lambda d: _disc_from_dirname(d.name) or 0):
                dno = _disc_from_dirname(disc_dir.name)
                cover = album_cover or _find_cover(disc_dir)
                for f in sorted(disc_dir.iterdir()):
                    if f.is_file() and f.suffix.lower() in _AUDIO_EXTS:
                        items.append(WorkItem(f, top, dno, cover))
        else:
            for f in sorted(top.iterdir()):
                if f.is_file() and f.suffix.lower() in _AUDIO_EXTS:
                    items.append(WorkItem(f, top, None, album_cover))

    return items
