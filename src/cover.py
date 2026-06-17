"""Cover art resizing.

The Echo's library scanner slows dramatically when embedded art exceeds
~500x500. We re-encode every cover as a baseline (not progressive) JPEG,
downsized to the configured ceiling while preserving aspect ratio.

Results are cached in-memory per source path so we don't re-do the same
cover for every track in a 30-track album.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image

from .config import Config

_cache: dict[Path, bytes] = {}


def render(source: Path | None, cfg: Config) -> bytes | None:
    if source is None or not source.is_file():
        return None
    if source in _cache:
        return _cache[source]

    with Image.open(source) as im:
        im = im.convert("RGB")
        im.thumbnail((cfg.max_cover_size_px, cfg.max_cover_size_px), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=cfg.cover_jpeg_quality, optimize=True,
                progressive=False)
        data = buf.getvalue()

    _cache[source] = data
    return data


def write_external(data: bytes, target_album_dir: Path) -> None:
    """Drop a cover.jpg next to the tracks. Idempotent: skip if up-to-date."""
    target_album_dir.mkdir(parents=True, exist_ok=True)
    target = target_album_dir / "cover.jpg"
    if target.exists() and target.read_bytes() == data:
        return
    target.write_bytes(data)
