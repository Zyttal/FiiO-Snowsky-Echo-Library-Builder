"""Build tab — drive `build_library build` from the GUI.

Mirrors the CLI's options: source/output pickers, format + mirror dropdowns,
--only / --as-compilation / --force toggles, dry-run vs. run, per-file
progress table. Reuses the CLI's job pipeline via gui.workers.BuildRunner.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.workers import BuildRunner, JobSpec, TagEnrichmentRunner


class BuildTab(QWidget):
    build_finished = Signal(Path)  # output_dir

    def __init__(self, ffmpeg_path: Path | None) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.pool = QThreadPool.globalInstance()
        self._runner: BuildRunner | None = None
        self._row_for_target: dict[str, int] = {}
        self._build_layout()

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        if self.ffmpeg_path is None:
            banner = QLabel("ffmpeg not found — builds will fail until installed.")
            banner.setStyleSheet(
                "background-color: #ffd6d6; color: #800; padding: 6px;"
                "border: 1px solid #c44;"
            )
            outer.addWidget(banner)

        # Row: source picker
        self.source_edit = QLineEdit()
        self.source_btn = QPushButton("Browse…")
        self.source_btn.clicked.connect(lambda: self._pick_dir(self.source_edit))
        outer.addLayout(_labeled_row("Source:", self.source_edit, self.source_btn))

        # Row: output picker
        self.output_edit = QLineEdit()
        self.output_btn = QPushButton("Browse…")
        self.output_btn.clicked.connect(lambda: self._pick_dir(self.output_edit))
        outer.addLayout(_labeled_row("Output:", self.output_edit, self.output_btn))

        # Row: format + mirror + workers
        self.format_combo = QComboBox()
        self.format_combo.addItems(["flac", "mp3", "dsd"])
        self.mirror_combo = QComboBox()
        self.mirror_combo.addItems(["none", "flac", "mp3", "dsd"])
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 64)
        self.workers_spin.setValue(max(1, (os.cpu_count() or 2) - 1))
        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Format:"))
        opts_row.addWidget(self.format_combo)
        opts_row.addSpacing(12)
        opts_row.addWidget(QLabel("Mirror:"))
        opts_row.addWidget(self.mirror_combo)
        opts_row.addSpacing(12)
        opts_row.addWidget(QLabel("Workers:"))
        opts_row.addWidget(self.workers_spin)
        opts_row.addStretch()
        outer.addLayout(opts_row)

        # Row: filters + flags
        self.only_edit = QLineEdit()
        self.only_edit.setPlaceholderText("substring (optional)")
        self.compilation_edit = QLineEdit()
        self.compilation_edit.setPlaceholderText("compilation folder substring (optional)")
        outer.addLayout(_labeled_row("Only album:", self.only_edit))
        outer.addLayout(_labeled_row("Treat as compilation:", self.compilation_edit))
        self.force_check = QCheckBox("Force rebuild (ignore manifest)")
        outer.addWidget(self.force_check)
        self.enrich_check = QCheckBox(
            "Look up missing tags via MusicBrainz (slow: ~1 s per missing track)"
        )
        self.enrich_check.setToolTip(
            "Before the build phase, fill in missing GENRE / DATE / "
            "ALBUMARTIST tags by querying MusicBrainz. Cached per session "
            "to avoid repeat lookups. The Echo's 'Unknown' genre vanishes "
            "when this is on."
        )
        outer.addWidget(self.enrich_check)

        # Row: action buttons
        btn_row = QHBoxLayout()
        self.dry_run_btn = QPushButton("Dry run")
        self.run_btn = QPushButton("Run")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.dry_run_btn.clicked.connect(lambda: self._build(dry_run=True))
        self.run_btn.clicked.connect(lambda: self._build(dry_run=False))
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.dry_run_btn)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        # Progress + table
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        outer.addWidget(self.progress)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File", "Status", "Error / Notes"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        outer.addWidget(self.table)

    @staticmethod
    def _pick_dir(edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(None, "Choose a folder",
                                                edit.text() or str(Path.home()))
        if path:
            edit.setText(path)

    def _gather_jobs(self) -> tuple[list[JobSpec], Path] | None:
        """Run the same scan/plan pipeline as the CLI's build, but stop short
        of dispatching. Returns (jobs, output_dir) or None on user-facing error."""
        from src import config as config_mod, layout, scan, tags

        source = Path(self.source_edit.text()).expanduser()
        output = Path(self.output_edit.text()).expanduser()
        if not source.is_dir():
            QMessageBox.warning(self, "Bad source", "Source folder doesn't exist.")
            return None
        if not self.output_edit.text():
            QMessageBox.warning(self, "Bad output", "Output folder is required.")
            return None

        output.mkdir(parents=True, exist_ok=True)
        source = source.resolve()
        output = output.resolve()

        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        cfg = config_mod.load(cfg_path)
        cfg.workers = self.workers_spin.value()

        only = self.only_edit.text().strip() or None
        comp_match = self.compilation_edit.text().strip() or None
        primary = self.format_combo.currentText()
        mirror = self.mirror_combo.currentText()

        items = scan.discover(source, only=only)
        if not items:
            QMessageBox.warning(self, "No source files",
                                "No audio found under the source folder.")
            return None

        from build_library import _selected_strategies
        from src.manifest import MANIFEST_NAME, Manifest
        strategies = _selected_strategies(primary, mirror)
        manifest = Manifest(output / MANIFEST_NAME)

        item_tags = []
        comp_counters: dict[tuple[str, int | None], int] = {}
        for item in items:
            t = tags.read_source(item.source, item.album_folder_name, item.disc_no)
            if t.genre is None and cfg.default_genre:
                t.genre = cfg.default_genre
            if comp_match and comp_match.lower() in item.album_folder_name.lower():
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

        jobs: list[JobSpec] = []
        force = self.force_check.isChecked()
        for item, src_tags in item_tags:
            for strategy in strategies:
                is_primary = strategy.name == primary
                out_root = strategy.output_root(output, is_primary)
                artist_album = layout._artist_album(src_tags, cfg)
                target = layout.target_path(
                    out_root, src_tags, cfg, strategy.ext,
                    multi_disc=artist_album in multi_disc,
                )
                if not force and manifest.is_current(item.source, strategy.name, target):
                    continue
                jobs.append(JobSpec(
                    source=item.source,
                    target=target,
                    strategy=strategy.name,
                    cover=item.cover,
                    src_tags_dict=src_tags.__dict__,
                    cfg_dict=cfg.__dict__,
                ))
        return jobs, output

    def _build(self, dry_run: bool) -> None:
        if self.ffmpeg_path is None and not dry_run:
            QMessageBox.critical(self, "ffmpeg required",
                                 "ffmpeg is required for builds. Install it first.")
            return
        gathered = self._gather_jobs()
        if gathered is None:
            return
        jobs, output_dir = gathered

        if dry_run:
            self.table.setRowCount(0)
            self._row_for_target.clear()
            for j in jobs[:200]:
                self._add_or_update_row(
                    target=str(j.target),
                    file_label=j.target.name,
                    status="planned",
                    note=f"[{j.strategy}]",
                )
            QMessageBox.information(
                self, "Dry run",
                f"{len(jobs)} jobs planned. Showing first "
                f"{min(len(jobs), 200)} in the table.",
            )
            return

        if not jobs:
            QMessageBox.information(self, "Nothing to do",
                                    "Manifest reports everything up-to-date.")
            return

        if self.enrich_check.isChecked():
            self._start_enrichment(jobs, output_dir)
        else:
            self._start_build(jobs, output_dir)

    def _start_enrichment(self, jobs: list, output_dir: Path) -> None:
        """Phase 1 — MusicBrainz enrichment. Walks every job missing a
        genre, looks it up, mutates jobs in place. Then proceeds to the
        build phase."""
        # Deduplicate by (source, strategy=primary) so a FLAC+MP3 pair
        # for the same source only triggers one lookup; the cache inside
        # the runner also dedupes by (artist, title, album) across
        # jobs that happen to be the same recording.
        to_enrich: list[tuple[int, dict]] = []
        for idx, j in enumerate(jobs):
            if j.src_tags_dict.get("genre"):
                continue
            to_enrich.append((idx, dict(j.src_tags_dict)))

        if not to_enrich:
            # Everything already has a genre — go straight to build.
            self._start_build(jobs, output_dir)
            return

        self.table.setRowCount(0)
        self._row_for_target.clear()
        self.progress.setRange(0, len(to_enrich))
        self.progress.setValue(0)
        self._set_running(True)
        self._jobs_to_build = jobs
        self._output_dir = output_dir

        self._enrich_runner = TagEnrichmentRunner(to_enrich)
        self._enrich_runner.signals.progress.connect(self._on_enrich_progress)
        self._enrich_runner.signals.enriched.connect(self._on_enrich_enriched)
        self._enrich_runner.signals.finished.connect(self._on_enrich_finished)
        self._enrich_runner.signals.cancelled.connect(self._on_enrich_cancelled)
        self.pool.start(self._enrich_runner)

    def _on_enrich_progress(self, i: int, label: str) -> None:
        self.progress.setValue(i + 1)
        # Reuse the table for live MB feedback so the user can see what's
        # happening; we'll wipe and repopulate for the build phase.
        self._add_or_update_row(
            target=f"enrich-{i}",
            file_label=label,
            status="looking up",
            note="",
        )

    def _on_enrich_enriched(self, idx: int, updated_tags: dict) -> None:
        self._jobs_to_build[idx].src_tags_dict.update(updated_tags)
        # Reflect the genre that just landed for the row that's currently
        # showing this artist/title (best-effort, not strictly mapped).
        artist = updated_tags.get("artist", "")
        title = updated_tags.get("title", "")
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text() == f"{artist} - {title}":
                self.table.setItem(row, 1, _status_item("enriched"))
                genre = updated_tags.get("genre", "")
                self.table.setItem(row, 2, QTableWidgetItem(
                    f"genre={genre}" if genre else ""
                ))
                break

    def _on_enrich_finished(self, n: int) -> None:
        # Move on to the build phase with the (now possibly mutated) job
        # list.
        self._start_build(self._jobs_to_build, self._output_dir)

    def _on_enrich_cancelled(self) -> None:
        self._set_running(False)
        self._enrich_runner = None
        QMessageBox.information(self, "Enrichment cancelled",
                                "Build was not started.")

    def _start_build(self, jobs: list, output_dir: Path) -> None:
        self.table.setRowCount(0)
        self._row_for_target.clear()
        for j in jobs:
            self._add_or_update_row(
                target=str(j.target),
                file_label=j.target.name,
                status="queued",
                note=f"[{j.strategy}]",
            )

        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self._set_running(True)
        self._output_dir = output_dir

        self._runner = BuildRunner(jobs, workers=self.workers_spin.value())
        self._runner.signals.file_done.connect(self._on_file_done)
        self._runner.signals.finished.connect(self._on_finished)
        self._runner.signals.cancelled.connect(self._on_cancelled)
        self.pool.start(self._runner)

    def _cancel(self) -> None:
        if self._runner:
            self._runner.cancel()

    def _on_file_done(self, result: dict) -> None:
        target = result.get("target", "")
        label = Path(target).name if target else "?"
        if result.get("ok"):
            self._add_or_update_row(target, label, "done",
                                    f"[{result.get('strategy')}]")
        else:
            self._add_or_update_row(target, label, "error",
                                    result.get("error", ""))
        self.progress.setValue(self.progress.value() + 1)

    def _on_finished(self, ok: int, err: int) -> None:
        self._set_running(False)
        msg = f"Done: {ok} ok"
        if err:
            msg += f", {err} errors"
        QMessageBox.information(self, "Build complete", msg)
        if hasattr(self, "_output_dir"):
            self.build_finished.emit(self._output_dir)

    def _on_cancelled(self) -> None:
        self._set_running(False)
        QMessageBox.information(self, "Build cancelled",
                                "In-flight jobs may have finished; "
                                "queued jobs were dropped.")

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running)
        self.dry_run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for w in (self.source_edit, self.source_btn, self.output_edit,
                  self.output_btn, self.format_combo, self.mirror_combo,
                  self.workers_spin, self.only_edit, self.compilation_edit,
                  self.force_check):
            w.setEnabled(not running)

    def _add_or_update_row(
        self, target: str, file_label: str, status: str, note: str
    ) -> None:
        row = self._row_for_target.get(target)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._row_for_target[target] = row
        self.table.setItem(row, 0, QTableWidgetItem(file_label))
        self.table.setItem(row, 1, _status_item(status))
        self.table.setItem(row, 2, QTableWidgetItem(note))


def _labeled_row(label: str, *widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    for w in widgets:
        row.addWidget(w)
    return row


def _status_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    colors = {
        "done": "#2a7a2a",
        "error": "#a02020",
        "running": "#7a5a00",
        "queued": "#555",
        "planned": "#555",
    }
    item.setForeground(Qt.GlobalColor.darkGreen if text == "done"
                       else Qt.GlobalColor.darkRed if text == "error"
                       else Qt.GlobalColor.gray)
    return item
