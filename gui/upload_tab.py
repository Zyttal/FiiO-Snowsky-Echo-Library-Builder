"""Upload to Device tab — pick playlists and albums to push to the SD card.

Layout:
  Top:    Library + SD card pickers; Cancel + Reload row; progress bar
          and per-push status label (hidden when idle).
  Left:   Vertical splitter with two stacked sections.
            Top section — Playlists: filter, list, per-section buttons
                          (New, Delete, Push selected, Push all).
            Bottom section — Albums: filter, list, per-section buttons
                             (Push selected, Push all).
  Right:  Track preview list — populated from whichever side (playlist
          or album) the user last clicked.

Replaces the v0.1.5 Device tab — the favorites-as-M3U export wasn't
useful because the FiiO Echo firmware can't play M3U on-device.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gui.workers import AlbumPushRunner, PlaylistPushRunner

# File suffixes the Echo can play. Used when scanning the library for
# album folders — any directory directly containing one of these is an
# album.
_AUDIO_EXTS = {".flac", ".m4a", ".opus", ".mp3", ".ogg"}


class UploadTab(QWidget):
    library_changed = Signal(Path)  # output_dir, when memberships change

    def __init__(self) -> None:
        super().__init__()
        self._library_root: Path | None = None
        self._album_dirs: list[Path] = []
        self.pool = QThreadPool.globalInstance()
        self._playlist_runner: PlaylistPushRunner | None = None
        self._album_runner: AlbumPushRunner | None = None
        self._build_layout()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        # Library + SD card pickers
        outer.addLayout(_row_with_picker(
            "Library:", "lib_edit", self._pick_lib,
            placeholder="Output library root (manifest source)…",
            tab=self,
        ))
        outer.addLayout(_row_with_picker(
            "SD card:", "sd_edit", self._pick_sd,
            placeholder="Mounted SD card root (Playlists/ + Albums/ land here)…",
            tab=self,
        ))

        # Global action row — cancel / reload only. Push actions live
        # next to each section for clarity.
        actions = QHBoxLayout()
        self.cancel_push_btn = QPushButton("Cancel push")
        self.cancel_push_btn.setEnabled(False)
        self.reload_btn = QPushButton("Reload")
        self.cancel_push_btn.clicked.connect(self._cancel_push)
        self.reload_btn.clicked.connect(self._reload)
        actions.addWidget(self.cancel_push_btn)
        actions.addWidget(self.reload_btn)
        actions.addStretch()
        outer.addLayout(actions)

        # Push progress strip — hidden when idle.
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)
        self.push_status = QLabel("")
        self.push_status.setVisible(False)
        outer.addWidget(self.push_status)

        # Caveat
        caveat = QLabel(
            "<small>Songs in two playlists land on the SD card as two file "
            "copies (FAT32/exFAT have no hardlinks/symlinks). Albums push "
            "as <SD>/Albums/&lt;Album&gt; - &lt;Artist&gt;/.</small>"
        )
        caveat.setWordWrap(True)
        outer.addWidget(caveat)

        # Main split: left side stacks Playlists / Albums; right side is
        # the track preview list.
        outer_split = QSplitter(Qt.Orientation.Horizontal)

        left_split = QSplitter(Qt.Orientation.Vertical)
        left_split.addWidget(self._build_playlists_panel())
        left_split.addWidget(self._build_albums_panel())
        left_split.setStretchFactor(0, 1)
        left_split.setStretchFactor(1, 1)

        self.track_list = QListWidget()
        outer_split.addWidget(left_split)
        outer_split.addWidget(self.track_list)
        outer_split.setStretchFactor(0, 2)
        outer_split.setStretchFactor(1, 3)
        outer.addWidget(outer_split, stretch=1)

        self.status = QLabel("(no library loaded)")
        outer.addWidget(self.status)

    def _build_playlists_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Playlists</b>"))
        header.addStretch()
        v.addLayout(header)

        self.playlist_filter = QLineEdit()
        self.playlist_filter.setPlaceholderText("Filter playlists…")
        self.playlist_filter.textChanged.connect(self._filter_playlists)
        v.addWidget(self.playlist_filter)

        self.playlist_list = QListWidget()
        self.playlist_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.playlist_list.itemSelectionChanged.connect(self._on_playlist_selected)
        v.addWidget(self.playlist_list)

        btns = QHBoxLayout()
        self.new_btn = QPushButton("New playlist…")
        self.del_btn = QPushButton("Delete")
        self.push_pl_sel_btn = QPushButton("Push selected")
        self.push_pl_all_btn = QPushButton("Push all")
        self.new_btn.clicked.connect(self._new_playlist)
        self.del_btn.clicked.connect(self._delete_playlist)
        self.push_pl_sel_btn.clicked.connect(self._push_playlists_selected)
        self.push_pl_all_btn.clicked.connect(self._push_playlists_all)
        for b in (self.new_btn, self.del_btn,
                  self.push_pl_sel_btn, self.push_pl_all_btn):
            btns.addWidget(b)
        v.addLayout(btns)
        return panel

    def _build_albums_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Albums</b>"))
        header.addStretch()
        v.addLayout(header)

        self.album_filter = QLineEdit()
        self.album_filter.setPlaceholderText("Filter albums…")
        self.album_filter.textChanged.connect(self._filter_albums)
        v.addWidget(self.album_filter)

        self.album_list = QListWidget()
        self.album_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.album_list.itemSelectionChanged.connect(self._on_album_selected)
        v.addWidget(self.album_list)

        btns = QHBoxLayout()
        self.push_al_sel_btn = QPushButton("Push selected")
        self.push_al_all_btn = QPushButton("Push all")
        self.push_al_sel_btn.clicked.connect(self._push_albums_selected)
        self.push_al_all_btn.clicked.connect(self._push_albums_all)
        for b in (self.push_al_sel_btn, self.push_al_all_btn):
            btns.addWidget(b)
        btns.addStretch()
        v.addLayout(btns)
        return panel

    # ------------------------------------------------------------------
    # Path pickers + reload
    # ------------------------------------------------------------------

    def set_library_root(self, root: Path) -> None:
        """Called from main.py when another tab knows the library root."""
        self.lib_edit.setText(str(root))
        self._reload()

    def _pick_lib(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose output library root",
            self.lib_edit.text() or str(Path.home()),
        )
        if path:
            self.lib_edit.setText(path)
            self._reload()

    def _pick_sd(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose SD card root",
            self.sd_edit.text() or str(Path.home()),
        )
        if path:
            self.sd_edit.setText(path)

    def _reload(self) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        text = self.lib_edit.text().strip()
        if not text:
            return
        root = Path(text).expanduser().resolve()
        if not (root / MANIFEST_NAME).exists():
            QMessageBox.warning(
                self, "No manifest",
                f"Couldn't find {MANIFEST_NAME} under {root}. "
                "Run a build first.",
            )
            return
        self._library_root = root
        manifest = Manifest(root / MANIFEST_NAME)

        names = manifest.playlist_names()
        self.playlist_list.clear()
        for name in names:
            count = len(manifest.playlist_entries(name))
            self.playlist_list.addItem(QListWidgetItem(f"{name}  ({count})"))

        # Discover albums on disk under the library root.
        self._album_dirs = _discover_albums(root)
        self.album_list.clear()
        for d in self._album_dirs:
            count = sum(
                1 for p in d.iterdir()
                if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
            )
            it = QListWidgetItem(f"{d.name}  ({count})")
            it.setData(Qt.ItemDataRole.UserRole, str(d))
            self.album_list.addItem(it)

        self.track_list.clear()
        self.status.setText(
            f"{len(names)} playlist(s), {len(self._album_dirs)} album(s) "
            f"in {root.name}"
        )

    # ------------------------------------------------------------------
    # Selection → track preview
    # ------------------------------------------------------------------

    def _on_playlist_selected(self) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        if not self._library_root:
            return
        item = self.playlist_list.currentItem()
        if not item or item.isHidden():
            return
        # Clear album selection so the preview reflects the click.
        self.album_list.blockSignals(True)
        self.album_list.clearSelection()
        self.album_list.blockSignals(False)
        name = self._playlist_name_from_item(item)
        manifest = Manifest(self._library_root / MANIFEST_NAME)
        self.track_list.clear()
        for entry in manifest.playlist_entries(name):
            self.track_list.addItem(QListWidgetItem(Path(entry.target).name))

    def _on_album_selected(self) -> None:
        item = self.album_list.currentItem()
        if not item or item.isHidden():
            return
        # Clear playlist selection so the preview reflects the click.
        self.playlist_list.blockSignals(True)
        self.playlist_list.clearSelection()
        self.playlist_list.blockSignals(False)
        album_dir = Path(item.data(Qt.ItemDataRole.UserRole))
        self.track_list.clear()
        for p in sorted(album_dir.iterdir(), key=lambda q: q.name.lower()):
            if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
                self.track_list.addItem(QListWidgetItem(p.name))

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filter(widget: QListWidget, text: str) -> None:
        needle = text.strip().lower()
        for i in range(widget.count()):
            it = widget.item(i)
            it.setHidden(bool(needle) and needle not in it.text().lower())

    def _filter_playlists(self, text: str) -> None:
        self._apply_filter(self.playlist_list, text)

    def _filter_albums(self, text: str) -> None:
        self._apply_filter(self.album_list, text)

    # ------------------------------------------------------------------
    # Playlist mutation
    # ------------------------------------------------------------------

    def _playlist_name_from_item(self, item: QListWidgetItem) -> str:
        return item.text().rsplit("  (", 1)[0]

    def _new_playlist(self) -> None:
        if not self._library_root:
            QMessageBox.warning(self, "No library",
                                "Set the library root first.")
            return
        name, ok = QInputDialog.getText(
            self, "New playlist", "Playlist name:")
        if not ok or not name.strip():
            return
        existing = [self._playlist_name_from_item(self.playlist_list.item(i))
                    for i in range(self.playlist_list.count())]
        if name in existing:
            QMessageBox.information(self, "Already exists",
                                    f"'{name}' already exists.")
            return
        self.playlist_list.addItem(QListWidgetItem(f"{name.strip()}  (0)"))
        self.status.setText(
            f"Created '{name.strip()}'. Add tracks from the Library tab "
            "(right-click a track → Add to playlist)."
        )

    def _delete_playlist(self) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        item = self.playlist_list.currentItem()
        if not item or not self._library_root:
            return
        name = self._playlist_name_from_item(item)
        reply = QMessageBox.question(
            self, "Delete playlist",
            f"Remove '{name}' membership from every track in the manifest? "
            "Files on the SD card aren't touched — re-push another playlist "
            "to clean those up.",
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        manifest = Manifest(self._library_root / MANIFEST_NAME)
        for e in manifest.playlist_entries(name):
            manifest.remove_from_playlist(Path(e.target), name)
        manifest.save()
        self._reload()
        self.library_changed.emit(self._library_root)

    # ------------------------------------------------------------------
    # Push: playlists
    # ------------------------------------------------------------------

    def _push_playlists_selected(self) -> None:
        if not self._guard_push_inputs():
            return
        selected = [
            self._playlist_name_from_item(it)
            for it in self.playlist_list.selectedItems()
            if not it.isHidden()
        ]
        if not selected:
            QMessageBox.warning(self, "Nothing selected",
                                "Pick at least one playlist on the left.")
            return
        self._start_playlist_push(selected)

    def _push_playlists_all(self) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        if not self._guard_push_inputs():
            return
        manifest = Manifest(self._library_root / MANIFEST_NAME)
        names = manifest.playlist_names()
        if not names:
            QMessageBox.information(self, "No playlists",
                                    "Nothing to push.")
            return
        self._start_playlist_push(names)

    def _start_playlist_push(self, names: list[str]) -> None:
        from src import config as config_mod

        sd_root = Path(self.sd_edit.text()).expanduser().resolve()
        sd_root.mkdir(parents=True, exist_ok=True)
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        cfg = config_mod.load(cfg_path)

        self._set_running(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.push_status.setVisible(True)
        self.push_status.setText("Starting playlist push…")
        self._playlist_runner = PlaylistPushRunner(
            self._library_root, sd_root, names, cfg.__dict__,
        )
        self._playlist_runner.signals.playlist_started.connect(
            self._on_playlist_started)
        self._playlist_runner.signals.track_progress.connect(
            self._on_playlist_track_progress)
        self._playlist_runner.signals.playlist_done.connect(
            self._on_playlist_done)
        self._playlist_runner.signals.finished.connect(
            self._on_playlist_push_finished)
        self._playlist_runner.signals.cancelled.connect(
            self._on_push_cancelled)
        self.pool.start(self._playlist_runner)

    def _on_playlist_started(self, name: str, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.push_status.setText(f"'{name}': 0/{total}")

    def _on_playlist_track_progress(
        self, name: str, idx: int, total: int, status: str, filename: str,
    ) -> None:
        self.progress.setValue(idx)
        verb = {"copied": "copied", "skipped": "skipped (up-to-date)",
                "missing": "MISSING source"}.get(status, status)
        self.push_status.setText(f"'{name}': {idx}/{total} — {verb}: {filename}")

    def _on_playlist_done(self, payload: dict) -> None:
        self.status.setText(
            f"'{payload['name']}': {payload['copied']} copied, "
            f"{payload['up_to_date']} up-to-date, "
            f"{payload['pruned']} pruned"
            + (f", {payload['missing']} source(s) missing"
               if payload["missing"] else "")
        )

    def _on_playlist_push_finished(self, total_playlists: int) -> None:
        self._playlist_runner = None
        self._on_any_push_finished(
            f"Pushed {total_playlists} playlist(s) to the SD card.")

    # ------------------------------------------------------------------
    # Push: albums
    # ------------------------------------------------------------------

    def _push_albums_selected(self) -> None:
        if not self._guard_push_inputs(needs_lib=False):
            return
        selected = [
            Path(it.data(Qt.ItemDataRole.UserRole))
            for it in self.album_list.selectedItems()
            if not it.isHidden()
        ]
        if not selected:
            QMessageBox.warning(self, "Nothing selected",
                                "Pick at least one album on the left.")
            return
        self._start_album_push(selected)

    def _push_albums_all(self) -> None:
        if not self._guard_push_inputs(needs_lib=False):
            return
        if not self._album_dirs:
            QMessageBox.information(self, "No albums",
                                    "No albums discovered. Reload?")
            return
        self._start_album_push(list(self._album_dirs))

    def _start_album_push(self, album_dirs: list[Path]) -> None:
        from src import config as config_mod

        sd_root = Path(self.sd_edit.text()).expanduser().resolve()
        sd_root.mkdir(parents=True, exist_ok=True)
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        cfg = config_mod.load(cfg_path)

        self._set_running(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.push_status.setVisible(True)
        self.push_status.setText(f"Starting album push ({len(album_dirs)})…")
        self._album_runner = AlbumPushRunner(
            album_dirs, sd_root, cfg.__dict__,
        )
        self._album_runner.signals.album_started.connect(
            self._on_album_started)
        self._album_runner.signals.track_progress.connect(
            self._on_album_track_progress)
        self._album_runner.signals.album_done.connect(
            self._on_album_done)
        self._album_runner.signals.finished.connect(
            self._on_album_push_finished)
        self._album_runner.signals.cancelled.connect(
            self._on_push_cancelled)
        self._album_runner.signals.error.connect(self._on_album_error)
        self.pool.start(self._album_runner)

    def _on_album_started(self, folder: str, track_count: int) -> None:
        self.progress.setRange(0, max(track_count, 1))
        self.progress.setValue(0)
        self.push_status.setText(f"'{folder}': 0/{track_count}")

    def _on_album_track_progress(
        self, folder: str, idx: int, total: int, status: str, filename: str,
    ) -> None:
        self.progress.setValue(idx)
        verb = {"copied": "copied", "skipped": "skipped (up-to-date)"}.get(
            status, status)
        self.push_status.setText(f"'{folder}': {idx}/{total} — {verb}: {filename}")

    def _on_album_done(self, payload: dict) -> None:
        bits = [f"{payload['copied']} copied",
                f"{payload['up_to_date']} up-to-date",
                f"{payload['pruned']} pruned"]
        if payload.get("cover_written"):
            bits.append("cover")
        if payload.get("lrc_failed"):
            bits.append(f"{payload['lrc_failed']} lrc skipped")
        self.status.setText(f"'{payload['folder']}': {', '.join(bits)}")

    def _on_album_push_finished(self, total: int) -> None:
        self._album_runner = None
        self._on_any_push_finished(
            f"Pushed {total} album(s) to the SD card.")

    def _on_album_error(self, msg: str) -> None:
        self._album_runner = None
        self._set_running(False)
        self.progress.setVisible(False)
        self.push_status.setVisible(False)
        QMessageBox.warning(self, "Album push error", msg)

    # ------------------------------------------------------------------
    # Shared push lifecycle
    # ------------------------------------------------------------------

    def _guard_push_inputs(self, needs_lib: bool = True) -> bool:
        """Sanity-check the library and SD card paths before kicking off
        a push. `needs_lib` is True for playlist pushes (manifest-backed)
        but optional for album pushes (FS-backed)."""
        if self._playlist_runner is not None or self._album_runner is not None:
            QMessageBox.information(self, "Push in progress",
                                    "Wait for the current push to finish.")
            return False
        if needs_lib and not self._library_root:
            QMessageBox.warning(self, "No library", "Set the library root first.")
            return False
        if not self.sd_edit.text().strip():
            QMessageBox.warning(self, "No SD card", "Set the SD card root first.")
            return False
        return True

    def _cancel_push(self) -> None:
        if self._playlist_runner is not None:
            self._playlist_runner.cancel()
        if self._album_runner is not None:
            self._album_runner.cancel()
        self.push_status.setText("Cancelling…")

    def _on_push_cancelled(self) -> None:
        self._playlist_runner = None
        self._album_runner = None
        self._set_running(False)
        self.progress.setVisible(False)
        self.push_status.setVisible(False)
        QMessageBox.information(self, "Push cancelled",
                                "Stopped before finishing.")

    def _on_any_push_finished(self, msg: str) -> None:
        self._set_running(False)
        self.progress.setVisible(False)
        self.push_status.setVisible(False)
        QMessageBox.information(self, "Push complete", msg)

    def _set_running(self, running: bool) -> None:
        for b in (self.new_btn, self.del_btn,
                  self.push_pl_sel_btn, self.push_pl_all_btn,
                  self.push_al_sel_btn, self.push_al_all_btn,
                  self.reload_btn):
            b.setEnabled(not running)
        self.cancel_push_btn.setEnabled(running)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _discover_albums(lib_root: Path) -> list[Path]:
    """Walk `lib_root` and return every directory directly containing
    audio. Sorted alphabetically by folder name. Cheap enough for tens
    of thousands of files."""
    albums: set[Path] = set()
    for p in lib_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS:
            albums.add(p.parent)
    return sorted(albums, key=lambda q: q.name.lower())


def _row_with_picker(label: str, attr: str, browse_handler,
                     placeholder: str, tab: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    setattr(tab, attr, edit)
    browse = QPushButton("Browse…")
    browse.clicked.connect(browse_handler)
    row.addWidget(QLabel(label))
    row.addWidget(edit)
    row.addWidget(browse)
    return row
