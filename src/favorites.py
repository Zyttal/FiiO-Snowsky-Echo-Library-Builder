"""Favorites: write playlists to the Echo's SD card, read them back where
the device's storage format permits.

The Echo reads `.m3u` / `.m3u8` playlists at the root of the SD card. We
always write a CRLF-terminated, relative-path UTF-8 M3U — that's the format
FiiO firmwares universally accept and what the device's playlist UI surfaces.

Reading favorites back off the device is best-effort: as of 2026-06, FiiO
does not publish where the on-device "Add to Favorites" list lives. We probe
the SD card for plausible locations (hidden FiiO folders, SQLite, plain
playlists) and return whatever we can find. If nothing matches, callers
should fall back to "push-only" — generate the M3U from manifest favorites
and let the user re-favorite from the playlist on-device.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

PLAYLIST_NAME = "Favorites.m3u"

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
    `sd_root`, with forward slashes (FiiO accepts both; forward keeps the
    file portable). Returns the written path."""
    sd_root = sd_root.resolve()
    playlist_path = sd_root / name
    lines = ["#EXTM3U"]
    for track in tracks:
        track = track.resolve()
        try:
            rel = track.relative_to(sd_root)
        except ValueError:
            # Track lives outside the SD card root — skip rather than write
            # an absolute path that won't resolve on the device.
            continue
        lines.append(str(rel).replace("\\", "/"))
    playlist_path.write_text(
        "\r\n".join(lines) + "\r\n", encoding="utf-8", newline=""
    )
    return playlist_path


def read_device_favorites(sd_root: Path) -> list[Path]:
    """Return absolute paths of tracks the device considers favorited.

    Three probes, in order:
      1. Any .m3u/.m3u8 named like 'favorite*' at the SD root or hidden dirs.
      2. SQLite databases under known FiiO hidden dirs — look for tables/cols
         named like 'favorite', 'starred', 'rating'.
      3. Plain-text lists named 'favorites.txt' or 'starred.txt'.

    Returns [] if nothing is found. Never raises on a malformed source;
    skip-and-continue instead.
    """
    sd_root = sd_root.resolve()
    if not sd_root.exists():
        return []

    found: list[Path] = []
    found.extend(_probe_m3u(sd_root))
    found.extend(_probe_sqlite(sd_root))
    found.extend(_probe_textlist(sd_root))

    # Dedupe while preserving order.
    seen: set[Path] = set()
    out: list[Path] = []
    for p in found:
        if p not in seen and p.exists():
            seen.add(p)
            out.append(p)
    return out


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
    out: list[Path] = []
    for d in _candidate_dirs(sd_root):
        for ext in ("m3u", "m3u8"):
            for pl in d.glob(f"*.{ext}"):
                if "favorite" in pl.stem.lower() or "starred" in pl.stem.lower():
                    out.extend(_parse_m3u(pl, sd_root))
    return out


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
    """Look in hidden dirs for *.db / *.sqlite with plausible favorites
    tables. Read-only — never writes."""
    out: list[Path] = []
    for d in _candidate_dirs(sd_root):
        if d == sd_root:
            continue
        for db_path in list(d.glob("*.db")) + list(d.glob("*.sqlite")):
            out.extend(_read_sqlite_favorites(db_path, sd_root))
    return out


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
    out: list[Path] = []
    for d in _candidate_dirs(sd_root):
        for name in ("favorites.txt", "starred.txt"):
            f = d / name
            if not f.is_file():
                continue
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    track = Path(line)
                    if not track.is_absolute():
                        track = (sd_root / track).resolve()
                    out.append(track)
            except OSError:
                continue
    return out
