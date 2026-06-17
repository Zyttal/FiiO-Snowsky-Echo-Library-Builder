#!/usr/bin/env python3
"""echo-library-builder — convert and sort a music library for the FiiO Snowsky Echo.

Usage:
    ./build_library.py build --source /mnt/games/Music --output /mnt/games/Music/Echo-Library
    ./build_library.py build --format mp3 --mirror none --source ... --output ...
    ./build_library.py build --mirror mp3 --source ... --output ...
    ./build_library.py verify --output ...
    ./build_library.py status --output ...
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import click
from tqdm import tqdm

from src import config as config_mod
from src import cover, layout, scan, tags
from src.convert import STRATEGIES, Strategy, check_ffmpeg
from src.manifest import MANIFEST_NAME, Manifest


@dataclass(frozen=True)
class Job:
    work: scan.WorkItem
    strategy_name: str
    target: Path
    src_tags: "tags.SourceTags"


def _process_one(job_payload: dict) -> dict:
    """Worker: convert one source -> one target. Runs in a subprocess.

    Accepts a plain dict so it pickles cleanly across the pool boundary.
    """
    source = Path(job_payload["source"])
    target = Path(job_payload["target"])
    strategy_name = job_payload["strategy"]
    cover_path = Path(job_payload["cover"]) if job_payload["cover"] else None
    cfg_dict = job_payload["cfg"]
    src_tags_dict = job_payload["tags"]

    cfg = config_mod.Config(**cfg_dict)
    strategy = STRATEGIES[strategy_name]
    src_tags = tags.SourceTags(**src_tags_dict)

    try:
        strategy.run(source, target, cfg)
        picture_bytes = cover.render(cover_path, cfg)
        # In preserve mode the target extension varies per source, so
        # dispatch the tag writer by the actual target suffix rather than
        # by strategy.ext. write_tags returns False when the format has
        # no tag writer (DSF, APE, raw WAV) — caller is fine with that
        # since folder layout + filename still keep them browseable.
        if strategy_name == "preserve":
            tags.write_tags(target, src_tags, picture_bytes)
        elif strategy.ext == "flac":
            tags.write_flac(target, src_tags, picture_bytes)
        elif strategy.ext == "mp3":
            tags.write_mp3(target, src_tags, picture_bytes)
        # DSD/.dsf has no widely-supported tag block; rely on folder layout.
        if picture_bytes:
            cover.write_external(picture_bytes, target.parent)
        return {"ok": True, "target": str(target), "source": str(source),
                "strategy": strategy_name}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "target": str(target), "source": str(source),
                "strategy": strategy_name, "error": str(e)}


def _selected_strategies(primary: str, mirror: str) -> list[Strategy]:
    out: list[Strategy] = [STRATEGIES[primary]]
    if mirror and mirror != "none" and mirror != primary:
        out.append(STRATEGIES[mirror])
    return out


@click.group()
def cli():
    """Build and maintain a FiiO Snowsky Echo music library."""


@cli.command()
@click.option("--source", "source_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Source root containing album folders.")
@click.option("--output", "output_dir", type=click.Path(file_okay=False, path_type=Path),
              required=True,
              help="Destination root. Per-format subfolders are created here.")
@click.option("--format", "primary_format",
              type=click.Choice(list(STRATEGIES.keys())), default="flac",
              show_default=True, help="Primary output format.")
@click.option("--mirror", type=click.Choice(["none", "preserve", "flac", "mp3", "dsd"]),
              default="none", show_default=True,
              help="Additionally produce a mirror tree in this format.")
@click.option("--only", default=None,
              help="Process only album folders whose name contains this substring.")
@click.option("--as-compilation", "compilation_match", default=None,
              help="Treat source folders whose name contains this substring as a "
                   "single compilation: all tracks land under 'Various Artists/<folder>/' "
                   "instead of being dispersed by their ARTIST tag. The track ALBUMARTIST "
                   "is set to 'Various Artists', and the original ARTIST is preserved in "
                   "the track title (e.g. '03 - The Beatles - Yesterday.flac').")
@click.option("--force", is_flag=True,
              help="Re-convert everything, ignoring the manifest.")
@click.option("--prune", is_flag=True,
              help="Delete target files whose source no longer exists.")
@click.option("--dry-run", is_flag=True,
              help="Print planned actions and counts without writing anything.")
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              default=None, help="Path to config.yaml (defaults to ./config.yaml).")
@click.option("--workers", type=int, default=None,
              help="Parallel workers (default: CPU count - 1).")
def build(source_dir, output_dir, primary_format, mirror, only, compilation_match,
          force, prune, dry_run, config_path, workers):
    """Convert and reorganize a music library for the Echo."""
    if not dry_run:
        check_ffmpeg()

    cfg_path = config_path or Path(__file__).parent / "config.yaml"
    cfg = config_mod.load(cfg_path)
    if workers:
        cfg.workers = workers

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(output_dir / MANIFEST_NAME)

    items = scan.discover(source_dir.resolve(), only=only)
    if not items:
        click.echo("No audio files found.", err=True)
        sys.exit(1)

    strategies = _selected_strategies(primary_format, mirror)
    click.echo(f"Source: {source_dir}  ({len(items)} files)")
    click.echo(f"Output: {output_dir}")
    click.echo(f"Formats: {', '.join(s.name for s in strategies)} "
               f"(primary: {primary_format})")

    # First pass: read every track's tags once, build the multi-disc index.
    item_tags: list[tuple[scan.WorkItem, tags.SourceTags]] = []
    compilation_counters: dict[tuple[str, int | None], int] = {}
    for item in items:
        t = tags.read_source(item.source, item.album_folder_name, item.disc_no)
        if t.genre is None and cfg.default_genre:
            t.genre = cfg.default_genre
        if compilation_match and compilation_match.lower() in item.album_folder_name.lower():
            # Preserve the original artist in the title so the Echo still shows it.
            original_artist = t.artist
            t.title = f"{original_artist} - {t.title}"
            t.artist = "Various Artists"
            t.album_artist = "Various Artists"
            t.album = item.album_folder_name
            # Source folder structure (Disc N/) is authoritative in compilation mode,
            # not the per-track DISCNUMBER tag (which points at the original album).
            t.disc_no = item.disc_no
            # Renumber sequentially per (compilation, disc) so the Echo plays in
            # source-folder order rather than every track being '01'.
            key = (item.album_folder_name, t.disc_no)
            compilation_counters[key] = compilation_counters.get(key, 0) + 1
            t.track_no = compilation_counters[key]
        item_tags.append((item, t))
    disc_index = layout.discs_per_album([(cfg, t) for _, t in item_tags])
    multi_disc_albums = {k for k, v in disc_index.items() if len(v) > 1}

    jobs: list[Job] = []
    skipped = 0
    for item, src_tags in item_tags:
        for strategy in strategies:
            is_primary = strategy.name == primary_format
            out_root = strategy.output_root(output_dir, is_primary)
            artist_album = layout._artist_album(src_tags, cfg)
            # In preserve mode the extension is decided per source.
            ext = (strategy.decide_ext(item.source)
                   if hasattr(strategy, "decide_ext")
                   else strategy.ext)
            target = layout.target_path(
                out_root, src_tags, cfg, ext,
                multi_disc=artist_album in multi_disc_albums,
            )
            if not force and manifest.is_current(item.source, strategy.name, target):
                skipped += 1
                continue
            jobs.append(Job(item, strategy.name, target, src_tags))

    click.echo(f"Plan: {len(jobs)} to (re)convert, {skipped} up-to-date.")

    if dry_run:
        for j in jobs[:50]:
            click.echo(f"  [{j.strategy_name}] {j.work.source.name} -> {j.target}")
        if len(jobs) > 50:
            click.echo(f"  ... and {len(jobs) - 50} more")
        return

    error_count = 0
    if jobs:
        error_count = _run_jobs(jobs, items, strategies, cfg, manifest, output_dir)

    if prune:
        _prune_orphans(manifest)

    manifest.save()
    if error_count:
        click.echo(f"Done with {error_count} errors.")
        sys.exit(1)
    click.echo("Done.")


def _run_jobs(jobs, items, strategies, cfg, manifest, output_dir):
    payloads = []
    for j in jobs:
        payloads.append({
            "source": str(j.work.source),
            "target": str(j.target),
            "strategy": j.strategy_name,
            "cover": str(j.work.cover) if j.work.cover else None,
            "cfg": cfg.__dict__,
            "tags": j.src_tags.__dict__,
        })

    errors = []
    with ProcessPoolExecutor(max_workers=cfg.resolved_workers()) as pool:
        futures = {pool.submit(_process_one, p): p for p in payloads}
        with tqdm(total=len(futures), desc="Converting", unit="file") as bar:
            for fut in as_completed(futures):
                result = fut.result()
                if result["ok"]:
                    manifest.record(Path(result["source"]), result["strategy"],
                                    Path(result["target"]))
                else:
                    errors.append(result)
                bar.update(1)

    if errors:
        click.echo(f"\n{len(errors)} errors:", err=True)
        for e in errors[:10]:
            click.echo(f"  {Path(e['source']).name}: {e['error']}", err=True)
        if len(errors) > 10:
            click.echo(f"  ... and {len(errors) - 10} more", err=True)
    return len(errors)


def _prune_orphans(manifest: Manifest) -> int:
    removed = 0
    for entry in manifest.orphans():
        t = Path(entry.target)
        try:
            if t.exists():
                t.unlink()
                removed += 1
            manifest.forget(Path(entry.source), entry.fmt)
            # Clean up empty parent directories up to the format root
            parent = t.parent
            while parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            pass
    if removed:
        click.echo(f"Pruned {removed} orphaned files.")
    return removed


@cli.command()
@click.option("--source", "source_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Source root that was used to produce the output tree.")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Output library root that was built but has no manifest "
                   "(or a stale one).")
@click.option("--format", "primary_format",
              type=click.Choice(list(STRATEGIES.keys())), default="flac",
              show_default=True, help="Primary output format used by the build.")
@click.option("--as-compilation", "compilation_match", default=None,
              help="If the build used --as-compilation, pass the same substring.")
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              default=None, help="Path to config.yaml (defaults to ./config.yaml).")
def reconcile(source_dir, output_dir, primary_format, compilation_match, config_path):
    """Rebuild the manifest from an already-converted output tree.

    Walks the source the same way `build` would, computes the target path
    each file would have landed at, and for any target that physically
    exists on disk, records a manifest entry. Useful when a previous run
    (e.g. an earlier GUI build) converted files but didn't save the
    manifest — without it the Device, Playlists, and incremental rebuild
    paths all think nothing exists.
    """
    cfg_path = config_path or Path(__file__).parent / "config.yaml"
    cfg = config_mod.load(cfg_path)
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    manifest = Manifest(output_dir / MANIFEST_NAME)
    items = scan.discover(source_dir)

    strategy = STRATEGIES[primary_format]
    out_root = strategy.output_root(output_dir, is_primary=True)

    # Same compilation-handling pass as build()
    item_tags = []
    comp_counters: dict[tuple[str, int | None], int] = {}
    for item in items:
        t = tags.read_source(item.source, item.album_folder_name, item.disc_no)
        if t.genre is None and cfg.default_genre:
            t.genre = cfg.default_genre
        if compilation_match and compilation_match.lower() in item.album_folder_name.lower():
            original_artist = t.artist
            t.title = f"{original_artist} - {t.title}"
            t.artist = "Various Artists"
            t.album_artist = "Various Artists"
            t.album = item.album_folder_name
            t.disc_no = item.disc_no
            key = (item.album_folder_name, t.disc_no)
            comp_counters[key] = comp_counters.get(key, 0) + 1
            t.track_no = comp_counters[key]
        item_tags.append((item, t))

    disc_index = layout.discs_per_album([(cfg, t) for _, t in item_tags])
    multi_disc = {k for k, v in disc_index.items() if len(v) > 1}

    recorded = 0
    missing = 0
    for item, src_tags in item_tags:
        ext = (strategy.decide_ext(item.source)
               if hasattr(strategy, "decide_ext") else strategy.ext)
        artist_album = layout._artist_album(src_tags, cfg)
        target = layout.target_path(
            out_root, src_tags, cfg, ext,
            multi_disc=artist_album in multi_disc,
        )
        if target.exists():
            manifest.record(item.source, strategy.name, target)
            recorded += 1
        else:
            missing += 1

    manifest.save()
    click.echo(f"Reconciled: {recorded} entries recorded, "
               f"{missing} planned targets not present on disk.")


@cli.command()
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True)
def status(output_dir):
    """Summarize what's currently in the output library."""
    output_dir = output_dir.resolve()
    manifest = Manifest(output_dir / MANIFEST_NAME)
    entries = manifest.all_entries()
    by_fmt: dict[str, int] = {}
    for e in entries:
        by_fmt[e.fmt] = by_fmt.get(e.fmt, 0) + 1
    click.echo(f"Manifest: {output_dir / MANIFEST_NAME}")
    click.echo(f"Tracked entries: {len(entries)}")
    for k, n in sorted(by_fmt.items()):
        click.echo(f"  {k}: {n}")
    orphans = manifest.orphans()
    if orphans:
        click.echo(f"Orphans (source missing): {len(orphans)} — run `build --prune` to remove.")


