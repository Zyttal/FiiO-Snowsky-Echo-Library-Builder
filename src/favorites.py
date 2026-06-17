"""Favorites: export the curated list as an .m3u backup on the SD card,
and best-effort probe any existing favorites data the device may have
left there.

Important: FiiO has publicly stated the Snowsky Echo's chip cannot play
M3U playlists (Head-Fi / Reddit threads, mid-2026 — chip-level limit,
not a missing firmware feature). The `.m3u` we write is therefore a
durable BACKUP — useful for restoring favorites by hand when a firmware
flash reformats internal storage (FiiO's install notes warn this may
happen on every update), or for reading on another M3U-aware player.
Note: V1.3.0 (April 2026) fixed routine media-library re-scans from
clearing Favorites, so on V1.3.0+ the only loss-of-favorites risk is a
firmware flash. The device itself won't surface the .m3u.

Format choice: CRLF-terminated, relative-path UTF-8 M3U. Maximally
portable, parses cleanly in foobar2000, Plex, etc.

Reading favorites back off the device is best-effort: FiiO doesn't
publish where the on-device "Add to Favorites" list lives. We probe the
SD card for plausible locations (hidden FiiO folders, SQLite, plain
text lists) and return whatever we can find. As of 2026-06 the favorites
are believed to live in internal flash and not on the card at all, so
this routine usually returns empty — that's expected.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

PLAYLIST_NAME = "Favorites.m3u"


class EmptyPlaylistError(ValueError):
    """Raised when write_playlist would otherwise write a header-only M3U.

    Carries the count of tracks that were skipped (lived outside sd_root)
    so the caller can construct a helpful error message.
    """
    def __init__(self, skipped: int):
        super().__init__(
            f"playlist would be empty — all {skipped} tracks live outside "
            f"the SD card root. Copy the library to the card first."
        )
        self.skipped = skipped


@dataclass
class FavoritesReport:
    """What `read_device_favorites_report` actually found on the card."""
    m3u_files: list[Path] = field(default_factory=list)
    sqlite_files: list[Path] = field(default_factory=list)
    text_files: list[Path] = field(default_factory=list)
    tracks: list[Path] = field(default_factory=list)
    tracks_missing: list[Path] = field(default_factory=list)

    @property
    def any_source_found(self) -> bool:
        return bool(self.m3u_files or self.sqlite_files or self.text_files)

# Hidden directories FiiO firmwares have historically used to store
# device-side state (per Head-Fi/forum reports across the M-series). Order
# matters: most-specific first.
_DEVICE_DIRS = (".fiio", ".snowsky", ".echo", ".local")


def write_playlist(
    sd_root: Path,
    tracks: list[Path],
    name: str = PLAYLIST_NAME,
) -> Path:
    """Write a CRLF M3U at the SD card root. Paths are written relative to
    `sd_root`, with forward slashes. Raises EmptyPlaylistError if none of
    the supplied tracks live under sd_root — writing a header-only M3U is
    a common UX trap (Pull then claims "no favorites found")."""
    sd_root = sd_root.resolve()
    playlist_path = sd_root / name
    lines = ["#EXTM3U"]
    skipped = 0
    for track in tracks:
        track = track.resolve()
        try:
            rel = track.relative_to(sd_root)
        except ValueError:
            # Track lives outside the SD card root — skip rather than write
            # an absolute path that won't resolve on the device.
            skipped += 1
            continue
        lines.append(str(rel).replace("\\", "/"))
    if len(lines) == 1:
        # Only the #EXTM3U header — refuse rather than write a stub file.
        raise EmptyPlaylistError(skipped=skipped)
    playlist_path.write_text(
        "\r\n".join(lines) + "\r\n", encoding="utf-8", newline=""
    )
    return playlist_path


def read_device_favorites(sd_root: Path) -> list[Path]:
    """Backwards-compatible: returns the same list of track paths the GUI
    used to consume. Drops the existence filter that hid the "empty M3U"
    UX trap; callers that want to know which tracks aren't on the card
    should use `read_device_favorites_report` instead."""
    return read_device_favorites_report(sd_root).tracks


def read_device_favorites_report(sd_root: Path) -> FavoritesReport:
    """Structured probe: which source files were found, what they
    reference, which referenced tracks aren't physically on the card.

    Three probes, in order:
      1. Any .m3u/.m3u8 named like 'favorite*' at the SD root or hidden dirs.
      2. SQLite databases under known FiiO hidden dirs.
      3. Plain-text lists named 'favorites.txt' or 'starred.txt'.

    Never raises on a malformed source; skip-and-continue instead.
    """
    report = FavoritesReport()
    sd_root = sd_root.resolve()
    if not sd_root.exists():
        return report

    m3u_files, m3u_tracks = _probe_m3u_detailed(sd_root)
    sqlite_files, sqlite_tracks = _probe_sqlite_detailed(sd_root)
    text_files, text_tracks = _probe_textlist_detailed(sd_root)
    report.m3u_files = m3u_files
    report.sqlite_files = sqlite_files
    report.text_files = text_files

    seen: set[Path] = set()
    for src in (m3u_tracks, sqlite_tracks, text_tracks):
        for p in src:
            if p in seen:
                continue
            seen.add(p)
            report.tracks.append(p)
            if not p.exists():
                report.tracks_missing.append(p)
    return report


def _candidate_dirs(sd_root: Path) -> list[Path]:
    """Places worth probing: the root itself plus any of the known hidden
    FiiO directories that happen to exist."""
    dirs = [sd_root]
    for name in _DEVICE_DIRS:
        candidate = sd_root / name
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def _probe_m3u(sd_root: Path) -> list[Path]:
    return _probe_m3u_detailed(sd_root)[1]


def _probe_m3u_detailed(sd_root: Path) -> tuple[list[Path], list[Path]]:
    """Return (files_found, parsed_track_paths). files_found includes
    even empty M3Us so the caller can distinguish "no file" from "file
    present but empty"."""
    files: list[Path] = []
    tracks: list[Path] = []
    for d in _candidate_dirs(sd_root):
        for ext in ("m3u", "m3u8"):
            for pl in d.glob(f"*.{ext}"):
                if "favorite" in pl.stem.lower() or "starred" in pl.stem.lower():
                    files.append(pl)
                    tracks.extend(_parse_m3u(pl, sd_root))
    return files, tracks


