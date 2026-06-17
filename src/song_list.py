"""Parse the downloader's input file.

Each non-blank, non-comment line is one song request. Three forms accepted:

    Artist - Title
    Artist - Album - Title
    # Comment lines start with hash and are skipped

The 3-field form lets you pin the album when MusicBrainz might otherwise
guess wrong (e.g., a track that appears on both a studio album and a
greatest-hits compilation — we prefer studio, but you can override).

UTF-8 only. Em dashes and en dashes are *not* accepted as separators —
people paste those in by accident and the resulting search queries are
unrecognisable. We document plain ASCII '-' as the only separator.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SongRequest:
    artist: str
    title: str
    album: str | None = None   # explicit album when user gave 3-field form
    line_no: int = 0           # 1-indexed source line for error reporting


def parse(path: Path) -> list[SongRequest]:
    """Read a song-list file. Raises FileNotFoundError on missing files
    and ValueError with the offending line number on malformed lines."""
    text = path.read_text(encoding="utf-8")
    out: list[SongRequest] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(" - ")]
        if len(parts) == 2:
            artist, title = parts
            out.append(SongRequest(artist=artist, title=title, line_no=i))
        elif len(parts) == 3:
            artist, album, title = parts
            out.append(SongRequest(artist=artist, album=album,
                                   title=title, line_no=i))
        else:
            raise ValueError(
                f"{path}:{i}: expected 'Artist - Title' or "
                f"'Artist - Album - Title', got: {raw!r}"
            )
    return out
