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
