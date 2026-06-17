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
- **Playlists (folder-as-playlist)** — since the Echo can read folder
  structures but not M3U, "playlists" are physical folders at
  `<SD>/Playlists/<Name>/` with sequentially-numbered tracks. CLI
  (`playlist add / remove / list / push`) and a Playlists GUI tab. A
  track can be in multiple playlists; each membership becomes a real
  file copy on the SD card (FAT32/exFAT lack hardlinks/symlinks). Push
  is incremental with prune-on-removal.
- **Library deletion** — right-click a track, album, or artist in the
  Library tab to delete it from disk. Bulk "Empty library…" button with
  two-step confirmation wipes every Artist folder under the loaded
  root, preserving the manifest, FiiO info files, and Playlists/. Only
  ever operates on the path you've loaded — the Echo's internal flash
  is unreachable from the host anyway.
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
