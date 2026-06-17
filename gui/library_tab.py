"""Library tab — browse the output, toggle favorites, refresh on rebuild.

Reads the existing manifest at <output>/.echo-library-manifest.json plus the
on-disk FLACs (for genre + bitrate). Tree structure is Artist > Album > Track.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gui.workers import LibraryScanRunner


class LibraryTab(QWidget):
    favorites_changed = Signal()
    playlists_changed = Signal()

    COL_NAME = 0
    COL_FAV = 1
    COL_GENRE = 2
    COL_BITRATE = 3
    COL_PLAYLISTS = 4

    def __init__(self) -> None:
        super().__init__()
        self._output_dir: Path | None = None
        self._manifest = None  # src.manifest.Manifest, set on load
        self._scan: LibraryScanRunner | None = None
        self._pool = QThreadPool.globalInstance()
        self._artists: dict[str, QTreeWidgetItem] = {}
        self._albums: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._build_layout()

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Output library root…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_dir)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._reload)
        self.cancel_btn = QPushButton("Cancel scan")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_scan)
        row.addWidget(QLabel("Library:"))
        row.addWidget(self.path_edit)
        row.addWidget(browse)
        row.addWidget(reload_btn)
        row.addWidget(self.cancel_btn)
        outer.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(
            ["Artist / Album / Track", "Favorite", "Genre", "Bitrate", "Playlists"]
        )
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        outer.addWidget(self.tree)

        self.status = QLabel("(no library loaded)")
        outer.addWidget(self.status)

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose output library root",
            self.path_edit.text() or str(Path.home()),
        )
        if path:
            self.path_edit.setText(path)
            self._reload()

    def reload_if_loaded(self, output_dir: Path) -> None:
        """Called when a build completes — refresh if the user has loaded
        the same output dir we just built into."""
        if self._output_dir and Path(output_dir).resolve() == self._output_dir.resolve():
            self._reload()
        elif not self._output_dir:
            self.path_edit.setText(str(output_dir))
            self._reload()

    def _reload(self) -> None:
        from src.manifest import MANIFEST_NAME, Manifest

        text = self.path_edit.text().strip()
        if not text:
            return
        root = Path(text).expanduser().resolve()
        if not root.is_dir():
            QMessageBox.warning(self, "Not a folder", f"{root} is not a folder.")
            return

        # Cancel any in-flight scan so a fast Reload click doesn't pile
        # two scans on top of each other.
        if self._scan is not None:
            self._scan.cancel()
            self._scan = None

        self._output_dir = root
        manifest_path = root / MANIFEST_NAME
        self._manifest = Manifest(manifest_path)

        if not manifest_path.exists():
            # Browsing a folder with no manifest (typically the SD card).
            # The tab still walks the tree so the user can see what's there,
            # but favorite/playlist edits will silently no-op — warn once.
            self.status.setText(
                f"No manifest at {manifest_path}. You can browse tracks, "
                "but favorite/playlist edits won't stick — point Library at "
                "the local Echo-Library tree (where build wrote) to manage "
                "metadata."
            )
        else:
            self.status.setText("Scanning…")

        self._populate_tree(root)

    def _populate_tree(self, root: Path) -> None:
        # Block itemChanged signals while we populate, otherwise every
        # setCheckState fires _on_item_changed.
        self.tree.blockSignals(True)
        self.tree.clear()
        self._artists.clear()
        self._albums.clear()

        # Precompute a flat lookup so the background scanner doesn't need
        # to walk the Manifest object (which isn't thread-safe to share).
        lookup: dict[str, tuple[bool, list[str]]] = {}
        if self._manifest:
            for entry in self._manifest.all_entries():
                lookup[entry.target] = (entry.favorite, list(entry.playlists))

        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)  # busy indicator until 'started' fires

        self._scan = LibraryScanRunner(root, lookup)
        self._scan.signals.started.connect(self._on_scan_started)
        self._scan.signals.track.connect(self._on_scan_track)
        self._scan.signals.finished.connect(self._on_scan_finished)
        self._scan.signals.cancelled.connect(self._on_scan_cancelled)
        self._scan.signals.error.connect(self._on_scan_error)
        self._pool.start(self._scan)

    def _on_scan_started(self, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)

    def _on_scan_track(self, payload: dict) -> None:
        artist = payload["artist"]
        album = payload["album"]

        artist_item = self._artists.get(artist)
        if artist_item is None:
            artist_item = QTreeWidgetItem([artist, "", "", "", ""])
            self.tree.addTopLevelItem(artist_item)
            self._artists[artist] = artist_item
            artist_item.setExpanded(True)

        album_key = (artist, album)
        album_item = self._albums.get(album_key)
        if album_item is None:
            album_item = QTreeWidgetItem([album, "", "", "", ""])
            artist_item.addChild(album_item)
            self._albums[album_key] = album_item

        playlists_str = ", ".join(payload["playlists"])
        track_item = QTreeWidgetItem([
            payload["filename"],
            "",
            payload["genre"],
            payload["bitrate"],
            playlists_str,
        ])
        track_item.setCheckState(
            self.COL_FAV,
            Qt.CheckState.Checked if payload["favorite"]
            else Qt.CheckState.Unchecked,
        )
        track_item.setData(0, Qt.ItemDataRole.UserRole, payload["path"])
        album_item.addChild(track_item)
        self.progress.setValue(self.progress.value() + 1)

    def _on_scan_finished(self, count: int) -> None:
        self.tree.blockSignals(False)
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        self._scan = None
        no_manifest = bool(self._manifest) and not self._manifest.path.exists()
        prefix = "(no manifest — edits won't persist) " if no_manifest else ""
        self.status.setText(
            f"{prefix}{len(self._artists)} artists, "
            f"{len(self._albums)} albums, {count} FLAC tracks"
        )

    def _on_scan_cancelled(self) -> None:
        self.tree.blockSignals(False)
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        self._scan = None
        self.status.setText("Scan cancelled.")

    def _on_scan_error(self, msg: str) -> None:
        self.tree.blockSignals(False)
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        self._scan = None
        QMessageBox.warning(self, "Scan error", msg)

    def _cancel_scan(self) -> None:
        if self._scan is not None:
            self._scan.cancel()

    def _is_favorite(self, flac_path: Path) -> bool:
        if not self._manifest:
            return False
        target_str = str(flac_path)
        for entry in self._manifest.all_entries():
            if entry.target == target_str:
                return entry.favorite
        return False

    def _playlists_for(self, flac_path: Path) -> list[str]:
        if not self._manifest:
            return []
        target_str = str(flac_path)
        for entry in self._manifest.all_entries():
            if entry.target == target_str:
                return list(entry.playlists)
        return []

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != self.COL_FAV:
            return
        target = item.data(0, Qt.ItemDataRole.UserRole)
        if not target or not self._manifest:
            return
        value = item.checkState(self.COL_FAV) == Qt.CheckState.Checked
        updated = self._manifest.set_favorite(Path(target), value)
        if updated:
            self._manifest.save()
            self.favorites_changed.emit()

    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        target = item.data(0, Qt.ItemDataRole.UserRole)
        if not target or not self._manifest:
            return  # not a track row
        target_path = Path(target)

        menu = QMenu(self)
        existing_playlists = self._manifest.playlist_names()
        current = set(self._playlists_for(target_path))

        if existing_playlists:
            add_menu = menu.addMenu("Add to playlist")
            for name in existing_playlists:
                if name in current:
                    continue
                action = QAction(name, self)
                action.triggered.connect(
                    lambda _, n=name: self._toggle_playlist(target_path, n, True))
                add_menu.addAction(action)
            if not any(n for n in existing_playlists if n not in current):
                stub = QAction("(already in all)", self)
                stub.setEnabled(False)
                add_menu.addAction(stub)

            if current:
                remove_menu = menu.addMenu("Remove from playlist")
                for name in sorted(current):
                    action = QAction(name, self)
                    action.triggered.connect(
                        lambda _, n=name: self._toggle_playlist(target_path, n, False))
                    remove_menu.addAction(action)

            menu.addSeparator()

        new_action = QAction("New playlist with this track…", self)
        new_action.triggered.connect(lambda: self._new_playlist_with(target_path))
        menu.addAction(new_action)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _toggle_playlist(self, target: Path, name: str, add: bool) -> None:
        if not self._manifest:
            return
        if add:
            self._manifest.add_to_playlist(target, name)
        else:
            self._manifest.remove_from_playlist(target, name)
        self._manifest.save()
        # Update just this row's Playlists column rather than reloading the
        # whole tree.
        for item in self._iter_track_items():
            if item.data(0, Qt.ItemDataRole.UserRole) == str(target):
                item.setText(self.COL_PLAYLISTS,
                             ", ".join(self._playlists_for(target)))
                break
        self.playlists_changed.emit()

    def _new_playlist_with(self, target: Path) -> None:
        name, ok = QInputDialog.getText(
            self, "New playlist", "Playlist name:")
        if not ok or not name.strip():
            return
        self._toggle_playlist(target, name.strip(), True)

    def _iter_track_items(self):
        """Yield every leaf (track) item in the tree."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            artist = root.child(i)
            for j in range(artist.childCount()):
                album = artist.child(j)
                for k in range(album.childCount()):
                    yield album.child(k)
