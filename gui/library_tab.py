"""Library tab — browse the output, toggle favorites, refresh on rebuild.

Reads the existing manifest at <output>/.echo-library-manifest.json plus the
on-disk FLACs (for genre + bitrate). Tree structure is Artist > Album > Track.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from gui.workers import LibraryScanRunner, LyricsRunner


class LibraryTab(QWidget):
    favorites_changed = Signal()
    playlists_changed = Signal()

    COL_NAME = 0
    COL_ALBUM = 1
    COL_FAV = 2
    COL_GENRE = 3
    COL_YEAR = 4
    COL_FORMAT = 5
    COL_BITRATE = 6
    COL_DURATION = 7
    COL_PLAYLISTS = 8
    COLUMN_COUNT = 9
    # Thumbnail target side-length on the leftmost cell. Qt scales the
    # cached pixmap down to this when rendering the row.
    THUMB_PX = 36

    def __init__(self) -> None:
        super().__init__()
        self._output_dir: Path | None = None
        self._manifest = None  # src.manifest.Manifest, set on load
        self._scan: LibraryScanRunner | None = None
        self._lyrics_runner: LyricsRunner | None = None
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

        # Live search across the tree — matches against track filename,
        # album, or artist. Empty needle restores the full tree.
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Filter by artist / album / track…"
        )
        self.search_edit.textChanged.connect(self._on_search_changed)
        search_row.addWidget(self.search_edit)
        outer.addLayout(search_row)

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
        self.lyrics_btn = QPushButton("Fetch lyrics")
        self.lyrics_btn.setToolTip(
            "For every track in the loaded library, query LRCLIB and drop "
            "a <track>.lrc sidecar next to the audio. Free public API, no "
            "key required. Skips tracks that already have a sidecar. "
            "Echo and other DAPs that read .lrc files display the synced "
            "lyrics on playback."
        )
        self.lyrics_btn.clicked.connect(self._fetch_lyrics)
        danger_row.addWidget(self.lyrics_btn)
        danger_row.addStretch()
        outer.addLayout(danger_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(self.COLUMN_COUNT)
        self.tree.setHeaderLabels([
            "Artist / Album / Track", "Album", "Favorite", "Genre", "Year",
            "Format", "Bitrate", "Duration", "Playlists",
        ])
        self.tree.header().setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(self.COL_ALBUM, QHeaderView.ResizeMode.ResizeToContents)
        for col in (self.COL_FAV, self.COL_GENRE, self.COL_YEAR,
                    self.COL_FORMAT, self.COL_BITRATE, self.COL_DURATION,
                    self.COL_PLAYLISTS):
            self.tree.header().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setIconSize(QSize(self.THUMB_PX, self.THUMB_PX))
        # In-memory per-album thumbnail cache so a 30-track album decodes
        # its cover once, not 30 times. Keyed by album folder path.
        self._album_thumbs: dict[str, "QIcon"] = {}
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        # Ctrl-click for individual additions, Shift-click for a range —
        # so the user can grab everything from "AC/DC" to "ZZ Top" or
        # one whole artist row and drop the lot into a playlist.
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
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
        album_dir = str(track_path.parent)

        artist_item = self._artists.get(artist)
        if artist_item is None:
            artist_item = QTreeWidgetItem([artist] + [""] * (self.COLUMN_COUNT - 1))
            self.tree.addTopLevelItem(artist_item)
            self._artists[artist] = artist_item
            artist_item.setExpanded(True)
            # Store the artist folder path so delete-artist can target it.
            artist_item.setData(0, Qt.ItemDataRole.UserRole + 1,
                                str(track_path.parent.parent))

        album_key = (artist, album)
        album_item = self._albums.get(album_key)
        if album_item is None:
            row = [album] + [""] * (self.COLUMN_COUNT - 1)
            row[self.COL_ALBUM] = album
            album_item = QTreeWidgetItem(row)
            artist_item.addChild(album_item)
            self._albums[album_key] = album_item
            album_item.setData(0, Qt.ItemDataRole.UserRole + 1, album_dir)
            icon = self._album_icon(album_dir, payload.get("cover_bytes"))
            if icon is not None:
                album_item.setIcon(self.COL_NAME, icon)

        playlists_str = ", ".join(payload["playlists"])
        row = [
            payload["filename"],
            album,
            "",
            payload["genre"],
            payload.get("year", ""),
            payload.get("format", ""),
            payload["bitrate"],
            payload.get("duration", ""),
            playlists_str,
        ]
        track_item = QTreeWidgetItem(row)
        track_item.setCheckState(
            self.COL_FAV,
            Qt.CheckState.Checked if payload["favorite"]
            else Qt.CheckState.Unchecked,
        )
        track_item.setData(0, Qt.ItemDataRole.UserRole, payload["path"])
        icon = self._album_icon(album_dir, payload.get("cover_bytes"))
        if icon is not None:
            track_item.setIcon(self.COL_NAME, icon)
        album_item.addChild(track_item)
        self.progress.setValue(self.progress.value() + 1)

    def _album_icon(self, album_dir: str, cover_bytes: bytes | None) -> QIcon | None:
        """Return a cached QIcon for the album's cover, or None when no
        cover is available. Decoded once per album folder."""
        cached = self._album_thumbs.get(album_dir)
        if cached is not None:
            return cached
        if not cover_bytes:
            return None
        pm = QPixmap()
        if not pm.loadFromData(cover_bytes):
            return None
        scaled = pm.scaled(
            self.THUMB_PX, self.THUMB_PX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        icon = QIcon(scaled)
        self._album_thumbs[album_dir] = icon
        return icon

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

    def _on_search_changed(self, text: str) -> None:
        """Live-filter the tree by typed text. Empty needle restores the
        full tree. A leaf is shown when the needle appears in the track
        filename, the album row text, or the artist row text; matching
        leaves drag their ancestors visible too."""
        needle = text.strip().lower()
        if not needle:
            for i in range(self.tree.topLevelItemCount()):
                _show_recursive(self.tree.topLevelItem(i), True)
            return
        # Hide everything first, then unhide matches + ancestors.
        for i in range(self.tree.topLevelItemCount()):
            _show_recursive(self.tree.topLevelItem(i), False)
        for item in self._iter_track_items():
            album_item = item.parent()
            artist_item = album_item.parent() if album_item else None
            hay = " ".join((
                item.text(self.COL_NAME),
                album_item.text(self.COL_NAME) if album_item else "",
                artist_item.text(self.COL_NAME) if artist_item else "",
            )).lower()
            if needle in hay:
                cur = item
                while cur is not None:
                    cur.setHidden(False)
                    cur = cur.parent()

    def _fetch_lyrics(self) -> None:
        """Spawn LyricsRunner over the loaded library. Idempotent; skips
        tracks that already have a sidecar."""
        if self._output_dir is None:
            QMessageBox.warning(self, "No library loaded",
                                "Load a library first.")
            return
        if self._lyrics_runner is not None:
            return
        self.lyrics_btn.setEnabled(False)
        self.status.setText("Lyrics: starting…")
        # Reuse the scan progress bar — busy indicator (range 0..0) until
        # the runner's `started` signal hands us the real total.
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self._lyrics_runner = LyricsRunner(self._output_dir, overwrite=False)
        self._lyrics_runner.signals.started.connect(self._on_lyrics_started)
        self._lyrics_runner.signals.progress.connect(self._on_lyrics_progress)
        self._lyrics_runner.signals.finished.connect(self._on_lyrics_finished)
        self._lyrics_runner.signals.error.connect(self._on_lyrics_error)
        self._pool.start(self._lyrics_runner)

    def _on_lyrics_started(self, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(0)
        self.status.setText(f"Lyrics: 0/{total} tracks")

    def _on_lyrics_progress(self, i: int, total: int, label: str) -> None:
        self.progress.setValue(i)
        self.status.setText(f"Lyrics: {i}/{total} — {label}")

    def _on_lyrics_finished(self, fetched: int, skipped: int,
                            misses: int, errors: int) -> None:
        self.lyrics_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._lyrics_runner = None
        msg = (f"{fetched} fetched, {skipped} already-present, "
               f"{misses} no-match, {errors} errors")
        self.status.setText(f"Lyrics done. {msg}.")
        QMessageBox.information(self, "Lyrics done", msg)

    def _on_lyrics_error(self, msg: str) -> None:
        self.lyrics_btn.setEnabled(True)
        self.progress.setVisible(False)
        self._lyrics_runner = None
        QMessageBox.warning(self, "Lyrics error", msg)


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
        clicked = self.tree.itemAt(pos)
        if clicked is None:
            return

        # If the user right-clicked on a row that's part of a larger
        # selection, act on the whole selection. Otherwise act on the
        # one clicked row.
        selected = self.tree.selectedItems()
        if clicked not in selected:
            selected = [clicked]

        # Walk every selected item down to its track-row descendants;
        # dedupe paths in case the user selected both an artist row and
        # one of their album rows.
        track_paths = self._collect_track_paths(selected)
        folder_items = self._collect_folder_items(selected)

        menu = QMenu(self)

        if track_paths:
            self._append_playlist_actions(
                menu, track_paths,
                label_suffix=(f" ({len(track_paths)} tracks)"
                              if len(track_paths) > 1 else ""),
            )
            menu.addSeparator()
            verb = ("Delete tracks" if len(track_paths) > 1
                    else "Delete track")
            del_tracks = QAction(
                f"{verb} ({len(track_paths)})", self)
            del_tracks.triggered.connect(
                lambda: self._delete_tracks(track_paths))
            menu.addAction(del_tracks)

        if folder_items:
            menu.addSeparator()
            # Per-folder delete (each picks its own confirmation), since
            # different folders may be different sizes.
            for it, folder in folder_items:
                level = "album" if it.parent() else "artist"
                count = self._count_descendant_tracks(it)
                del_folder = QAction(
                    f"Delete {level} '{it.text(self.COL_NAME)}' "
                    f"({count} track{'s' if count != 1 else ''})",
                    self,
                )
                del_folder.triggered.connect(
                    lambda _, p=folder, i=it: self._delete_folder(Path(p), i))
                menu.addAction(del_folder)

        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _collect_track_paths(self, items: list[QTreeWidgetItem]) -> list[Path]:
        """Walk every selected item down to its track descendants and
        return a deduplicated list of track paths in tree order."""
        seen: set[str] = set()
        out: list[Path] = []

        def walk(it: QTreeWidgetItem) -> None:
            data = it.data(0, Qt.ItemDataRole.UserRole)
            if data:
                if data not in seen:
                    seen.add(data)
                    out.append(Path(data))
                return
            # Not a track row — recurse into children.
            for j in range(it.childCount()):
                walk(it.child(j))

        for it in items:
            walk(it)
        return out

    def _collect_folder_items(
        self, items: list[QTreeWidgetItem],
    ) -> list[tuple[QTreeWidgetItem, str]]:
        """Pick the artist/album rows from the selection so the context
        menu can offer per-folder delete actions."""
        out: list[tuple[QTreeWidgetItem, str]] = []
        for it in items:
            if it.data(0, Qt.ItemDataRole.UserRole):
                continue  # track row, not a folder
            folder = it.data(0, Qt.ItemDataRole.UserRole + 1)
            if folder:
                out.append((it, folder))
        return out

    def _append_playlist_actions(self, menu, target_paths: list[Path],
                                 label_suffix: str = "") -> None:
        if not self._manifest:
            return
        existing_playlists = self._manifest.playlist_names()
        # For a multi-track selection a playlist is "current" only when
        # every selected track is already in it. Anything narrower would
        # produce ambiguous Add/Remove labels.
        per_track_membership = [set(self._playlists_for(p)) for p in target_paths]
        if per_track_membership:
            in_all = set.intersection(*per_track_membership)
            in_any = set.union(*per_track_membership)
        else:
            in_all = set()
            in_any = set()

        if existing_playlists:
            add_menu = menu.addMenu(f"Add to playlist{label_suffix}")
            available = [n for n in existing_playlists if n not in in_all]
            for name in available:
                action = QAction(name, self)
                action.triggered.connect(
                    lambda _, n=name: self._toggle_playlist_bulk(
                        target_paths, n, True))
                add_menu.addAction(action)
            if not available:
                stub = QAction("(already in all)", self)
                stub.setEnabled(False)
                add_menu.addAction(stub)

            if in_any:
                remove_menu = menu.addMenu(f"Remove from playlist{label_suffix}")
                for name in sorted(in_any):
                    action = QAction(name, self)
                    action.triggered.connect(
                        lambda _, n=name: self._toggle_playlist_bulk(
                            target_paths, n, False))
                    remove_menu.addAction(action)

        new_action = QAction(
            ("New playlist with these tracks…" if len(target_paths) > 1
             else "New playlist with this track…"),
            self,
        )
        new_action.triggered.connect(
            lambda: self._new_playlist_with_bulk(target_paths))
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
        self._toggle_playlist_bulk([target], name, add)

    def _toggle_playlist_bulk(
        self, targets: list[Path], name: str, add: bool,
    ) -> None:
        """Add or remove `targets` from playlist `name` in one manifest
        save. Updates the Playlists column on every affected row instead
        of reloading the whole tree (which would be slow for big libraries)."""
        if not self._manifest or not targets:
            return
        for target in targets:
            if add:
                self._manifest.add_to_playlist(target, name)
            else:
                self._manifest.remove_from_playlist(target, name)
        self._manifest.save()

        # Index target strings → row item once, then update the affected
        # rows. Avoids self._iter_track_items() per-target.
        wanted = {str(p) for p in targets}
        for item in self._iter_track_items():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data in wanted:
                item.setText(self.COL_PLAYLISTS,
                             ", ".join(self._playlists_for(Path(data))))
        self.playlists_changed.emit()
        self.status.setText(
            f"{'Added' if add else 'Removed'} {len(targets)} track"
            f"{'s' if len(targets) != 1 else ''} "
            f"{'to' if add else 'from'} '{name}'."
        )

    def _new_playlist_with(self, target: Path) -> None:
        self._new_playlist_with_bulk([target])

    def _new_playlist_with_bulk(self, targets: list[Path]) -> None:
        if not targets:
            return
        name, ok = QInputDialog.getText(
            self, "New playlist",
            f"Playlist name (will hold {len(targets)} track"
            f"{'s' if len(targets) != 1 else ''}):",
        )
        if not ok or not name.strip():
            return
        self._toggle_playlist_bulk(targets, name.strip(), True)

    def _iter_track_items(self):
        """Yield every leaf (track) item in the tree."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            artist = root.child(i)
            for j in range(artist.childCount()):
                album = artist.child(j)
                for k in range(album.childCount()):
                    yield album.child(k)


def _show_recursive(item, visible: bool) -> None:
    """Hide/show an item and every descendant. Used by the search filter
    to reset the tree's visibility state in one pass before unhiding
    matches."""
    item.setHidden(not visible)
    for i in range(item.childCount()):
        _show_recursive(item.child(i), visible)