@cli.command()
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True)
def verify(output_dir):
    """Spot-check the output tree for Echo-friendliness."""
    from mutagen.flac import FLAC

    output_dir = output_dir.resolve()
    issues = []
    n = 0
    missing_genre = 0
    missing_genre_albums: dict[str, int] = {}
    for flac_path in output_dir.rglob("*.flac"):
        n += 1
        f = FLAC(flac_path)
        t = f.tags or {}
        if not t.get("ARTIST") or not t.get("ALBUM") or not t.get("TITLE"):
            issues.append(f"missing core tags: {flac_path}")
        if not t.get("GENRE"):
            missing_genre += 1
            album_key = str(flac_path.parent.relative_to(output_dir))
            missing_genre_albums[album_key] = missing_genre_albums.get(album_key, 0) + 1
        if f.info.bits_per_sample > 16:
            issues.append(f"bit depth > 16: {flac_path}")
        if f.info.sample_rate > 96000:
            issues.append(f"sample rate > 96k: {flac_path}")
        for p in f.pictures:
            if len(p.data) > 1_500_000:
                issues.append(f"cover > 1.5MB: {flac_path}")
        for forbidden in "&\"'":
            if forbidden in flac_path.name:
                issues.append(f"forbidden char {forbidden!r} in filename: {flac_path}")
    click.echo(f"Verified {n} FLAC files.")
    if n:
        pct = 100 * missing_genre / n
        click.echo(f"Genre coverage: {n - missing_genre}/{n} tagged "
                   f"({missing_genre} missing, {pct:.1f}%).")
        if missing_genre:
            worst = sorted(missing_genre_albums.items(), key=lambda kv: -kv[1])[:5]
            click.echo("Top albums missing GENRE:")
            for album, count in worst:
                click.echo(f"  {count:4d}  {album}")
    if not issues:
        click.echo("All checks passed.")
    else:
        click.echo(f"{len(issues)} issues:")
        for i in issues[:30]:
            click.echo(f"  {i}")
        if len(issues) > 30:
            click.echo(f"  ... and {len(issues) - 30} more")


