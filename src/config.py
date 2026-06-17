"""Configuration loading and defaults.

Defaults live here as a single source of truth. config.yaml overrides any of
these; CLI flags override the YAML.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    target_sample_rate: int = 44100
    target_bit_depth: int = 16
    flac_compression_level: int = 5
    mp3_quality: int = 0
    dsd_rate: int = 2_822_400
    max_cover_size_px: int = 500
    cover_jpeg_quality: int = 90
    max_segment_length: int = 80
    forbidden_chars: str = '"<>:|?*/\\'
    ampersand_replacement: str = "and"
    workers: int | None = None
    default_genre: str | None = None
    enrich_tags_via_musicbrainz: bool = False

    def resolved_workers(self) -> int:
        if self.workers is not None:
            return max(1, self.workers)
        return max(1, (os.cpu_count() or 2) - 1)


def load(path: Path | None) -> Config:
    if path is None or not path.exists():
        return Config()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    valid_keys = {f.name for f in Config.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in valid_keys and v is not None}
    return Config(**filtered)
