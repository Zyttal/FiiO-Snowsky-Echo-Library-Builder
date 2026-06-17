# echo-library-builder

Convert and sort a music library into a layout the **FiiO Snowsky Echo** will
scan, tag, and play correctly. Re-runs are incremental — drop new albums into
the source folder and re-run to process only what changed.

> **Just want to run it?** See [QUICKSTART.md](QUICKSTART.md) for a step-by-step
> walkthrough. The rest of this file explains *why* each step exists.

## Why this exists

The FiiO Snowsky Echo is a small portable music player with a 2.39" color
screen and dual CS43198 DACs. It officially supports FLAC up to 24-bit/192 kHz,
DSD256, plus MP3/M4A/OGG/APE/WAV. In practice, plenty of perfectly-spec'd FLAC
files fail to play. The known culprits — none documented in the manual — are:

| Quirk | What goes wrong | What this tool does |
| --- | --- | --- |
| ID3v2 inside FLAC | Echo wants Vorbis comments only; ID3 in FLAC trips the parser | Strips ID3, writes clean Vorbis tags |
| Embedded cover art > 500x500 | Indexer slows or stalls during scan | Resizes covers to ≤500x500 baseline JPEG |
| `&`, quotes, smart quotes in filenames | Tracks silently skip during playback | Replaces `&` with `and`, strips quotes |
| Nested `Disc 1/Disc 2/` folders | Device sorts by folder then filename — not by DISCNUMBER tag — so order breaks | Flattens to `Album (Disc 1)/`, `Album (Disc 2)/` |
| Track number as `3/12` | Some firmware versions display it literally | Writes bare `TRACKNUMBER=3` |
| 24-bit / high-sample-rate FLAC | EQ silently disables above 16-bit | Downconverts to 16-bit / 44.1 kHz by default |
| Source FLACs missing the GENRE tag | Echo shows "Unknown" for every track | Optional `default_genre` config fallback fills in a value when the source has none |
| Favorites kept only inside the device | No way to back up or restore them | `Favorites.m3u` push from the GUI/CLI seeds a playlist the Echo reads |

Everything is configurable; the defaults match what the device prefers.

## Prerequisites

- **ffmpeg** on PATH: `sudo apt install ffmpeg`
- **pyenv** with Python 3.11.15 available

## Setup

The pyenv virtualenv `echo-library` (on Python 3.11.15) is already created and
auto-activates when you `cd` into this directory (driven by `.python-version`).

```bash
cd /mnt/games/Music/echo-library-builder
pyenv exec python -m pip install -r requirements.txt    # one-time
```

If you ever need to re-create the venv from scratch:

```bash
pyenv virtualenv 3.11.15 echo-library
pyenv exec python -m pip install -r requirements.txt
```

## Usage

All commands assume you are inside `/mnt/games/Music/echo-library-builder` so
the `.python-version` pin activates automatically. Use `pyenv exec python` (or
just `./build_library.py` since the file is executable) to invoke the script.

### Build (default: FLAC 16/44.1)

```bash
pyenv exec python build_library.py build \
    --source /mnt/games/Music \
    --output /mnt/games/Music/Echo-Library \
    --as-compilation "Playlist"
```

This walks every album folder under `--source`, skips the `.zip` files and the
`Echo-Library*` output siblings, and writes a clean tree to
`<output>/<Artist>/<Album>/NN - Title.flac`. The `--as-compilation` flag is
optional — see *Compilation / playlist folders* below.

**Verified run on this library**: 390 FLAC files, 30.9 GB source → 11 GB
output, 2 minutes 41 seconds on 11 workers. 0 errors, all 390 pass `verify`.

### Optional formats

```bash
# Smaller MP3 instead of FLAC
./build_library.py build --format mp3 ...

# DSD64 (.dsf) — experimental, doesn't improve over PCM source
./build_library.py build --format dsd ...

# Lossless FLAC primary + MP3 mirror tree in the same pass
./build_library.py build --mirror mp3 ...
```

Each format writes to its own subdir under `--output`: `Echo-Library/`,
`Echo-Library-MP3/`, `Echo-Library-DSD/`.

### Incremental re-runs

