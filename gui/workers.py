"""Threading layer between the Qt event loop and the CLI's job pipeline.

We deliberately run jobs in a ProcessPoolExecutor (same as the CLI) rather
than QThreadPool — ffmpeg and mutagen are CPU-bound, and the worker function
is already pickle-clean dicts. A single QRunnable supervises the pool from a
background Qt thread and emits per-file signals back to the UI.
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
