"""On-disk manifest for incremental re-runs.

Stored at <output_root>/.echo-library-manifest.json. Keyed by
(source_path, format), value is {source_mtime, source_size, target_path}.

Re-running with no changes is O(scan) — no ffmpeg, no mutagen, no Pillow.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

MANIFEST_NAME = ".echo-library-manifest.json"


@dataclass
class Entry:
    source: str
    fmt: str
    target: str
    source_mtime: float
    source_size: int


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
        self._entries[self._key(source, fmt)] = Entry(
            source=str(source),
            fmt=fmt,
            target=str(target),
            source_mtime=st.st_mtime,
            source_size=st.st_size,
        )

    def forget(self, source: Path, fmt: str) -> Entry | None:
        return self._entries.pop(self._key(source, fmt), None)

    def all_entries(self) -> list[Entry]:
        return list(self._entries.values())

    def orphans(self) -> list[Entry]:
        """Entries whose source no longer exists."""
        return [e for e in self._entries.values() if not Path(e.source).exists()]