def _parse_m3u(path: Path, sd_root: Path) -> list[Path]:
    out: list[Path] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        track = Path(line)
        if not track.is_absolute():
            track = (sd_root / track).resolve()
        out.append(track)
    return out


def _probe_sqlite(sd_root: Path) -> list[Path]:
    return _probe_sqlite_detailed(sd_root)[1]


def _probe_sqlite_detailed(sd_root: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    tracks: list[Path] = []
    for d in _candidate_dirs(sd_root):
        if d == sd_root:
            continue
        for db_path in list(d.glob("*.db")) + list(d.glob("*.sqlite")):
            files.append(db_path)
            tracks.extend(_read_sqlite_favorites(db_path, sd_root))
    return files, tracks


def _read_sqlite_favorites(db_path: Path, sd_root: Path) -> list[Path]:
    out: list[Path] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return out
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for table in tables:
            if not re.search(r"favor|star|rating", table, re.IGNORECASE):
                continue
            try:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            except sqlite3.Error:
                continue
            path_col = next(
                (c for c in cols if re.search(r"path|file|uri", c, re.IGNORECASE)),
                None,
            )
            if not path_col:
                continue
            try:
                for (val,) in conn.execute(f"SELECT {path_col} FROM {table}"):
                    if not val:
                        continue
                    track = Path(val)
                    if not track.is_absolute():
                        track = (sd_root / track).resolve()
                    out.append(track)
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return out


def _probe_textlist(sd_root: Path) -> list[Path]:
    return _probe_textlist_detailed(sd_root)[1]


def _probe_textlist_detailed(sd_root: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    tracks: list[Path] = []
    for d in _candidate_dirs(sd_root):
        for name in ("favorites.txt", "starred.txt"):
            f = d / name
            if not f.is_file():
                continue
            files.append(f)
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    track = Path(line)
                    if not track.is_absolute():
                        track = (sd_root / track).resolve()
                    tracks.append(track)
            except OSError:
                continue
    return files, tracks
