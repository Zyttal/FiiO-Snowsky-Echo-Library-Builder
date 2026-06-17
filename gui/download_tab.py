"""Download tab — pull songs from YouTube into the source library.

Input is a text file with `Artist - Title` or `Artist - Album - Title`
lines. Each song is enriched via MusicBrainz, downloaded from YouTube,
re-encoded to FLAC by ffmpeg, tagged with the canonical metadata, and
dropped into <dest>/<Album> - <Artist>/. The existing Build tab picks it
up on the next build.

Single-threaded supervisor (see DownloadRunner) — MB's rate limit makes
parallelism moot, and the table updates as each song lands.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.workers import DownloadRunner


class DownloadTab(QWidget):
    download_finished = Signal(Path)  # dest_root, for cross-tab refresh

    def __init__(self) -> None:
        super().__init__()
        self.pool = QThreadPool.globalInstance()
        self._runner: DownloadRunner | None = None
        self._row_for_line: dict[int, int] = {}
        self._build_layout()

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        # Row: song list picker
        self.list_edit = QLineEdit()
        self.list_edit.setPlaceholderText("Path to song list (text file)…")
        list_browse = QPushButton("Browse…")
        list_browse.clicked.connect(self._pick_list)
        outer.addLayout(_row("Songs:", self.list_edit, list_browse))

        # Row: dest picker
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Source library root (where build reads from)…")
        dest_browse = QPushButton("Browse…")
        dest_browse.clicked.connect(self._pick_dest)
        outer.addLayout(_row("Source root:", self.dest_edit, dest_browse))

        # Help line
        help_label = QLabel(
            "<small>Input format: one song per line — "
            "<code>Artist - Title</code> or <code>Artist - Album - Title</code>. "
            "The 3-field form picks the right album on MusicBrainz; without it, "
            "popular tracks often resolve to compilations.</small>"
        )
        help_label.setWordWrap(True)
        outer.addWidget(help_label)

        # Row: action buttons
        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("Download")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._start)
        self.cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        # Progress + table
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        outer.addWidget(self.progress)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "Song", "Status", "Notes"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        outer.addWidget(self.table)

    def _pick_list(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose song list", self.list_edit.text() or str(Path.home()),
            "Text files (*.txt *.list);;All files (*)",
        )
        if path:
            self.list_edit.setText(path)

    def _pick_dest(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose source library root",
            self.dest_edit.text() or str(Path.home()),
        )
        if path:
            self.dest_edit.setText(path)

    def _start(self) -> None:
        from src import config as config_mod
        from src.song_list import parse as parse_song_list

        list_path = Path(self.list_edit.text()).expanduser()
        dest_root = Path(self.dest_edit.text()).expanduser()
        if not list_path.is_file():
            QMessageBox.warning(self, "Bad song list", "Choose a valid text file.")
            return
        if not self.dest_edit.text():
            QMessageBox.warning(self, "No destination", "Choose a source library root.")
            return
        dest_root.mkdir(parents=True, exist_ok=True)
        dest_root = dest_root.resolve()

        try:
            requests = parse_song_list(list_path)
        except ValueError as e:
            QMessageBox.critical(self, "Parse error", str(e))
            return
        if not requests:
            QMessageBox.information(self, "Empty list", "No songs to download.")
            return

        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        cfg = config_mod.load(cfg_path)

        self.table.setRowCount(0)
        self._row_for_line.clear()
        for req in requests:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(req.line_no)))
            label = f"{req.artist} - {req.title}"
            if req.album:
                label += f"   [{req.album}]"
            self.table.setItem(row, 1, QTableWidgetItem(label))
            self.table.setItem(row, 2, _status_item("queued"))
            self.table.setItem(row, 3, QTableWidgetItem(""))
            self._row_for_line[req.line_no] = row

        self.progress.setRange(0, len(requests))
        self.progress.setValue(0)
        self._set_running(True)
        self._dest_root = dest_root

        self._runner = DownloadRunner(requests, dest_root, cfg.__dict__)
        self._runner.signals.song_started.connect(self._on_song_started)
        self._runner.signals.song_done.connect(self._on_song_done)
        self._runner.signals.finished.connect(self._on_finished)
        self._runner.signals.cancelled.connect(self._on_cancelled)
        self.pool.start(self._runner)

    def _cancel(self) -> None:
        if self._runner:
            self._runner.cancel()

    def _on_song_started(self, payload: dict) -> None:
        row = self._row_for_line.get(payload["line_no"])
        if row is None:
            return
        self.table.setItem(row, 2, _status_item("running"))

    def _on_song_done(self, payload: dict) -> None:
        row = self._row_for_line.get(payload["line_no"])
        if row is None:
            return
        status = "done" if payload["ok"] else "error"
        self.table.setItem(row, 2, _status_item(status))
        notes = list(payload.get("notes", []))
        if payload.get("error"):
            notes.insert(0, payload["error"])
        elif payload.get("target"):
            try:
                notes.insert(0, str(Path(payload["target"]).relative_to(self._dest_root)))
            except (ValueError, AttributeError):
                notes.insert(0, payload["target"])
        self.table.setItem(row, 3, QTableWidgetItem("  •  ".join(notes)))
        self.progress.setValue(self.progress.value() + 1)

    def _on_finished(self, ok: int, err: int) -> None:
        self._set_running(False)
        msg = f"Done: {ok} downloaded"
        if err:
            msg += f", {err} failed"
        QMessageBox.information(self, "Downloads complete", msg)
        if hasattr(self, "_dest_root"):
            self.download_finished.emit(self._dest_root)

    def _on_cancelled(self) -> None:
        self._set_running(False)
        QMessageBox.information(self, "Cancelled",
                                "Remaining songs were skipped.")

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for w in (self.list_edit, self.dest_edit):
            w.setEnabled(not running)


def _row(label: str, *widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(QLabel(label))
    for w in widgets:
        row.addWidget(w)
    return row


def _status_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setForeground(
        Qt.GlobalColor.darkGreen if text == "done"
        else Qt.GlobalColor.darkRed if text == "error"
        else Qt.GlobalColor.darkYellow if text == "running"
        else Qt.GlobalColor.gray
    )
    return item
