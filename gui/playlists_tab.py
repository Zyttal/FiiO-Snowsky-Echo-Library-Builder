"""Playlists tab — manage folder-as-playlist memberships and push to card.

Left pane: list of playlists with track counts.
Right pane: tracks in the selected playlist.
Buttons: New, Delete, Push selected, Push all.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from gui.workers import PlaylistPushRunner


class PlaylistsTab(QWidget):
    library_changed = Signal(Path)  # output_dir, when memberships change

    def __init__(self) -> None:
        super().__init__()
        self._library_root: Path | None = None
        self._sd_root: Path | None = None
        self.pool = QThreadPool.globalInstance()
        self._runner: PlaylistPushRunner | None = None
        self._build_layout()

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
            placeholder="Mounted SD card root (playlists land in <SD>/Playlists/)…",
            tab=self,
        ))

        # Action row
        actions = QHBoxLayout()
        self.new_btn = QPushButton("New playlist…")
        self.del_btn = QPushButton("Delete playlist")
        self.push_one_btn = QPushButton("Push selected to card")
        self.push_all_btn = QPushButton("Push all to card")
        self.reload_btn = QPushButton("Reload")
        self.new_btn.clicked.connect(self._new_playlist)
        self.del_btn.clicked.connect(self._delete_playlist)
        self.push_one_btn.clicked.connect(lambda: self._push(all_playlists=False))
        self.push_all_btn.clicked.connect(lambda: self._push(all_playlists=True))
        self.reload_btn.clicked.connect(self._reload)
        for b in (self.new_btn, self.del_btn, self.push_one_btn,
                  self.push_all_btn, self.reload_btn):
            actions.addWidget(b)
        actions.addStretch()
        outer.addLayout(actions)

        # Caveat
        caveat = QLabel(
            "<small>Songs in two playlists land on the SD card as two file "
            "copies (FAT32/exFAT have no hardlinks/symlinks). A 30-track "
            "playlist is ~150 MB — fine on a 256 GB card.</small>"
        )
        caveat.setWordWrap(True)
        outer.addWidget(caveat)

        # Left: playlist list. Right: tracks in selected playlist.
        splitter = QSplitter()
        self.playlist_list = QListWidget()
        self.playlist_list.itemSelectionChanged.connect(self._on_playlist_selected)
        self.track_list = QListWidget()
        splitter.addWidget(self.playlist_list)
        splitter.addWidget(self.track_list)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, stretch=1)

        self.status = QLabel("(no library loaded)")
        outer.addWidget(self.status)

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
        self.track_list.clear()
        for name in names:
            count = len(manifest.playlist_entries(name))
            self.playlist_list.addItem(QListWidgetItem(f"{name}  ({count})"))
        if names:
            self.playlist_list.setCurrentRow(0)
        self.status.setText(f"{len(names)} playlists in {root.name}")

    def _on_playlist_selected(self) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        if not self._library_root:
            return
        item = self.playlist_list.currentItem()
        if not item:
            return
        name = item.text().rsplit("  (", 1)[0]
        manifest = Manifest(self._library_root / MANIFEST_NAME)
        self.track_list.clear()
        for entry in manifest.playlist_entries(name):
            self.track_list.addItem(QListWidgetItem(Path(entry.target).name))

    def _new_playlist(self) -> None:
        if not self._library_root:
            QMessageBox.warning(self, "No library",
                                "Set the library root first.")
            return
        name, ok = QInputDialog.getText(
            self, "New playlist", "Playlist name:")
        if not ok or not name.strip():
            return
        # No tracks yet; just show it in the list. It'll be empty until the
        # user adds tracks via the Library tab.
        existing = [self.playlist_list.item(i).text().rsplit("  (", 1)[0]
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
        name = item.text().rsplit("  (", 1)[0]
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

    def _push(self, all_playlists: bool) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        if not self._library_root:
            QMessageBox.warning(self, "No library", "Set the library root first.")
            return
        sd_text = self.sd_edit.text().strip()
        if not sd_text:
            QMessageBox.warning(self, "No SD card", "Set the SD card root first.")
            return
        sd_root = Path(sd_text).expanduser().resolve()
        sd_root.mkdir(parents=True, exist_ok=True)

        manifest = Manifest(self._library_root / MANIFEST_NAME)
        if all_playlists:
            names = manifest.playlist_names()
        else:
            item = self.playlist_list.currentItem()
            if not item:
                QMessageBox.warning(self, "No playlist",
                                    "Select a playlist on the left first.")
                return
            names = [item.text().rsplit("  (", 1)[0]]
        if not names:
            QMessageBox.information(self, "No playlists",
                                    "Nothing to push.")
            return

        from src import config as config_mod
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        cfg = config_mod.load(cfg_path)

        self._set_running(True)
        self._runner = PlaylistPushRunner(
            self._library_root, sd_root, names, cfg.__dict__,
        )
        self._runner.signals.playlist_done.connect(self._on_playlist_done)
        self._runner.signals.finished.connect(self._on_push_finished)
        self.pool.start(self._runner)

    def _on_playlist_done(self, payload: dict) -> None:
        self.status.setText(
            f"'{payload['name']}': {payload['copied']} copied, "
            f"{payload['up_to_date']} up-to-date, "
            f"{payload['pruned']} pruned"
            + (f", {payload['missing']} source(s) missing"
               if payload["missing"] else "")
        )

    def _on_push_finished(self, total_playlists: int) -> None:
        self._set_running(False)
        QMessageBox.information(
            self, "Push complete",
            f"Pushed {total_playlists} playlist(s) to the SD card.")

    def _set_running(self, running: bool) -> None:
        for b in (self.new_btn, self.del_btn, self.push_one_btn,
                  self.push_all_btn, self.reload_btn):
            b.setEnabled(not running)


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
