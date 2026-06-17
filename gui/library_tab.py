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

        # Destructive actions row — kept visible but red-tinted to remind
        # the user this writes to disk.
        danger_row = QHBoxLayout()
        danger_row.addWidget(QLabel("Library actions:"))
        self.empty_btn = QPushButton("Empty library…")
        self.empty_btn.setStyleSheet(
            "QPushButton { color: #b00; } QPushButton:disabled { color: #777; }"
        )
        self.empty_btn.setToolTip(
            "Delete every audio file under the loaded library root. "
            "Preserves the manifest, cover.jpgs, and non-music files "
            "(System Volume Information, FiiO info text, Trash). "
            "Only operates on whatever path you've loaded — never touches "
            "the device's internal storage."
        )
        self.empty_btn.clicked.connect(self._empty_library)
        danger_row.addWidget(self.empty_btn)
        danger_row.addStretch()
        outer.addLayout(danger_row)

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
        track_path = Path(payload["path"])

        artist_item = self._artists.get(artist)
        if artist_item is None:
            artist_item = QTreeWidgetItem([artist, "", "", "", ""])
            self.tree.addTopLevelItem(artist_item)
            self._artists[artist] = artist_item
            artist_item.setExpanded(True)
            # Store the artist folder path so delete-artist can target it.
            artist_item.setData(0, Qt.ItemDataRole.UserRole + 1,
                                str(track_path.parent.parent))

        album_key = (artist, album)
        album_item = self._albums.get(album_key)
        if album_item is None:
            album_item = QTreeWidgetItem([album, "", "", "", ""])
            artist_item.addChild(album_item)
            self._albums[album_key] = album_item
            album_item.setData(0, Qt.ItemDataRole.UserRole + 1,
                               str(track_path.parent))

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
        menu = QMenu(self)

        target = item.data(0, Qt.ItemDataRole.UserRole)
        if target:
            # Track row — playlist actions + delete track
            target_path = Path(target)
            self._append_playlist_actions(menu, target_path)
            menu.addSeparator()
            del_action = QAction("Delete track", self)
            del_action.triggered.connect(
                lambda: self._delete_tracks([target_path]))
            menu.addAction(del_action)
        else:
            # Artist or album row — delete folder
            folder = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if folder:
                level = "album" if item.parent() else "artist"
                count = self._count_descendant_tracks(item)
                del_action = QAction(
                    f"Delete {level} ({count} track{'s' if count != 1 else ''})",
                    self,
                )
                del_action.triggered.connect(
                    lambda: self._delete_folder(Path(folder), item))
                menu.addAction(del_action)

        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _append_playlist_actions(self, menu, target_path: Path) -> None:
        if not self._manifest:
            return
        existing_playlists = self._manifest.playlist_names()
        current = set(self._playlists_for(target_path))

        if existing_playlists:
            add_menu = menu.addMenu("Add to playlist")
            available = [n for n in existing_playlists if n not in current]
            for name in available:
                action = QAction(name, self)
                action.triggered.connect(
                    lambda _, n=name: self._toggle_playlist(target_path, n, True))
                add_menu.addAction(action)
            if not available:
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

        new_action = QAction("New playlist with this track…", self)
        new_action.triggered.connect(lambda: self._new_playlist_with(target_path))
        menu.addAction(new_action)

    @staticmethod
    def _count_descendant_tracks(item: QTreeWidgetItem) -> int:
        if item.childCount() == 0:
            return 1 if item.data(0, Qt.ItemDataRole.UserRole) else 0
        n = 0
        for i in range(item.childCount()):
            n += LibraryTab._count_descendant_tracks(item.child(i))
        return n

    def _delete_tracks(self, paths: list[Path]) -> None:
        if not self._output_dir:
            return
        if not paths:
            return
        msg = (
            f"Delete {len(paths)} file{'s' if len(paths) != 1 else ''} "
            f"from {self._output_dir}?\n\n"
            "This deletes the audio file(s) on disk; the device's internal "
            "Favorites list (in its flash) is not touched."
        )
        reply = QMessageBox.question(
            self, "Delete tracks", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        removed = 0
        manifest_dirty = False
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                QMessageBox.warning(self, "Delete failed", f"{path}: {e}")
                continue
            if self._manifest and self._manifest.path.exists():
                if self._manifest.forget_target(path) > 0:
                    manifest_dirty = True

        if manifest_dirty:
            self._manifest.save()

        self._reload()
        self.status.setText(f"Deleted {removed} track(s).")

    def _empty_library(self) -> None:
        """Wipe every audio file under the loaded library root, plus the
        Artist/Album folder structure. Preserves the manifest itself, FiiO
        info text files, .Trash-1000, and System Volume Information."""
        if not self._output_dir:
            QMessageBox.warning(self, "No library loaded",
                                "Load a library root first.")
            return

        root = self._output_dir
        from src.manifest import MANIFEST_NAME

        # Pick out only the top-level directories that look like Artist/
        # folders — i.e., they have at least one Album/Track shape below
        # them. Don't touch top-level files or directories that don't
        # match the layout.
        artist_dirs: list[Path] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name in (".Trash-1000", "System Volume Information",
                              "Playlists"):
                continue
            # Has at least one audio file inside (somewhere)? Then treat
            # it as an Artist folder.
            if any(
                p.suffix.lower() in (".flac", ".mp3", ".m4a", ".ogg",
                                     ".wav", ".ape", ".dsf")
                for p in child.rglob("*")
            ):
                artist_dirs.append(child)

        if not artist_dirs:
            QMessageBox.information(
                self, "Already empty",
                f"No Artist/Album folders found under {root}.",
            )
            return

        track_count = sum(
            sum(1 for p in d.rglob("*")
                if p.suffix.lower() in (".flac", ".mp3", ".m4a", ".ogg",
                                        ".wav", ".ape", ".dsf"))
            for d in artist_dirs
        )

        # Two-step confirmation. First a Yes/No, then a typed phrase, so
        # an accidental Enter on the first dialog doesn't wipe the card.
        first = QMessageBox.warning(
            self, "Empty library?",
            f"This will delete:\n\n"
            f"  • {track_count} audio files\n"
            f"  • {len(artist_dirs)} artist folders under {root}\n\n"
            "Preserved:\n"
            f"  • {MANIFEST_NAME} (so your favorite/playlist marks survive)\n"
            "  • Top-level files (FiiO info .txt, etc.)\n"
            "  • System Volume Information, .Trash-1000\n"
            "  • Playlists/ folder (use Playlists tab to clear those)\n\n"
            "The Echo's internal Favorites list in its flash is NOT touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            return

        phrase, ok = QInputDialog.getText(
            self, "Confirm empty",
            f"Type EMPTY to wipe {len(artist_dirs)} folders from {root}:",
        )
        if not ok or phrase.strip() != "EMPTY":
            self.status.setText("Empty cancelled.")
            return

        import shutil
        failed: list[tuple[Path, str]] = []
        for d in artist_dirs:
            try:
                shutil.rmtree(d)
            except OSError as e:
                failed.append((d, str(e)))

        if self._manifest and self._manifest.path.exists():
            # Drop every entry whose target is under this root.
            self._manifest.forget_targets_under(root)
            self._manifest.save()

        if failed:
            QMessageBox.warning(
                self, "Some deletes failed",
                "Couldn't delete:\n" + "\n".join(f"  {p}: {e}"
                                                  for p, e in failed[:10]),
            )

        self._reload()
        msg = f"Emptied {len(artist_dirs) - len(failed)}/{len(artist_dirs)} folders."
        self.status.setText(msg)

    def _delete_folder(self, folder: Path, tree_item: QTreeWidgetItem) -> None:
        if not self._output_dir:
            return
        if folder == self._output_dir or self._output_dir not in folder.parents:
            QMessageBox.critical(
                self, "Refusing to delete",
                f"{folder} isn't strictly inside the loaded library "
                f"({self._output_dir}). Refusing.",
            )
            return
        import shutil
        count = self._count_descendant_tracks(tree_item)
        msg = (
            f"Delete the folder\n  {folder}\nand all {count} track"
            f"{'s' if count != 1 else ''} inside it?\n\n"
            "Only files on this SD card / local library are affected — the "
            "Echo's internal Favorites list is not touched."
        )
        reply = QMessageBox.question(
            self, "Delete folder", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            shutil.rmtree(folder)
        except OSError as e:
            QMessageBox.warning(self, "Delete failed", f"{folder}: {e}")
            return
        if self._manifest and self._manifest.path.exists():
            self._manifest.forget_targets_under(folder)
            self._manifest.save()
        self._reload()
        self.status.setText(f"Deleted {folder.name} ({count} tracks).")

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