@cli.command()
@click.option("--list", "list_path",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True,
              help="Text file with one song per line: 'Artist - Title' or "
                   "'Artist - Album - Title' (Album form recommended for "
                   "accurate MusicBrainz matches).")
@click.option("--dest", "dest_root",
              type=click.Path(file_okay=False, path_type=Path),
              required=True,
              help="Source library root. Each download lands as a FLAC "
                   "under <dest>/<Album> - <Artist>/, ready for `build`.")
@click.option("--config", "config_path", type=click.Path(path_type=Path),
              default=None, help="Path to config.yaml (defaults to ./config.yaml).")
@click.option("--skip-existing/--overwrite", default=True, show_default=True,
              help="Skip songs whose target file already exists.")
def download(list_path, dest_root, config_path, skip_existing):
    """Download a list of songs from YouTube, enrich tags via MusicBrainz,
    and drop them into the source library so `build` can pick them up."""
    if not dest_root.exists():
        dest_root.mkdir(parents=True, exist_ok=True)
    dest_root = dest_root.resolve()

    cfg_path = config_path or Path(__file__).parent / "config.yaml"
    cfg = config_mod.load(cfg_path)

    from src.downloader import download_song
    from src.song_list import parse as parse_song_list

    try:
        requests = parse_song_list(list_path)
    except ValueError as e:
        click.echo(str(e), err=True)
        sys.exit(2)

    if not requests:
        click.echo("Empty song list — nothing to do.", err=True)
        return

    click.echo(f"Downloading {len(requests)} songs to {dest_root}")

    ok = err = skipped = 0
    for req in requests:
        prefix = f"  [{req.line_no:03d}] {req.artist} - {req.title}"
        if skip_existing and _already_present(dest_root, req, cfg):
            click.echo(f"{prefix}   SKIP (exists)")
            skipped += 1
            continue
        click.echo(f"{prefix} ...")
        result = download_song(
            artist=req.artist, title=req.title, album_hint=req.album,
            source_root=dest_root, cfg=cfg, line_no=req.line_no,
        )
        if result.ok:
            ok += 1
            target_rel = result.target.relative_to(dest_root) if result.target else "?"
            click.echo(f"        -> {target_rel}")
            for n in (result.notes or []):
                click.echo(f"           ({n})")
        else:
            err += 1
            click.echo(f"        FAIL: {result.error}", err=True)
            for n in (result.notes or []):
                click.echo(f"              ({n})", err=True)

    click.echo(f"Done. {ok} downloaded, {skipped} skipped, {err} failed.")
    if err:
        sys.exit(1)


