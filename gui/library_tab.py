"""Library tab — browse the output, toggle favorites, refresh on rebuild.

Reads the existing manifest at <output>/.echo-library-manifest.json plus the
on-disk FLACs (for genre + bitrate). Tree structure is Artist > Album > Track.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
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
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


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
        row.addWidget(QLabel("Library:"))
        row.addWidget(self.path_edit)
        row.addWidget(browse)
        row.addWidget(reload_btn)
        outer.addLayout(row)

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
        self._output_dir = root
        self._manifest = Manifest(root / MANIFEST_NAME)
        self._populate_tree(root)

    def _populate_tree(self, root: Path) -> None:
        from mutagen.flac import FLAC

        # Block itemChanged signals while we populate, otherwise every
        # setCheckState fires _on_item_changed.
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            artists: dict[str, QTreeWidgetItem] = {}
            albums: dict[tuple[str, str], QTreeWidgetItem] = {}

            flac_paths = sorted(root.rglob("*.flac"))
            track_count = 0
            for flac_path in flac_paths:
                rel = flac_path.relative_to(root)
                parts = rel.parts
                if len(parts) < 3:
                    continue  # not Artist/Album/Track shape
                artist, album = parts[0], parts[1]

                artist_item = artists.get(artist)
                if artist_item is None:
                    artist_item = QTreeWidgetItem([artist, "", "", "", ""])
                    self.tree.addTopLevelItem(artist_item)
                    artists[artist] = artist_item

                album_key = (artist, album)
                album_item = albums.get(album_key)
                if album_item is None:
                    album_item = QTreeWidgetItem([album, "", "", "", ""])
                    artist_item.addChild(album_item)
                    albums[album_key] = album_item

                try:
                    flac = FLAC(flac_path)
                    genre = (flac.tags.get("GENRE") or [""])[0] if flac.tags else ""
                    bitrate = self._fmt_bitrate(flac)
                except Exception:
                    genre, bitrate = "", ""

                playlists_str = ", ".join(self._playlists_for(flac_path))
                track_item = QTreeWidgetItem([
                    flac_path.name,
                    "",
                    genre,
                    bitrate,
                    playlists_str,
                ])
                fav = self._is_favorite(flac_path)
                track_item.setCheckState(
                    self.COL_FAV,
                    Qt.CheckState.Checked if fav else Qt.CheckState.Unchecked,
                )
                track_item.setData(0, Qt.ItemDataRole.UserRole, str(flac_path))
                album_item.addChild(track_item)
                track_count += 1

            self.tree.expandToDepth(0)
            self.status.setText(
                f"{len(artists)} artists, {len(albums)} albums, "
                f"{track_count} FLAC tracks"
            )
        finally:
            self.tree.blockSignals(False)

    @staticmethod
    def _fmt_bitrate(flac) -> str:
        try:
            kbps = flac.info.bitrate / 1000
            return f"{kbps:.0f} kbps"
        except Exception:
            return ""

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