Re-running with the same arguments is a near-instant no-op when nothing has
changed. A manifest at `<output>/.echo-library-manifest.json` tracks the
(source mtime, size) of every converted track. Drop a new album folder in the
source, re-run, and only that album gets processed.

```bash
./build_library.py status --output /mnt/games/Music/Echo-Library
./build_library.py build --source ... --output ... --prune    # remove orphans
./build_library.py build --source ... --output ... --force    # rebuild all
./build_library.py build --source ... --output ... --only "Undertow"
./build_library.py build --source ... --output ... --dry-run
```

### Verify

```bash
./build_library.py verify --output /mnt/games/Music/Echo-Library
```

Spot-checks tag presence, bit depth, sample rate, cover size, and filename
safety. Useful before copying to the SD card.

### Compilation / playlist folders

If a source folder is really a multi-artist mixtape (e.g. a "Classic Rock"
playlist), use `--as-compilation <substring>` so all matching tracks land
under `Various Artists/<folder>/` with the original artist preserved in each
track title and sequential numbering per disc:

```bash
./build_library.py build --source ... --output ... --as-compilation "Playlist"
```

Without the flag, the tool disperses tracks to their tagged ARTIST folders
(useful if you want artist-based browsing on the Echo).

### Download (YouTube → MusicBrainz → source tree)

Hand the tool a text file of songs and it will fetch each from YouTube,
look up canonical metadata on MusicBrainz, re-encode as FLAC with clean
Vorbis tags and embedded cover art, and land it in your source library so
the next `build` picks it up.

```bash
./build_library.py download --list songs.txt --dest /mnt/games/Music/
```

Input format — one song per line. The 3-field form pins the album, which
materially improves MusicBrainz matches for popular tracks (which often
appear on dozens of compilations):

```
# echo-library-builder/songs.txt
Pink Floyd - The Dark Side of the Moon - Time
The Beatles - Help! - Yesterday
TOOL - Ænima - Stinkfist
Radiohead - Karma Police
```

The downloader uses the existing `default_genre` config as a fallback
when MusicBrainz has no genre tag for the recording, so the Echo never
sees "Unknown" for downloaded tracks.

**Copyright note**: downloading commercial audio from YouTube is against
their ToS and the legality varies by jurisdiction. This tool exists for
personal-library use on the user's own device; how you use it is on you.

### Favorites

The Echo's on-device "Add to Favorites" list lives in internal flash and isn't
exposed on the SD card in any documented format. This tool gives you a way
around that: mark tracks favorite in the GUI (Library tab) and push them as
a CRLF M3U playlist the device reads.

```bash
# Push manifest favorites to <SD card>/Favorites.m3u
./build_library.py favorites push --output /mnt/games/Music/Echo-Library \
    --sd-root /media/$USER/ECHO/

# Best-effort: read favorites back off the card (probes hidden FiiO dirs,
# SQLite, plain playlists). Returns nothing if favorites are flash-only.
./build_library.py favorites pull --sd-root /media/$USER/ECHO/
```

### GUI

A PySide6 desktop GUI ships in `gui/`. Same job pipeline as the CLI, three
tabs (Build / Library / Device).

```bash
pyenv exec python -m gui
```

For non-Python users, build a standalone installer per OS:

```bash
packaging/build_linux.sh        # AppImage in dist/
packaging/build_macos.sh        # .dmg in dist/
./packaging/build_windows.ps1   # .exe in dist\ (PowerShell)
```

Each bundles a static ffmpeg so end-users don't need to install anything.
The macOS `.app` is unsigned; first-time users must right-click → Open.

### Cutting a release

The release workflow at `.github/workflows/release.yml` builds the three
installers on native runners and attaches them to a GitHub Release when
a `v*.*.*` tag is pushed.

```bash
# Update CHANGELOG.md with the v0.2.0 section first
git tag v0.2.0
git push origin v0.2.0
```

The release body is auto-filled from the matching section in
`CHANGELOG.md`; if no entry exists for the tag, a placeholder is used.

### Tests

```bash
pyenv exec python -m pytest tests/ -v
```

## Output structure (actual)

