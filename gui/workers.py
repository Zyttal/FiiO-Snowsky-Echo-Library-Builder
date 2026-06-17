"""Threading layer between the Qt event loop and the CLI's job pipeline.

We deliberately run jobs in a ProcessPoolExecutor (same as the CLI) rather
than QThreadPool — ffmpeg and mutagen are CPU-bound, and the worker function
is already pickle-clean dicts. A single QRunnable supervises the pool from a
background Qt thread and emits per-file signals back to the UI.

The DownloadRunner at the bottom is the opposite shape: yt-dlp + MB are
I/O bound, the MB rate limit makes parallelism pointless, and the work is
inherently sequential. Same QRunnable interface, single-threaded body.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal


@dataclass
class JobSpec:
    """Plain payload for one source -> one target conversion."""
    source: Path
    target: Path
    strategy: str
    cover: Path | None
    src_tags_dict: dict
    cfg_dict: dict

    def as_payload(self) -> dict:
        return {
            "source": str(self.source),
            "target": str(self.target),
            "strategy": self.strategy,
            "cover": str(self.cover) if self.cover else None,
            "cfg": self.cfg_dict,
            "tags": self.src_tags_dict,
        }


class BuildSignals(QObject):
    """Qt signals emitted from the background supervisor."""
    started = Signal(int)                              # total jobs
    file_done = Signal(dict)                           # result dict from _process_one
    finished = Signal(int, int)                        # ok_count, error_count
    cancelled = Signal()


class BuildRunner(QRunnable):
    """Run a list of JobSpecs through a ProcessPoolExecutor on a Qt thread."""

    def __init__(self, jobs: list[JobSpec], workers: int) -> None:
        super().__init__()
        self.jobs = jobs
        self.workers = max(1, workers)
        self.signals = BuildSignals()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from build_library import _process_one  # local import: heavy modules

        payloads = [j.as_payload() for j in self.jobs]
        self.signals.started.emit(len(payloads))

        ok = err = 0
        if not payloads:
            self.signals.finished.emit(ok, err)
            return

        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(_process_one, p) for p in payloads]
            for fut in as_completed(futures):
                if self._cancel:
                    for f in futures:
                        f.cancel()
                    self.signals.cancelled.emit()
                    return
                result = fut.result()
                self.signals.file_done.emit(result)
                if result.get("ok"):
                    ok += 1
                else:
                    err += 1
        self.signals.finished.emit(ok, err)


class PlaylistPushSignals(QObject):
    playlist_done = Signal(dict)        # {name, copied, up_to_date, pruned, missing}
    finished = Signal(int)              # total playlists pushed


class PlaylistPushRunner(QRunnable):
    """Push one or more playlists to the SD card on a background thread.

    Each playlist's copy step is sequential (we're network-I/O-bound on
    SD writes anyway and the per-track shutil.copy2 already saturates a
    typical card)."""

    def __init__(self, library_root: Path, sd_root: Path,
                 names: list[str], cfg_dict: dict) -> None:
        super().__init__()
        self.library_root = library_root
        self.sd_root = sd_root
        self.names = names
        self.cfg_dict = cfg_dict
        self.signals = PlaylistPushSignals()

    def run(self) -> None:
        from src import config as config_mod
        from src.manifest import MANIFEST_NAME, Manifest
        from src.playlist import push_playlist

        cfg = config_mod.Config(**self.cfg_dict)
        manifest = Manifest(self.library_root / MANIFEST_NAME)
        pushed = 0
        for name in self.names:
            entries = manifest.playlist_entries(name, fmt="flac")
            if not entries:
                continue
            tracks = [Path(e.target) for e in entries]
            report = push_playlist(name, tracks, self.sd_root, cfg, prune=True)
            self.signals.playlist_done.emit({
                "name": name,
                "copied": len(report.copied),
                "up_to_date": len(report.skipped_up_to_date),
                "pruned": len(report.pruned),
                "missing": len(report.missing_sources),
            })
            pushed += 1
        self.signals.finished.emit(pushed)


class EnrichmentSignals(QObject):
    """Per-item signals emitted by TagEnrichmentRunner."""
    started = Signal(int)             # total items to look up
    progress = Signal(int, str)       # index, "Artist - Title" label
    enriched = Signal(int, dict)      # index, updated tags dict
    finished = Signal(int)            # number actually enriched
    cancelled = Signal()


class TagEnrichmentRunner(QRunnable):
    """Walk a list of (index, SourceTags) pairs through MusicBrainz and
    fill in missing genre. Runs sequentially because MusicBrainz's 1-req
    /sec rate limit makes parallelism pointless.

    Inputs are plain dicts (the SourceTags __dict__) so this is safe to
    serialise into a Qt signal; the build path applies the returned
    dicts back to the in-memory list before queueing conversion jobs.
    """

    def __init__(self, items: list[tuple[int, dict]]) -> None:
        super().__init__()
        # items: list of (index, source_tags_dict)
        self.items = items
        self.signals = EnrichmentSignals()
        self._cancel = False
        self._cache: dict[tuple[str, str, str | None], dict] = {}

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from src.musicbrainz import enrich

        self.signals.started.emit(len(self.items))
        actually_enriched = 0
        for i, (job_idx, src_tags) in enumerate(self.items):
            if self._cancel:
                self.signals.cancelled.emit()
                return
            label = f"{src_tags.get('artist', '?')} - {src_tags.get('title', '?')}"
            self.signals.progress.emit(i, label)

            # Skip items that don't need enrichment.
            if src_tags.get("genre"):
                continue

            key = (
                src_tags.get("artist", "") or "",
                src_tags.get("title", "") or "",
                src_tags.get("album", "") or None,
            )
            if key in self._cache:
                meta = self._cache[key]
            else:
                try:
                    enriched = enrich(
                        src_tags.get("artist", ""),
                        src_tags.get("title", ""),
                        album_hint=src_tags.get("album"),
                    )
                except Exception:
                    enriched = None
                meta = {
                    "genre": enriched.genre if enriched else None,
                    "date": enriched.date if enriched else None,
                    "album_artist": enriched.album_artist if enriched else None,
                } if enriched else {}
                self._cache[key] = meta

            updated = dict(src_tags)
            mb_genre = meta.get("genre")
            if mb_genre and not updated.get("genre"):
                updated["genre"] = mb_genre
            mb_date = meta.get("date")
            if mb_date and not updated.get("date"):
                updated["date"] = mb_date
            mb_aa = meta.get("album_artist")
            if mb_aa and not updated.get("album_artist"):
                updated["album_artist"] = mb_aa
            if updated != src_tags:
                self.signals.enriched.emit(job_idx, updated)
                actually_enriched += 1

        self.signals.finished.emit(actually_enriched)


class LibraryScanSignals(QObject):
    """Per-file signals emitted by LibraryScanRunner."""
    started = Signal(int)             # total flac files discovered
    track = Signal(dict)              # one track's metadata, see LibraryScanRunner.run
    finished = Signal(int)            # total tracks reported
    cancelled = Signal()
    error = Signal(str)


class LibraryScanRunner(QRunnable):
    """Walk a library tree on a background thread and emit per-track
    metadata. Used by the Library tab so opening a multi-hundred-file
    SD card doesn't freeze the GUI.

    `manifest_lookup` is a flat dict keyed by str(target_path) and
    holding tuples of (favorite: bool, playlists: list[str]). Building
    it on the calling thread before submit means the worker doesn't
    need to touch the Manifest object (which isn't thread-safe to share).
    """
    def __init__(self, root: Path, manifest_lookup: dict) -> None:
        super().__init__()
        self.root = root
        self.manifest_lookup = manifest_lookup
        self.signals = LibraryScanSignals()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            from mutagen.flac import FLAC
            paths = sorted(self.root.rglob("*.flac"))
            self.signals.started.emit(len(paths))
            count = 0
            for p in paths:
                if self._cancel:
                    self.signals.cancelled.emit()
                    return
                rel = p.relative_to(self.root)
                parts = rel.parts
                if len(parts) < 3:
                    continue
                artist, album = parts[0], parts[1]

                genre = year = bitrate = fmt = duration = ""
                try:
                    flac = FLAC(p)
                    if flac.tags:
                        gv = flac.tags.get("GENRE")
                        if gv:
                            genre = gv[0]
                        dv = flac.tags.get("DATE") or flac.tags.get("YEAR")
                        if dv:
                            # DATE can be YYYY-MM-DD; we only want YYYY
                            year = str(dv[0])[:4]
                    try:
                        bitrate = f"{flac.info.bitrate / 1000:.0f} kbps"
                    except Exception:
                        pass
                    try:
                        bd = flac.info.bits_per_sample
                        sr = flac.info.sample_rate / 1000
                        fmt = f"{bd}-bit / {sr:g} kHz"
                    except Exception:
                        pass
                    try:
                        total = int(flac.info.length)
                        duration = f"{total // 60}:{total % 60:02d}"
                    except Exception:
                        pass
                except Exception:
                    pass

                target_str = str(p)
                fav, playlists = self.manifest_lookup.get(
                    target_str, (False, []))
                self.signals.track.emit({
                    "path": target_str,
                    "filename": p.name,
                    "artist": artist,
                    "album": album,
                    "genre": genre,
                    "year": year,
                    "bitrate": bitrate,
                    "format": fmt,
                    "duration": duration,
                    "favorite": fav,
                    "playlists": list(playlists),
                })
                count += 1
            self.signals.finished.emit(count)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"{type(e).__name__}: {e}")


class DownloadSignals(QObject):
    """Qt signals emitted from the download supervisor."""
    started = Signal(int)                 # total songs
    song_started = Signal(dict)           # {line_no, artist, title}
    song_done = Signal(dict)              # DownloadResult-as-dict
    finished = Signal(int, int)           # ok_count, error_count
    cancelled = Signal()


class DownloadRunner(QRunnable):
    """Run a song list sequentially through src.downloader.download_song.

    Sequential because (a) MusicBrainz's 1-req/sec rate limit serializes
    every batch anyway and (b) yt-dlp's network throughput already
    saturates the link for one song. No pool needed."""

    def __init__(self, requests: list, dest_root: Path, cfg_dict: dict) -> None:
        super().__init__()
        self.requests = requests
        self.dest_root = dest_root
        self.cfg_dict = cfg_dict
        self.signals = DownloadSignals()
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from src import config as config_mod
        from src.downloader import download_song

        cfg = config_mod.Config(**self.cfg_dict)
        self.signals.started.emit(len(self.requests))
        ok = err = 0
        for req in self.requests:
            if self._cancel:
                self.signals.cancelled.emit()
                return
            self.signals.song_started.emit({
                "line_no": req.line_no,
                "artist": req.artist,
                "title": req.title,
            })
            result = download_song(
                artist=req.artist,
                title=req.title,
                album_hint=req.album,
                source_root=self.dest_root,
                cfg=cfg,
                line_no=req.line_no,
            )
            payload = {
                "ok": result.ok,
                "line_no": result.line_no,
                "artist": result.artist,
                "title": result.title,
                "target": str(result.target) if result.target else "",
                "youtube_url": result.youtube_url or "",
                "error": result.error or "",
                "notes": result.notes or [],
            }
            self.signals.song_done.emit(payload)
            if result.ok:
                ok += 1
            else:
                err += 1
        self.signals.finished.emit(ok, err)
