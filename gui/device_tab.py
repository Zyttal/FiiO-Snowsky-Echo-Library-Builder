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

        # SD card row
        row = QHBoxLayout()
        self.sd_edit = QLineEdit()
        self.sd_edit.setPlaceholderText("Mounted SD card root…")
        sd_browse = QPushButton("Browse…")
        sd_browse.clicked.connect(self._pick_sd)
        row.addWidget(QLabel("SD card:"))
        row.addWidget(self.sd_edit)
        row.addWidget(sd_browse)
        outer.addLayout(row)

        # Library row (manifest source for push)
        row2 = QHBoxLayout()
        self.lib_edit = QLineEdit()
        self.lib_edit.setPlaceholderText(
            "Output library root (defaults to SD card root if blank)…"
        )
        lib_browse = QPushButton("Browse…")
        lib_browse.clicked.connect(self._pick_lib)
        row2.addWidget(QLabel("Library:"))
        row2.addWidget(self.lib_edit)
        row2.addWidget(lib_browse)
        outer.addLayout(row2)

        # Action buttons
        btn_row = QHBoxLayout()
        self.pull_btn = QPushButton("Probe SD card for existing favorites")
        self.push_btn = QPushButton("Export favorites as .m3u backup")
        self.pull_btn.clicked.connect(self._pull)
        self.push_btn.clicked.connect(self._push)
        btn_row.addWidget(self.pull_btn)
        btn_row.addWidget(self.push_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        caveat = QLabel(
            "<small><b>Heads up:</b> FiiO has stated the Snowsky Echo's "
            "chip can't play M3U playlists — the export writes a standard "
            "CRLF .m3u as a <i>backup</i>, not for on-device playback. "
            "FW V1.3.0 (April 2026) fixed routine media-library re-scans "
            "from clearing Favorites, but firmware flashes themselves may "
            "still reformat internal storage (per FiiO's install notes), so "
            "the export is your restore path across firmware updates and "
            "for reading on any other player.</small>"
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
                "No favorites file found on the SD card. The Echo keeps its "
                "internal Favorites in flash (not on the card), so Pull only "
                "surfaces files written by Push or by another tool. Use Push "
                "to export one."
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
                "Almost always: a previous Push ran with the SD card pointing "
                "at a folder that didn't already contain your library, so "
                "every track got skipped. Copy your library to the card "
                "(rsync) and re-run Push."
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
                self, "No manifest",
                f"Couldn't find {MANIFEST_NAME} under {lib_root}. "
                "Run a build first so there's a library to draw favorites from.",
            )
            return

        manifest = Manifest(lib_root / MANIFEST_NAME)
        # Prefer FLAC favorites for the export — the primary format is the
        # canonical one in the manifest. Fall back to any format if no FLAC
        # favorites exist. (The Echo can't play the .m3u directly anyway;
        # this just affects which paths land in the backup.)
        favs = manifest.favorites(fmt="flac") or manifest.favorites()
        if not favs:
            QMessageBox.information(
                self, "No favorites",
                "No tracks are marked favorite in the manifest. "
                "Use the Library tab's star column to mark some first.",
            )
            return

        tracks = [Path(e.target) for e in favs]
        try:
            written = write_playlist(sd_root, tracks)
        except EmptyPlaylistError as e:
            QMessageBox.critical(
                self, "Nothing to export",
                f"All {e.skipped} favorited tracks live outside the SD card "
                f"root you set ({sd_root}).\n\n"
                "The library has to physically be on the SD card before the "
                "M3U paths can make sense. Steps:\n\n"
                "  1. rsync your Echo-Library tree to the SD card\n"
                "  2. Point this tab at <SD card>/Music/ (or wherever the "
                "library landed on the card)\n"
                "  3. Push again",
            )
            return

        n = len(tracks)
        skipped = sum(1 for t in tracks if sd_root not in t.resolve().parents)
        msg = f"Exported {n - skipped}/{n} tracks to {written.name}."
        if skipped:
            msg += (f"\n({skipped} favorited tracks live outside the SD card "
                    "root and were skipped — copy the library to the card "
                    "first, then export again.)")
        QMessageBox.information(self, "Exported", msg)
        self.status.setText(msg)

    def note_favorites_changed(self) -> None:
        """Called when the Library tab updates a favorite — just hint to the
        user that the export is now out of date."""
        cur = self.status.text()
        suffix = "  [favorites changed — re-export to refresh the backup]"
        if suffix not in cur:
            self.status.setText((cur + suffix).strip())
