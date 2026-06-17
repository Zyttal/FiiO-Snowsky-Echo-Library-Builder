"""Locate ffmpeg across packaging modes.

Search order:
1. Bundled binary next to the running executable (PyInstaller bundle case).
2. PATH (`shutil.which`).
3. Known per-OS install locations (Homebrew, /usr/local, Program Files).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def find_ffmpeg() -> Path | None:
    """Return a path to a usable ffmpeg, or None if not found."""
    bundled = _bundled_binary()
    if bundled and bundled.exists() and os.access(bundled, os.X_OK):
        return bundled

    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path)

    for candidate in _known_install_paths():
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate

    return None


def install_hint() -> str:
    """One-paragraph message telling the user how to install ffmpeg on their OS."""
    if sys.platform.startswith("win"):
        return (
            "ffmpeg was not found. The easiest path on Windows is winget:\n"
            "    winget install --id=Gyan.FFmpeg -e\n"
            "Or download a static build from https://www.gyan.dev/ffmpeg/builds/ "
            "and add its bin/ folder to your PATH."
        )
    if sys.platform == "darwin":
        return (
            "ffmpeg was not found. Install via Homebrew:\n"
            "    brew install ffmpeg\n"
            "Or download a static build from https://evermeet.cx/ffmpeg/ and place "
            "it in /usr/local/bin/."
        )
    return (
        "ffmpeg was not found. Install via your package manager:\n"
        "    Debian/Ubuntu:  sudo apt install ffmpeg\n"
        "    Fedora:         sudo dnf install ffmpeg\n"
        "    Arch:           sudo pacman -S ffmpeg"
    )


def _bundled_binary() -> Path | None:
    exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    # PyInstaller: bundled resources live next to sys.executable in one-file mode
    # (extracted to _MEIPASS), and in the same folder in one-folder mode.
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / exe_name)
        candidates.append(Path(meipass) / "ffmpeg" / exe_name)
    candidates.append(Path(sys.executable).parent / exe_name)
    candidates.append(Path(sys.executable).parent / "ffmpeg" / exe_name)
    for c in candidates:
        if c.exists():
            return c
    return None


def _known_install_paths() -> list[Path]:
    if sys.platform.startswith("win"):
        return [
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        ]
    if sys.platform == "darwin":
        return [
            Path("/opt/homebrew/bin/ffmpeg"),
            Path("/usr/local/bin/ffmpeg"),
        ]
    return [
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ]
