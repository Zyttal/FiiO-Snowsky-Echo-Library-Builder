# Changelog

Each section is a tagged release. The release workflow (`.github/workflows/release.yml`)
extracts the matching section as the GitHub Release body when a `vX.Y.Z` tag is pushed.

## v0.1.0

First public release. The CLI handles the original Echo-library build
flow, plus three new top-level features:

- **GUI** — PySide6 desktop app (Build, Library, Device, Download tabs).
  Launch with `python -m gui`, or grab the per-OS installer attached to
  this release.
- **Favorites** — mark tracks in the Library tab and export them as a
  `Favorites.m3u` backup on the SD card. FiiO has stated the Echo's chip
  cannot play M3U, so the export is strictly a backup/restore format —
  useful for re-favoriting by hand after a firmware flash reformats
  internal storage (FW V1.3.0 fixed routine library scans from wiping
  Favorites, but the firmware-flash risk remains), or for reading on any
  other M3U-aware player.
- **YouTube downloader** — feed a song list to the Download tab (or
  `./build_library.py download --list ...`); each line is enriched via
  MusicBrainz and landed in the source tree as a tagged FLAC ready for
  `build`.
- **Genre fix** — MP3 mirror now writes a `TCON` frame, and the new
  `default_genre` config keeps the Echo from showing "Unknown" when
  source FLACs lack a `GENRE` Vorbis comment. The pre-existing mutagen
  `audio.tags.delete()` crash that had silently broken the MP3 mirror
  since mutagen 1.46 is fixed.

Installers:

- `echo-library-builder-x86_64.AppImage` — Linux x86_64
- `echo-library-builder.dmg` — macOS (unsigned; right-click → Open the first time)
- `echo-library-builder.exe` — Windows x86_64

Each installer bundles a static `ffmpeg`. No Python install required.
