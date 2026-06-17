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

from gui.workers import BuildRunner, JobSpec, TagEnrichmentRunner, is_mb_shaped


class BuildTab(QWidget):
    build_finished = Signal(Path)  # output_dir

    def __init__(self, ffmpeg_path: Path | None) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.pool = QThreadPool.globalInstance()
        self._runner: BuildRunner | None = None
        self._enrich_runner: TagEnrichmentRunner | None = None
        self._pending_state: dict | None = None
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
        self.format_combo.addItems(["preserve", "flac", "mp3", "dsd"])
        self.format_combo.setToolTip(
            "preserve: keep each source format when the Echo can play it "
            "(MP3/M4A/OGG copy as-is, FLAC up to 16/96 copies as-is, FLAC "
            "higher than that downconverts to 16/44.1, WAV becomes FLAC). "
            "Otherwise pick a fixed output format."
        )
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

    def _read_source_tags(self) -> dict | None:
        """Phase 1 of the build pipeline: scan source, read tags with the
        REAL artist/title intact (no compilation rewrite). Synchronous;
        fast enough for libraries up to a few thousand files.

        The compilation rewrite happens later in `_apply_comp_and_build`,
        so MusicBrainz enrichment in between sees the real artist+title
        instead of "Various Artists" / "Artist - Title"."""
        from src import config as config_mod, scan, tags

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

        item_tags = []
        for item in items:
            t = tags.read_source(item.source, item.album_folder_name, item.disc_no)
            # default_genre is a fallback when MB isn't asked to enrich;
            # it gets applied here so the enrichment phase doesn't see
            # tags with a missing genre when there's no need to look it up.
            if t.genre is None and cfg.default_genre:
                t.genre = cfg.default_genre
            item_tags.append((item, t))

        return {
            "cfg": cfg,
            "items": items,
            "item_tags": item_tags,
            "output": output,
            "comp_match": comp_match,
            "primary": primary,
            "mirror": mirror,
        }

    def _apply_comp_and_build_jobs(self, state: dict) -> list[JobSpec]:
        """Phase 3 of the build pipeline: apply compilation handling to
        whatever the enrichment phase produced, compute target paths,
        filter against the manifest, return ready-to-queue JobSpecs."""
        from src import layout
        from build_library import _selected_strategies
        from src.manifest import MANIFEST_NAME, Manifest

        cfg = state["cfg"]
        items = state["items"]
        item_tags = state["item_tags"]
        output = state["output"]
        comp_match = state["comp_match"]
        primary = state["primary"]
        mirror = state["mirror"]

        strategies = _selected_strategies(primary, mirror)
        manifest = Manifest(output / MANIFEST_NAME)

        comp_counters: dict[tuple[str, int | None], int] = {}
        for item, t in item_tags:
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

        disc_index = layout.discs_per_album([(cfg, t) for _, t in item_tags])
        multi_disc = {k for k, v in disc_index.items() if len(v) > 1}

        jobs: list[JobSpec] = []
        force = self.force_check.isChecked()
        for item, src_tags in item_tags:
            for strategy in strategies:
                is_primary = strategy.name == primary
                out_root = strategy.output_root(output, is_primary)
                artist_album = layout._artist_album(src_tags, cfg)
                ext = (strategy.decide_ext(item.source)
                       if hasattr(strategy, "decide_ext")
                       else strategy.ext)
                target = layout.target_path(
                    out_root, src_tags, cfg, ext,
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
        return jobs

    def _build(self, dry_run: bool) -> None:
        if self.ffmpeg_path is None and not dry_run:
            QMessageBox.critical(self, "ffmpeg required",
                                 "ffmpeg is required for builds. Install it first.")
            return
        state = self._read_source_tags()
        if state is None:
            return

        if dry_run:
            # Skip enrichment for dry runs — the goal is to preview the
            # plan quickly, not pay 5 minutes of MB lookups.
            jobs = self._apply_comp_and_build_jobs(state)
            self.table.setRowCount(0)
            self._row_for_target.clear()
            for j in jobs:
                self._add_or_update_row(
                    target=str(j.target),
                    file_label=j.target.name,
                    status="planned",
                    note=f"[{j.strategy}]",
                )
            QMessageBox.information(
                self, "Dry run",
                f"{len(jobs)} jobs planned.",
            )
            return

        if self.enrich_check.isChecked():
            self._start_enrichment(state)
        else:
            jobs = self._apply_comp_and_build_jobs(state)
            if not jobs:
                QMessageBox.information(self, "Nothing to do",
                                        "Manifest reports everything up-to-date.")
                return
            self._start_build(jobs, state["output"])

    def _start_enrichment(self, state: dict) -> None:
        """Phase 2 — MusicBrainz enrichment. Walks every track whose
        SourceTags is NOT already MB-shaped (album+date+genre+album_artist
        all present), queries MB with the REAL artist+title (compilation
        rewrite hasn't happened yet), mutates the SourceTags in place.
        Then runs phase 3 + the build.

        Downloader-produced tracks are MB-shaped already, so a Build over
        a download-only library is a no-op for this phase even with the
        checkbox on."""
        # to_enrich indexes into state["item_tags"]
        to_enrich: list[tuple[int, dict]] = []
        skipped = 0
        for idx, (_, t) in enumerate(state["item_tags"]):
            if is_mb_shaped(t.__dict__):
                skipped += 1
                continue
            to_enrich.append((idx, dict(t.__dict__)))

        if not to_enrich:
            # Nothing to look up — skip straight to comp + build.
            jobs = self._apply_comp_and_build_jobs(state)
            if not jobs:
                msg = "Manifest reports everything up-to-date."
                if skipped:
                    msg = (f"All {skipped} tracks already MB-tagged; "
                           "nothing to build.")
                QMessageBox.information(self, "Nothing to do", msg)
                return
            if skipped:
                QMessageBox.information(
                    self, "MusicBrainz",
                    f"All {skipped} tracks already MB-tagged; "
                    "skipping enrichment.",
                )
            self._start_build(jobs, state["output"])
            return

        self.table.setRowCount(0)
        self._row_for_target.clear()
        if skipped:
            self._add_or_update_row(
                target="enrich-summary",
                file_label=f"({skipped} already MB-tagged)",
                status="skipped",
                note="album+date+genre+album_artist present",
            )
        self.progress.setRange(0, len(to_enrich))
        self.progress.setValue(0)
        self._set_running(True)
        self._pending_state = state

        # Remember which display row corresponds to which job index, so
        # both `enriched` and `no_match` signals can flip the row's status
        # cell without depending on a label-string match (which fails for
        # tracks whose artist/title contain special characters).
        self._enrich_row_for_idx: dict[int, int] = {}

        self._enrich_runner = TagEnrichmentRunner(to_enrich)
        self._enrich_runner.signals.progress.connect(self._on_enrich_progress)
        self._enrich_runner.signals.enriched.connect(self._on_enrich_enriched)
        self._enrich_runner.signals.no_match.connect(self._on_enrich_no_match)
        self._enrich_runner.signals.finished.connect(self._on_enrich_finished)
        self._enrich_runner.signals.cancelled.connect(self._on_enrich_cancelled)
        # Remember (idx → job_idx) so progress events know which job is
        # being processed without relying on string matching.
        self._enrich_jobidx_by_i = [job_idx for job_idx, _ in to_enrich]
        self.pool.start(self._enrich_runner)

    def _on_enrich_progress(self, i: int, label: str) -> None:
        self.progress.setValue(i + 1)
        # Reuse the table for live MB feedback so the user can see what's
        # happening; we'll wipe and repopulate for the build phase.
        target_key = f"enrich-{i}"
        self._add_or_update_row(
            target=target_key,
            file_label=label,
            status="looking up",
            note="",
        )
        # Remember which display row this MB call corresponds to so the
        # enriched / no_match callback can flip the cell deterministically.
        job_idx = self._enrich_jobidx_by_i[i] if i < len(
            self._enrich_jobidx_by_i) else -1
        if job_idx >= 0:
            self._enrich_row_for_idx[job_idx] = self._row_for_target[target_key]

    def _on_enrich_enriched(self, idx: int, updated_tags: dict) -> None:
        # Mutate the SourceTags in place so the upcoming compilation
        # rewrite + job-build phase picks up the enriched fields.
        t = self._pending_state["item_tags"][idx][1]
        if updated_tags.get("genre"):
            t.genre = updated_tags["genre"]
        if updated_tags.get("date"):
            t.date = updated_tags["date"]
        if updated_tags.get("album_artist"):
            t.album_artist = updated_tags["album_artist"]

        row = self._enrich_row_for_idx.get(idx)
        if row is not None:
            self.table.setItem(row, 1, _status_item("enriched"))
            genre = updated_tags.get("genre", "")
            self.table.setItem(row, 2, QTableWidgetItem(
                f"genre={genre}" if genre else "(no genre returned)"
            ))

    def _on_enrich_no_match(self, idx: int) -> None:
        row = self._enrich_row_for_idx.get(idx)
        if row is not None:
            self.table.setItem(row, 1, _status_item("no match"))
            self.table.setItem(row, 2, QTableWidgetItem("MusicBrainz had nothing"))

    def _on_enrich_finished(self, n: int) -> None:
        # Phase 3: now that the SourceTags are enriched, apply the
        # compilation rewrite and queue jobs.
        jobs = self._apply_comp_and_build_jobs(self._pending_state)
        output_dir = self._pending_state["output"]
        self._pending_state = None
        if not jobs:
            self._set_running(False)
            QMessageBox.information(self, "Nothing to do",
                                    "Manifest reports everything up-to-date.")
            return
        self._start_build(jobs, output_dir)

    def _on_enrich_cancelled(self) -> None:
        self._set_running(False)
        self._enrich_runner = None
        self._pending_state = None
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

        self._runner = BuildRunner(
            jobs,
            workers=self.workers_spin.value(),
            output_dir=output_dir,
        )
        self._runner.signals.file_done.connect(self._on_file_done)
        self._runner.signals.finished.connect(self._on_finished)
        self._runner.signals.cancelled.connect(self._on_cancelled)
        self.pool.start(self._runner)

    def _cancel(self) -> None:
        # Cancel whichever phase is currently running. Both runners are
        # tracked separately because the enrichment phase is its own
        # background job before the BuildRunner is started.
        if self._enrich_runner is not None:
            self._enrich_runner.cancel()
        if self._runner is not None:
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