def _already_present(dest_root, request, cfg) -> bool:
    """Best-effort check: scan <dest_root>/<* - request.artist>/ for any
    filename containing the title."""
    needle = request.title.lower()
    artist = request.artist.lower()
    for album_dir in dest_root.iterdir():
        if not album_dir.is_dir():
            continue
        if artist not in album_dir.name.lower():
            continue
        for f in album_dir.iterdir():
            if f.suffix.lower() == ".flac" and needle in f.stem.lower():
                return True
    return False


@cli.group()
def favorites():
    """Export favorites to an SD-card backup file, or probe the card for any.

    Heads up: FiiO has stated the Snowsky Echo's chip can't play M3U
    playlists — it's a hardware limit, not a firmware feature pending.
    `push` is therefore a pure backup/export format. It's still useful:
    FiiO's firmware install instructions warn that flashing a new
    firmware "may first format the internal memory" — where the on-device
    Favorites list lives — so the exported .m3u preserves your curated
    list across firmware upgrades, and can be read by any other M3U-aware
    player on a phone/PC.
    """


@favorites.command("push")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Output library root (also the SD card root if running from the card).")
@click.option("--sd-root", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="SD card root if different from --output. Defaults to --output.")
@click.option("--format", "fmt", type=click.Choice(list(STRATEGIES.keys())), default="flac",
              show_default=True, help="Pick favorites from this format's tree.")
