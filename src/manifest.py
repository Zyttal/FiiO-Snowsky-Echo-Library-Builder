"""On-disk manifest for incremental re-runs.

Stored at <output_root>/.echo-library-manifest.json. Keyed by
(source_path, format), value is {source_mtime, source_size, target_path}.

Re-running with no changes is O(scan) — no ffmpeg, no mutagen, no Pillow.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_NAME = ".echo-library-manifest.json"


@dataclass
class Entry:
    source: str
    fmt: str
    target: str
    source_mtime: float
    source_size: int
    favorite: bool = False
    playlists: list[str] = field(default_factory=list)


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self._entries: dict[str, Entry] = {}
        self._load()

    @staticmethod
    def _key(source: Path, fmt: str) -> str:
        return f"{fmt}::{source}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for k, v in raw.get("entries", {}).items():
            try:
                self._entries[k] = Entry(**v)
            except TypeError:
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "entries": {k: asdict(v) for k, v in self._entries.items()},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def lookup(self, source: Path, fmt: str) -> Entry | None:
        return self._entries.get(self._key(source, fmt))

    def is_current(self, source: Path, fmt: str, target: Path) -> bool:
        """True if the source hasn't changed AND target still exists."""
        e = self.lookup(source, fmt)
        if e is None or not target.exists():
            return False
        try:
            st = source.stat()
        except OSError:
            return False
        return (
            e.source_size == st.st_size
            and abs(e.source_mtime - st.st_mtime) < 1.0
            and Path(e.target) == target
        )

    def record(self, source: Path, fmt: str, target: Path) -> None:
        st = source.stat()
        key = self._key(source, fmt)
        prev = self._entries.get(key)
        self._entries[key] = Entry(
            source=str(source),
            fmt=fmt,
            target=str(target),
            source_mtime=st.st_mtime,
            source_size=st.st_size,
            favorite=prev.favorite if prev else False,
            playlists=list(prev.playlists) if prev else [],
        )

    def forget(self, source: Path, fmt: str) -> Entry | None:
        return self._entries.pop(self._key(source, fmt), None)

    def forget_target(self, target: Path) -> int:
        """Drop every entry whose `target` matches. Returns the number
        removed. The GUI's Library tab thinks in target paths (what it
        renders) rather than (source, fmt) pairs."""
        target_str = str(target)
        keys = [k for k, e in self._entries.items() if e.target == target_str]
        for k in keys:
            del self._entries[k]
        return len(keys)

    def forget_targets_under(self, root: Path) -> int:
        """Drop every entry whose target is under `root`. Used when the
        GUI bulk-deletes an album or artist folder."""
        root_str = str(root.resolve())
        keys = [
            k for k, e in self._entries.items()
            if e.target == root_str or e.target.startswith(root_str + "/")
        ]
        for k in keys:
            del self._entries[k]
        return len(keys)

    def all_entries(self) -> list[Entry]:
        return list(self._entries.values())

    def orphans(self) -> list[Entry]:
        """Entries whose source no longer exists."""
        return [e for e in self._entries.values() if not Path(e.source).exists()]

    def set_favorite(self, target: Path, value: bool) -> bool:
        """Flip favorite on the entry whose target matches `target`. Returns
        True if an entry was updated. Used by the GUI's Library tab when the
        user clicks a star."""
        target_str = str(target)
        for entry in self._entries.values():
            if entry.target == target_str:
                entry.favorite = value
                return True
        return False

    def favorites(self, fmt: str | None = None) -> list[Entry]:
        """All entries marked favorite. Filter by format if given."""
        return [
            e for e in self._entries.values()
            if e.favorite and (fmt is None or e.fmt == fmt)
        ]

    def add_to_playlist(self, target: Path, playlist: str) -> bool:
        """Tag the entry at `target` as belonging to `playlist`. Returns
        True on update. Names are kept verbatim — case and whitespace
        matter, the Echo's folder browser displays them directly."""
        target_str = str(target)
        for entry in self._entries.values():
            if entry.target != target_str:
                continue
            if playlist not in entry.playlists:
                entry.playlists.append(playlist)
                return True
            return False
        return False

    def remove_from_playlist(self, target: Path, playlist: str) -> bool:
        target_str = str(target)
        for entry in self._entries.values():
            if entry.target != target_str:
                continue
            if playlist in entry.playlists:
                entry.playlists.remove(playlist)
                return True
            return False
        return False

    def playlist_entries(
        self, playlist: str, fmt: str | None = None,
    ) -> list[Entry]:
        """All entries in `playlist`. Filter by format if given."""
        return [
            e for e in self._entries.values()
            if playlist in e.playlists and (fmt is None or e.fmt == fmt)
        ]

    def playlist_names(self) -> list[str]:
        """All distinct playlist names present in the manifest, sorted."""
        names: set[str] = set()
        for e in self._entries.values():
            names.update(e.playlists)
        return sorted(names)
