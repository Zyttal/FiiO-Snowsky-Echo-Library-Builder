"""Filename and path-segment sanitization for the FiiO Snowsky Echo.

The Echo's parser chokes on certain characters (`&`, quotes, FAT-illegal chars).
We strip or remap them, collapse whitespace, and clamp segment length.
"""
from __future__ import annotations

import re
import unicodedata

from .config import Config

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_DOTS_RE = re.compile(r"\.+$")


def segment(text: str, cfg: Config) -> str:
    """Sanitize one path segment (artist OR album OR title — no slashes).

    - Replace `&` with the configured word so 'AC/DC & Friends' stays readable
    - Strip every char in cfg.forbidden_chars
    - Drop quotes (single, double, smart quotes) outright
    - Normalize Unicode to NFC so the FAT/exFAT layer sees consistent bytes
    - Collapse whitespace, trim trailing dots (FAT illegal), clamp length
    """
    if not text:
        return "Unknown"

    s = unicodedata.normalize("NFC", text)
    s = s.replace("&", f" {cfg.ampersand_replacement} ")

    for ch in cfg.forbidden_chars + "'\"‘’“”":
        s = s.replace(ch, "")

    s = _WHITESPACE_RE.sub(" ", s).strip()
    s = _TRAILING_DOTS_RE.sub("", s).strip()

    if len(s) > cfg.max_segment_length:
        s = s[: cfg.max_segment_length].rstrip()

    return s or "Unknown"


def track_filename(track_no: int | None, title: str, ext: str, cfg: Config) -> str:
    """Build a `NN - Title.ext` filename. NN defaults to 00 when unknown.

    Zero-padded to 2 digits — the Echo sorts lexicographically by filename
    within a folder, so '02 - x.flac' must sort before '10 - x.flac'.
    """
    n = int(track_no) if track_no else 0
    return f"{n:02d} - {segment(title, cfg)}.{ext.lstrip('.')}"
