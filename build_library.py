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
    for flac_path in output_dir.rglob("*.flac"):
        n += 1
        f = FLAC(flac_path)
        t = f.tags or {}
        if not t.get("ARTIST") or not t.get("ALBUM") or not t.get("TITLE"):
            issues.append(f"missing core tags: {flac_path}")
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
    if not issues:
        click.echo("All checks passed.")
    else:
        click.echo(f"{len(issues)} issues:")
        for i in issues[:30]:
            click.echo(f"  {i}")
        if len(issues) > 30:
            click.echo(f"  ... and {len(issues) - 30} more")


if __name__ == "__main__":
    cli()