```
Echo-Library/
├── .echo-library-manifest.json
├── TOOL/
│   ├── Ænima/
│   │   ├── cover.jpg                          (500x500 baseline JPEG)
│   │   ├── 01 - Stinkfist.flac                (16-bit / 44.1 kHz)
│   │   ├── 02 - Eulogy.flac
│   │   └── … 15 tracks total
│   └── Undertow/
│       ├── cover.jpg
│       ├── 01 - Intolerance.flac
│       └── … 10 tracks
├── The Beatles/
│   └── Anthology Highlights/
│       └── … 32 tracks
└── Various Artists/
    ├── Classic Rock Classics - Playlist (Disc 1)/    326 tracks renumbered 01..326
    ├── Classic Rock Classics - Playlist (Disc 2)/    5 tracks
    └── Classic Rock Classics - Playlist (Disc 3)/    2 tracks
```

Every output FLAC has only these Vorbis comments (everything else stripped):
`ARTIST`, `ALBUM`, `TITLE`, `TRACKNUMBER`, `DISCNUMBER`, `DATE`, `ALBUMARTIST`
— plus one embedded JPEG cover at the same dimensions as the folder's
`cover.jpg`.

## SD card preparation

The Echo handles both FAT32 and exFAT on recent firmware. If you want maximum
compatibility (especially older firmware or older Echo Mini), format FAT32.
Beyond ~5000 files on one card, library indexing slows noticeably.

```bash
# Example: copy the FLAC tree to the SD card mountpoint.
# --delete is optional and only safe if the SD card is dedicated to this output.
rsync -av --progress --delete \
    /mnt/games/Music/Echo-Library/ \
    /media/$USER/ECHO/Music/
```

Don't copy the `.echo-library-manifest.json` to the card — the Echo will just
ignore it, but you can exclude it cleanly with `--exclude '.echo-library-manifest.json'`.

## Configuration

Edit `config.yaml` to change defaults. Every field is optional; missing values
fall back to defaults in `src/config.py`.

```yaml
target_sample_rate: 44100
target_bit_depth: 16
flac_compression_level: 5
mp3_quality: 0              # LAME V0 (~245 kbps VBR)
max_cover_size_px: 500
workers: null               # null = CPU count - 1
```

## Layout

```
echo-library-builder/
├── README.md
├── .python-version
├── requirements.txt
├── config.yaml
├── build_library.py        # CLI
├── src/
│   ├── config.py           # defaults + YAML loading
│   ├── scan.py             # source-tree walker, disc detection
│   ├── tags.py             # mutagen: read/write Vorbis + ID3v2.3
│   ├── convert.py          # ffmpeg strategies (FLAC / MP3 / DSD)
│   ├── cover.py            # Pillow: resize cover.jpg, in-memory cache
│   ├── layout.py           # compute Artist/Album/NN - Title.flac
│   ├── sanitize.py         # filename-safe transforms
│   ├── favorites.py        # M3U write + best-effort device probe
│   ├── song_list.py        # parse the downloader's input file
│   ├── musicbrainz.py      # canonical tag enrichment
│   ├── downloader.py       # yt-dlp + MB + tag write, all in one
│   └── manifest.py         # JSON manifest for incremental re-runs
├── gui/                    # PySide6 desktop GUI (python -m gui)
│   ├── main.py             # QApplication + tabbed main window
│   ├── download_tab.py     # song-list picker + per-song status
│   ├── build_tab.py        # source/output pickers + per-file progress
│   ├── library_tab.py      # tree view + favorite toggle column
│   ├── device_tab.py       # SD card picker + push/pull favorites
│   ├── workers.py          # QRunnable wrapper around the CLI's job pipeline
│   └── ffmpeg_probe.py     # locate bundled or system ffmpeg
└── packaging/              # one-shot installer builds per OS
    ├── pyinstaller.spec
    ├── build_linux.sh      # → dist/echo-library-builder-x86_64.AppImage
    ├── build_macos.sh      # → dist/echo-library-builder.dmg
    └── build_windows.ps1   # → dist\echo-library-builder.exe
```

## Troubleshooting

**`ffmpeg failed: ... Conversion failed`** — usually a corrupted source file.
Run ffprobe on the offending track to inspect.

**Tracks still skip on the Echo** — run `verify` first. If everything passes,
check the SD card filesystem (some Echo units misread NTFS).

**Library scan takes forever** — keep per-card file count under ~5000. Either
split across cards by artist range, or prune to fewer albums.
