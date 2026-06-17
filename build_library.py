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
        if strategy.ext == "flac":
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
@click.option("--mirror", type=click.Choice(["none", "flac", "mp3", "dsd"]),
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
            target = layout.target_path(
                out_root, src_tags, cfg, strategy.ext,
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
    """Read favorites off the device or push manifest favorites to the SD card."""


@favorites.command("push")
@click.option("--output", "output_dir", type=click.Path(exists=True, file_okay=False,
              path_type=Path), required=True,
              help="Output library root (also the SD card root if running from the card).")
@click.option("--sd-root", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="SD card root if different from --output. Defaults to --output.")
@click.option("--format", "fmt", type=click.Choice(list(STRATEGIES.keys())), default="flac",
              show_default=True, help="Pick favorites from this format's tree.")
@click.option("--name", default="Favorites.m3u", show_default=True,
              help="Playlist filename written at the SD card root.")
def favorites_push(output_dir, sd_root, fmt, name):
    """Write a Favorites.m3u from manifest-marked tracks to the SD card root."""
    from src.favorites import write_playlist

    output_dir = output_dir.resolve()
    sd_root = (sd_root or output_dir).resolve()
    manifest = Manifest(output_dir / MANIFEST_NAME)
    favs = manifest.favorites(fmt=fmt)
    if not favs:
        click.echo(f"No favorites recorded for format '{fmt}'. "
                   "Mark tracks via the GUI's Library tab first.")
        return
    tracks = [Path(e.target) for e in favs]
    written = write_playlist(sd_root, tracks, name=name)
    click.echo(f"Wrote {len(tracks)} tracks to {written}.")
    skipped = sum(1 for t in tracks if sd_root not in t.resolve().parents)
    if skipped:
        click.echo(f"  ({skipped} tracks live outside --sd-root and were skipped.)",
                   err=True)


@favorites.command("pull")
@click.option("--sd-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
              required=True, help="Mounted SD card root.")
def favorites_pull(sd_root):
    """Best-effort: list what the Echo considers favorited on the SD card."""
    from src.favorites import read_device_favorites

    tracks = read_device_favorites(sd_root.resolve())
    if not tracks:
        click.echo("No favorites found on the SD card. The Echo may keep them in "
                   "internal flash only; use `favorites push` to seed an M3U.")
        return
    click.echo(f"Found {len(tracks)} favorite tracks:")
    for t in tracks:
        click.echo(f"  {t}")


if __name__ == "__main__":
    cli()
