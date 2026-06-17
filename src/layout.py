"""Resolve the target path for one source track.

Output tree shape:
  <root>/<Artist>/<Album [(Disc N)]>/<NN - Title>.<ext>

The Echo doesn't sort by DISCNUMBER tag — only by folder path then filename —
so multi-disc albums must be flattened with the disc number baked into the
folder name. But we only apply the suffix when an album ACTUALLY spans
multiple discs in the resolved library; otherwise a stray DISCNUMBER=1 tag
would litter every single-disc album with a useless "(Disc 1)" suffix.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .sanitize import segment, track_filename
from .tags import SourceTags


def _artist_album(tags: SourceTags, cfg: Config) -> tuple[str, str]:
    return (
        segment(tags.album_artist or tags.artist, cfg),
        segment(tags.album, cfg),
    )


def discs_per_album(items_with_tags) -> dict[tuple[str, str], set[int]]:
    """Build a (artist, album) -> {disc_no, ...} index. None becomes 1 for
    indexing purposes only — actual rendering uses the original disc_no."""
    out: dict[tuple[str, str], set[int]] = {}
    for cfg, tags in items_with_tags:
        key = _artist_album(tags, cfg)
        out.setdefault(key, set()).add(tags.disc_no or 1)
    return out


def target_path(
    output_root: Path,
    tags: SourceTags,
    cfg: Config,
    ext: str,
    multi_disc: bool = False,
) -> Path:
    artist, album = _artist_album(tags, cfg)
    if multi_disc and tags.disc_no:
        album = f"{album} (Disc {tags.disc_no})"
        if len(album) > cfg.max_segment_length:
            album = album[: cfg.max_segment_length]
    fname = track_filename(tags.track_no, tags.title, ext, cfg)
    return output_root / artist / album / fname