@click.option("--name", default="Favorites.m3u", show_default=True,
              help="Backup filename written at the SD card root.")
def favorites_push(output_dir, sd_root, fmt, name):
    """Export manifest-marked favorites as a CRLF M3U on the SD card root.

    Backup-only — the Echo's chip can't play M3U (per FiiO). The file is
    a portable list of relative paths, useful for restoring favorites
    manually after a firmware wipe, or reading on any other device.
    """
    from src.favorites import EmptyPlaylistError, write_playlist

    output_dir = output_dir.resolve()
    sd_root = (sd_root or output_dir).resolve()
    manifest = Manifest(output_dir / MANIFEST_NAME)
    favs = manifest.favorites(fmt=fmt)
    if not favs:
        click.echo(f"No favorites recorded for format '{fmt}'. "
                   "Mark tracks via the GUI's Library tab first.")
        return
    tracks = [Path(e.target) for e in favs]
    try:
        written = write_playlist(sd_root, tracks, name=name, lib_root=output_dir)
    except EmptyPlaylistError as e:
        click.echo(
            f"ERROR: {e.skipped} favorited tracks all live outside "
            f"--output ({output_dir}).\n"
            "The M3U would have zero entries — refusing to write it.\n"
            "Make sure --output points at the local Echo-Library root that "
            "contains your manifest, not at the SD card mount.",
            err=True,
        )
        sys.exit(2)
    n = len(tracks)
    skipped = sum(1 for t in tracks if output_dir not in t.resolve().parents)
    click.echo(f"Exported {n - skipped}/{n} tracks to {written}.")
    click.echo("(Backup format — the Echo's chip cannot play M3U directly.)")
    if skipped:
        click.echo(
            f"  ({skipped} tracks live outside --output and were skipped.)",
            err=True,
        )


