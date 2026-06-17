# Quickstart — using echo-library-builder

A step-by-step walkthrough. For the *why* behind each step, see [README.md](README.md).

---

## 0. One-time prerequisites (already done on this machine)

You already have all of these — listed for completeness if you ever set this up elsewhere.

```bash
sudo apt install ffmpeg                          # audio conversion engine
pyenv install 3.11.15                            # if not present
pyenv virtualenv 3.11.15 echo-library            # the project venv
```

## 1. Open the project

```bash
cd /mnt/games/Music/echo-library-builder
```

That's it for activation — `.python-version` tells pyenv to use the `echo-library` venv automatically when you're inside this folder.

## 2. Install Python dependencies (one time only)

```bash
pyenv exec python -m pip install -r requirements.txt
```

This is already done. You only re-run it if `requirements.txt` ever changes.

## 3. Preview what will happen (always safe, writes nothing)

```bash
pyenv exec python build_library.py build \
    --source /mnt/games/Music \
    --output /mnt/games/Music/Echo-Library \
    --as-compilation "Playlist" \
    --dry-run
```

You'll see something like:

```
Source: /mnt/games/Music  (390 files)
Output: /mnt/games/Music/Echo-Library
Formats: flac (primary: flac)
Plan: 380 to (re)convert, 10 up-to-date.
  [flac] 01 - ... -> /mnt/games/Music/Echo-Library/...
  ... and N more
```

Use this any time you want to sanity-check before a real run.

## 4. Build the library for real

```bash
pyenv exec python build_library.py build \
    --source /mnt/games/Music \
    --output /mnt/games/Music/Echo-Library \
    --as-compilation "Playlist"
```

What this does:

- Walks every album folder under `--source` (ignores the `.zip` files)
- Converts each FLAC down to 16-bit / 44.1 kHz
- Writes clean Vorbis tags (strips the ID3/Tidal garbage)
- Resizes embedded covers to 500×500 baseline JPEG and also drops a `cover.jpg` per album
- Sanitizes filenames (no `&`, no quotes, no slashes)
- Groups the Classic Rock playlist under `Various Artists/` with sequential numbering (because of `--as-compilation "Playlist"`)

On your library this takes about **2½ minutes** and produces ~11 GB of output. A progress bar will keep you company.

**Want to skip the compilation grouping?** Drop the `--as-compilation "Playlist"` flag — tracks scatter into per-artist folders instead.

## 5. Verify before copying to the SD card

```bash
pyenv exec python build_library.py verify \
    --output /mnt/games/Music/Echo-Library
```

You should see:

```
Verified 390 FLAC files.
All checks passed.
```

If anything fails, the script lists the offending files so you can investigate.

## 6. Copy to your Echo's SD card

Plug in the card, find its mountpoint (usually shown by `lsblk` or in your file manager), then:

```bash
rsync -av --progress --delete \
    --exclude '.echo-library-manifest.json' \
    /mnt/games/Music/Echo-Library/ \
    /media/$USER/ECHO/Music/
```

Replace `/media/$USER/ECHO/Music/` with your actual SD card path. The `--delete` flag removes files on the card that aren't in the source — only safe if the card is dedicated to this library. Drop it if you want additive copies.

**Card format tip**: FAT32 is safest for the Echo. exFAT works on recent firmware but FAT32 is universal.

## 7. Adding new music later (the whole point of incremental mode)

```bash
# 1. Drop a new album folder into /mnt/games/Music/ (any name)
# 2. Re-run the exact same build command:

pyenv exec python build_library.py build \
    --source /mnt/games/Music \
    --output /mnt/games/Music/Echo-Library \
    --as-compilation "Playlist"
```

It only processes the new files — the existing 390 are recognized as up-to-date (via the manifest) and skipped instantly. Then re-run step 6 to sync the card.

## Other useful commands

