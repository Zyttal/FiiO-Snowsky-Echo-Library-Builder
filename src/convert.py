"""ffmpeg-driven format conversion strategies.

Each strategy:
- Reads from a source audio file (any of the formats Echo supports as input)
- Writes a target file with the configured rate/depth
- Drops all source metadata (-map_metadata -1) so we can rewrite cleanly later

The output is opened by mutagen *after* ffmpeg runs to attach tags + cover.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config


@dataclass(frozen=True)
class Strategy:
    name: str
    ext: str
    mirror_suffix: str   # appended to output base when used as a mirror tree

    def output_root(self, base: Path, is_primary: bool) -> Path:
        """Primary format goes directly under --output; mirrors get a suffix."""
        if is_primary:
            return base
        return base.with_name(f"{base.name}{self.mirror_suffix}")

    def run(self, source: Path, target: Path, cfg: Config) -> None:
        raise NotImplementedError


class FlacStrategy(Strategy):
    def __init__(self):
        super().__init__("flac", "flac", "")

    def run(self, source: Path, target: Path, cfg: Config) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        sample_fmt = "s16" if cfg.target_bit_depth == 16 else "s32"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-vn",
            "-map", "0:a:0",
            "-af", "aresample=resampler=soxr:precision=28",
            "-ar", str(cfg.target_sample_rate),
            "-sample_fmt", sample_fmt,
            "-compression_level", str(cfg.flac_compression_level),
            "-map_metadata", "-1",
            str(target),
        ]
        _run(cmd)


class Mp3Strategy(Strategy):
    def __init__(self):
        super().__init__("mp3", "mp3", "-MP3")

    def run(self, source: Path, target: Path, cfg: Config) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-vn",
            "-map", "0:a:0",
            "-c:a", "libmp3lame",
            "-q:a", str(cfg.mp3_quality),
            "-ar", "44100",
            "-map_metadata", "-1",
            "-id3v2_version", "3",
            str(target),
        ]
        _run(cmd)


class DsdStrategy(Strategy):
    """Experimental: PCM -> DSD64 (.dsf) via ffmpeg.

    Note: this is up-conversion. It does NOT improve fidelity over the
    source FLAC; it just changes the container. ffmpeg's DSD encoder is
    competent but slow. Output files are ~2-3x larger than the FLAC.
    """
    def __init__(self):
        super().__init__("dsd", "dsf", "-DSD")

    def run(self, source: Path, target: Path, cfg: Config) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-vn",
            "-map", "0:a:0",
            "-c:a", "dsd_lsbf_planar",
            "-ar", str(cfg.dsd_rate),
            "-map_metadata", "-1",
            str(target),
        ]
        _run(cmd)


STRATEGIES: dict[str, Strategy] = {
    "flac": FlacStrategy(),
    "mp3": Mp3Strategy(),
    "dsd": DsdStrategy(),
}


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {' '.join(cmd[:6])}...\n"
            f"stderr: {proc.stderr.strip()[:600]}"
        )


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH. Install it first: sudo apt install ffmpeg"
        )