@favorites.command("pull")
@click.option("--sd-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              required=True, help="Mounted SD card root.")
def favorites_pull(sd_root):
    """Best-effort: list what the Echo considers favorited on the SD card."""
    from src.favorites import read_device_favorites_report

    report = read_device_favorites_report(sd_root.resolve())

    if not report.any_source_found:
        click.echo(
            "No .m3u found on the SD card. The Echo has no MTP mode, so "
            "its on-device Favorites list (in internal flash) is unreachable "
            "— `favorites pull` only surfaces files we (or another tool) "
            "have already written to the card. Use `favorites push` to "
            "export one."
        )
        return

    sources = []
    if report.m3u_files:
        sources.append(f"{len(report.m3u_files)} M3U")
    if report.sqlite_files:
        sources.append(f"{len(report.sqlite_files)} sqlite")
    if report.text_files:
        sources.append(f"{len(report.text_files)} text list")
    src_str = ", ".join(sources)

    if not report.tracks:
        click.echo(
            f"Found {src_str} on the card but no track entries inside. "
            "Almost always: a previous `favorites push` ran with --sd-root "
            "pointed at a folder that didn't contain your library, so every "
            "track got skipped and the M3U is header-only. Copy the library "
            "to the card and re-run `push`."
        )
        for p in report.m3u_files + report.sqlite_files + report.text_files:
            click.echo(f"  ({p})")
        return

    click.echo(f"Found {len(report.tracks)} favorite tracks across {src_str}:")
    for t in report.tracks:
        suffix = "  (missing on card)" if t in report.tracks_missing else ""
        click.echo(f"  {t}{suffix}")
    if report.tracks_missing:
        click.echo(
            f"({len(report.tracks_missing)} referenced tracks aren't physically "
            "on the card — either delete the m3u entry or rsync the library again.)"
        )


@cli.group()
def playlist():
    """Folder-as-playlist support: copy tracks into <SD>/Playlists/<Name>/.

    The Echo can't play M3U (chip limit) but its Folder browse mode shows
    any directory of audio as a playable group. These commands manage
    membership in the manifest and copy the resulting files onto the SD
    card with sequential numbering so the device plays them in order.

    A song can be in multiple playlists; on FAT32/exFAT each membership
    is a physical file copy (no hardlinks/symlinks). Disk overhead is
    small in practice — a 30-track playlist is ~150 MB.
    """


@playlist.command("add")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Output library root (where the manifest lives).")
@click.option("--name", required=True, help="Playlist name.")
@click.option("--track", "tracks", multiple=True, type=click.Path(path_type=Path),
              required=True,
              help="Target FLAC path in the library. Repeat for multiple tracks.")
def playlist_add(output_dir, name, tracks):
    """Add one or more tracks (by their output path) to a playlist."""
    manifest = Manifest(output_dir.resolve() / MANIFEST_NAME)
    added = 0
    missing = 0
    for t in tracks:
        if manifest.add_to_playlist(Path(t).resolve(), name):
            added += 1
        else:
            missing += 1
    manifest.save()
    click.echo(f"Added {added} tracks to '{name}'"
               + (f"; {missing} not in manifest" if missing else "") + ".")


@playlist.command("remove")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True)
@click.option("--name", required=True, help="Playlist name.")
@click.option("--track", "tracks", multiple=True, type=click.Path(path_type=Path),
              required=True)