| Want to… | Command |
| --- | --- |
| See what's already in the library | `pyenv exec python build_library.py status --output /mnt/games/Music/Echo-Library` |
| Force a full re-conversion | add `--force` to the build command |
| Remove output for songs you deleted from the source | add `--prune` to the build command |
| Only process one album | add `--only "Undertow"` (substring match on folder name) |
| Make a smaller MP3 mirror alongside the FLAC tree | add `--mirror mp3` — produces `Echo-Library-MP3/` next to `Echo-Library/` |
| Use MP3 instead of FLAC as the primary | add `--format mp3` |
| Write a Favorites.m3u to the SD card | `pyenv exec python build_library.py favorites push --output ... --sd-root /media/$USER/ECHO/` |
| List what the Echo has favorited on the card | `pyenv exec python build_library.py favorites pull --sd-root /media/$USER/ECHO/` |
| Download a song list from YouTube | `pyenv exec python build_library.py download --list songs.txt --dest /mnt/games/Music/` |
| Launch the desktop GUI instead | `pyenv exec python -m gui` |
| Run the tests | `pyenv exec python -m pytest tests/ -v` |

## Downloading songs by name (YouTube → MusicBrainz → source tree)

If you want the tool to fetch new tracks for you rather than only re-package what you've already ripped, write a song list and feed it to the `download` command. Each line is one song. The 3-field form pins the album, which makes MusicBrainz lookups much more accurate:

```
# /mnt/games/Music/wishlist.txt
Pink Floyd - The Dark Side of the Moon - Time
TOOL - Ænima - Stinkfist
Radiohead - Karma Police
```

Then:

```bash
pyenv exec python build_library.py download \
    --list /mnt/games/Music/wishlist.txt \
    --dest /mnt/games/Music/
```

What happens for each line:

1. **MusicBrainz lookup** finds the album, year, genre, track and disc number, album artist, and a cover-art URL.
2. **YouTube search** picks the first result whose duration matches the MusicBrainz duration within ±20 % (rejects "live cover" mistakes).
3. **yt-dlp + ffmpeg** download the audio and encode it to FLAC.
4. **Tags + cover.jpg** land alongside the file via the same tag-writer the rest of the pipeline uses — clean Vorbis comments only, no ID3-in-FLAC.
5. **Files appear** at `<dest>/<Album> - <Artist>/NN - Title.flac` matching your existing folder convention.

After it finishes, re-run the `build` step from earlier and the new tracks flow straight into your Echo-Library tree.

The GUI exposes the same flow under the **Download** tab.

**Heads up**: downloading commercial audio from YouTube is against their ToS and the legality varies by jurisdiction. This is a personal-library tool — how you use it is on you.

## Launching the GUI

If you'd rather drive everything from a window instead of the terminal:

```bash
pyenv exec python -m gui
```

A single-window app opens with three tabs:

- **Build** — same options as the CLI's `build` command, plus a live per-file progress table. Dry-run first if you want a preview.
- **Library** — tree view of the output. Click the star column to mark a track favorite; the choice is saved in the manifest and survives rebuilds.
- **Device** — point at the SD card, push your favorites as `Favorites.m3u`, or try to pull what the Echo considers favorited (best-effort — FiiO doesn't publish the format).

The GUI reuses the same job pipeline as the CLI — no behavior differences, just a friendlier surface.

## Standalone installers (for people without Python)

If you want to hand this to someone who isn't going to set up pyenv:

```bash
# Linux (produces an AppImage in dist/)
packaging/build_linux.sh

# macOS (produces a .dmg in dist/)
packaging/build_macos.sh

# Windows (run in PowerShell; produces echo-library-builder.exe in dist\)
.\packaging\build_windows.ps1
```

Each script bundles a static ffmpeg, so end-users don't need to install anything else. Build artifacts live in `dist/` and aren't committed. The macOS `.app` is unsigned — first-time users must right-click → Open to bypass Gatekeeper.

## If something goes wrong

| Symptom | Try this |
| --- | --- |
| `ffmpeg not found` | `sudo apt install ffmpeg` |
| Conversion fails on one file | usually a corrupt source — run `ffprobe <file>` to inspect |
| Tracks still skip on the Echo after copying | run `verify` first; if clean, check the SD card filesystem (NTFS won't work) |
| Library scan on the Echo takes forever | keep per-card file count under ~5000 |

That's the whole workflow — `cd`, build, verify, rsync, enjoy.
