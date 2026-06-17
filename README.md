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
└── src/
    ├── config.py           # defaults + YAML loading
    ├── scan.py             # source-tree walker, disc detection
    ├── tags.py             # mutagen: read/write Vorbis + ID3v2.3
    ├── convert.py          # ffmpeg strategies (FLAC / MP3 / DSD)
    ├── cover.py            # Pillow: resize cover.jpg, in-memory cache
    ├── layout.py           # compute Artist/Album/NN - Title.flac
    ├── sanitize.py         # filename-safe transforms
    └── manifest.py         # JSON manifest for incremental re-runs
```

## Troubleshooting

**`ffmpeg failed: ... Conversion failed`** — usually a corrupted source file.
Run ffprobe on the offending track to inspect.

**Tracks still skip on the Echo** — run `verify` first. If everything passes,
check the SD card filesystem (some Echo units misread NTFS).

**Library scan takes forever** — keep per-card file count under ~5000. Either
split across cards by artist range, or prune to fewer albums.
