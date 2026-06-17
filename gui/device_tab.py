"""Device tab — point at the Echo's SD card root, pull/push favorites.

Push writes a CRLF M3U at the SD card root from manifest favorites.
Pull is best-effort: probes for plausible favorites storage on the card
(hidden FiiO dirs, SQLite, plain playlists). Shows whatever it finds.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class DeviceTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._build_layout()

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)

        # Library row first — it's the canonical source of favorites/manifest.
        row2 = QHBoxLayout()
        self.lib_edit = QLineEdit()
        self.lib_edit.setPlaceholderText(
            "Local Echo-Library root (where build wrote, contains the manifest)…"
        )
        lib_browse = QPushButton("Browse…")
        lib_browse.clicked.connect(self._pick_lib)
        row2.addWidget(QLabel("Local library:"))
        row2.addWidget(self.lib_edit)
        row2.addWidget(lib_browse)
        outer.addLayout(row2)

        # SD card row — where the .m3u backup gets written.
        row = QHBoxLayout()
        self.sd_edit = QLineEdit()
        self.sd_edit.setPlaceholderText(
            "Mounted Echo SD card (where the .m3u backup is written)…"
        )
        sd_browse = QPushButton("Browse…")
        sd_browse.clicked.connect(self._pick_sd)
        row.addWidget(QLabel("SD card:"))
        row.addWidget(self.sd_edit)
        row.addWidget(sd_browse)
        outer.addLayout(row)

        # Help line clarifying why both paths exist.
        help_label = QLabel(
            "<small>The <b>local library</b> is your computer-side "
            "Echo-Library/ tree (the manifest with favorites lives there). "
            "The <b>SD card</b> is the mounted Echo, where the .m3u export "
            "is written. They're usually different paths because the manifest "
            "doesn't get rsynced to the card.</small>"
        )
        help_label.setWordWrap(True)
        outer.addWidget(help_label)

        # Action buttons
        btn_row = QHBoxLayout()
        self.pull_btn = QPushButton("Scan SD card for existing .m3u backups")
        self.push_btn = QPushButton("Export favorites as .m3u backup")
        self.pull_btn.clicked.connect(self._pull)
        self.push_btn.clicked.connect(self._push)
        btn_row.addWidget(self.pull_btn)
        btn_row.addWidget(self.push_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        caveat = QLabel(
            "<small><b>Heads up:</b> the Echo has no MTP mode, so its "
            "internal Favorites list (in internal flash) can't be read "
            "from the host. Only the SD card is visible, and the device "
            "doesn't write favorites there. The Scan button just looks "
            "for .m3u files we (or you) have previously exported to the "
            "card — not what's favorited on-device.<br>"
            "<br>"
            "The Export button writes a standard CRLF .m3u as a "
            "<i>backup</i>, not for on-device playback (FiiO has stated "
            "the chip can't play M3U). FW V1.3.0 fixed library re-scans "
            "from clearing Favorites, but firmware flashes still reformat "
            "internal storage — so the export is your restore path across "
            "FW updates, and for reading on any other player.</small>"
        )
        caveat.setWordWrap(True)
        outer.addWidget(caveat)

        # Results list
        outer.addWidget(QLabel("Detected favorites on device:"))
        self.list = QListWidget()
        outer.addWidget(self.list)

        self.status = QLabel("")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)

    def _pick_sd(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose SD card root",
            self.sd_edit.text() or "/run/media" if not self._is_win() else "",
        )
        if path:
            self.sd_edit.setText(path)

    def _pick_lib(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Choose output library root",
            self.lib_edit.text() or self.sd_edit.text() or str(Path.home()),
        )
        if path:
            self.lib_edit.setText(path)

    @staticmethod
    def _is_win() -> bool:
        import sys
        return sys.platform.startswith("win")

    def _pull(self) -> None:
        from src.favorites import read_device_favorites_report
        sd = self.sd_edit.text().strip()
        if not sd:
            QMessageBox.warning(self, "No SD card path", "Set the SD card root first.")
            return
        sd_root = Path(sd).expanduser().resolve()
        if not sd_root.is_dir():
            QMessageBox.warning(self, "Not a folder", f"{sd_root} is not a folder.")
            return

        report = read_device_favorites_report(sd_root)
        self.list.clear()

        if not report.any_source_found:
            self.status.setText(
                "No .m3u found on the SD card. The Echo has no MTP mode, "
                "so its on-device Favorites list (in internal flash) is "
                "unreachable — this scan only surfaces files we (or another "
                "tool) have exported to the card. Click \"Export favorites "
                "as .m3u backup\" above to create one."
            )
            return

        sources = []
        if report.m3u_files:
            sources.append(f"{len(report.m3u_files)} M3U")
        if report.sqlite_files:
            sources.append(f"{len(report.sqlite_files)} sqlite")
        if report.text_files:
            sources.append(f"{len(report.text_files)} text list")
        src_str = ", ".join(sources)

        if not report.tracks:
            for p in report.m3u_files + report.sqlite_files + report.text_files:
                self.list.addItem(QListWidgetItem(f"({p}) — empty"))
            self.status.setText(
                f"Found {src_str} on the card but with zero track entries. "
                "Almost always: a previous Export ran with the SD card pointing "
                "at a folder that didn't already contain your library, so "
                "every track got skipped. Copy your library to the card "
                "(rsync) and re-run Export."
            )
            return

        for t in report.tracks:
            label = str(t)
            if t in report.tracks_missing:
                label += "   (missing on card)"
            self.list.addItem(QListWidgetItem(label))
        msg = f"Found {len(report.tracks)} favorites across {src_str}."
        if report.tracks_missing:
            msg += (f" {len(report.tracks_missing)} are referenced but not "
                    "physically on the card.")
        self.status.setText(msg)

    def _push(self) -> None:
        from src.favorites import EmptyPlaylistError, write_playlist
        from src.manifest import MANIFEST_NAME, Manifest

        sd = self.sd_edit.text().strip()
        if not sd:
            QMessageBox.warning(self, "No SD card path", "Set the SD card root first.")
            return
        sd_root = Path(sd).expanduser().resolve()
        if not sd_root.is_dir():
            QMessageBox.warning(self, "Not a folder", f"{sd_root} is not a folder.")
            return

        lib_text = self.lib_edit.text().strip() or sd
        lib_root = Path(lib_text).expanduser().resolve()
        if not (lib_root / MANIFEST_NAME).exists():
            QMessageBox.warning(
                self, "No manifest at the local library path",
                f"Couldn't find {MANIFEST_NAME} under {lib_root}.\n\n"
                "Set 'Local library' to the folder where build wrote your "
                "Echo-Library/ tree (the one with the manifest). The SD card "
                "usually doesn't have the manifest because the standard rsync "
                "command excludes it.\n\n"
                "Example: Local library = /mnt/games/Music/Echo-Library/, "
                "SD card = /media/zyttal/ECHO/.",
            )
            return

        manifest = Manifest(lib_root / MANIFEST_NAME)
        # Prefer FLAC favorites for the export — the primary format is the
        # canonical one in the manifest. Fall back to any format if no FLAC
        # favorites exist. (The Echo can't play the .m3u directly anyway;
        # this just affects which paths land in the backup.)
        # No format filter — manifest entries are keyed by (source, fmt)
        # but the favorite bit lives on whichever entry is in the
        # manifest. In preserve mode that's fmt="preserve"; in fixed-
        # format mode it's "flac"/"mp3"/etc. Querying without a filter
        # covers all of them.
        favs = manifest.favorites()
        if not favs:
            QMessageBox.information(
                self, "No favorites",
                "No tracks are marked favorite in the manifest. "
                "Use the Library tab's star column to mark some first.",
            )
            return

        tracks = [Path(e.target) for e in favs]
        try:
            written = write_playlist(sd_root, tracks, lib_root=lib_root)
        except EmptyPlaylistError as e:
            QMessageBox.critical(
                self, "Nothing to export",
                f"All {e.skipped} favorited tracks live outside the local "
                f"library root ({lib_root}).\n\n"
                "That usually means the manifest references paths under a "
                "different folder than the one set as 'Local library'. "
                "Point 'Local library' at the same folder you ran build into.",
            )
            return

        n = len(tracks)
        skipped = sum(1 for t in tracks if lib_root not in t.resolve().parents)
        msg = f"Exported {n - skipped}/{n} tracks to {written.name}."
        if skipped:
            msg += (f"\n({skipped} favorited tracks live outside the local "
                    "library root and were skipped.)")
        QMessageBox.information(self, "Exported", msg)
        self.status.setText(msg)

    def note_favorites_changed(self) -> None:
        """Called when the Library tab updates a favorite — just hint to the
        user that the export is now out of date."""
        cur = self.status.text()
        suffix = "  [favorites changed — re-export to refresh the backup]"
        if suffix not in cur:
            self.status.setText((cur + suffix).strip())