def playlist_remove(output_dir, name, tracks):
    """Remove tracks from a playlist (does not delete files)."""
    manifest = Manifest(output_dir.resolve() / MANIFEST_NAME)
    removed = 0
    for t in tracks:
        if manifest.remove_from_playlist(Path(t).resolve(), name):
            removed += 1
    manifest.save()
    click.echo(f"Removed {removed} tracks from '{name}'.")


@playlist.command("list")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True)
@click.option("--name", default=None,
              help="If given, list the tracks in this playlist. Otherwise list all playlists.")
def playlist_list(output_dir, name):
    """List playlists (or the contents of one)."""
    manifest = Manifest(output_dir.resolve() / MANIFEST_NAME)
    if name:
        entries = manifest.playlist_entries(name, fmt="flac")
        if not entries:
            click.echo(f"Playlist '{name}' is empty or doesn't exist.")
            return
        click.echo(f"{len(entries)} tracks in '{name}':")
        for e in entries:
            click.echo(f"  {e.target}")
        return
    names = manifest.playlist_names()
    if not names:
        click.echo("No playlists yet. Use `playlist add` to start one.")
        return
    for n in names:
        count = len(manifest.playlist_entries(n, fmt="flac"))
        click.echo(f"  {n}  ({count} tracks)")


@playlist.command("push")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Output library root (manifest source).")
@click.option("--sd-root", type=click.Path(file_okay=False, path_type=Path),
              required=True,
              help="Mounted SD card root. Playlists land in <sd-root>/Playlists/.")
@click.option("--name", default=None,
              help="Push this playlist only. Omit to push every playlist in the manifest.")
@click.option("--prune/--no-prune", default=True, show_default=True,
              help="Delete stale tracks on the card that the playlist no longer contains.")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None,
              help="Path to config.yaml (defaults to ./config.yaml).")
def playlist_push(output_dir, sd_root, name, prune, config_path):
    """Copy playlist tracks to the SD card as folders the Echo can browse."""
    from src.playlist import push_playlist

    output_dir = output_dir.resolve()
    sd_root = sd_root.resolve()
    sd_root.mkdir(parents=True, exist_ok=True)

    cfg_path = config_path or Path(__file__).parent / "config.yaml"
    cfg = config_mod.load(cfg_path)

    manifest = Manifest(output_dir / MANIFEST_NAME)
    targets = [name] if name else manifest.playlist_names()
    if not targets:
        click.echo("No playlists in the manifest. Use `playlist add` first.")
        return

    for pl in targets:
        entries = manifest.playlist_entries(pl, fmt="flac")
        if not entries:
            click.echo(f"  '{pl}': empty, skipped.")
            continue
        tracks = [Path(e.target) for e in entries]
        report = push_playlist(pl, tracks, sd_root, cfg, prune=prune)
        click.echo(
            f"  '{pl}' -> {report.target_dir}: "
            f"{len(report.copied)} copied, "
            f"{len(report.skipped_up_to_date)} up-to-date, "
            f"{len(report.pruned)} pruned"
            + (f", cover.jpg written" if report.cover_written else "")
        )
        if report.missing_sources:
            click.echo(
                f"    ({len(report.missing_sources)} source tracks missing — "
                "build them first or remove from the playlist.)",
                err=True,
            )


if __name__ == "__main__":
    cli()
