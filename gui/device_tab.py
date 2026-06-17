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
        self.pull_btn = QPushButton("Pull favorites from device")
        self.push_btn = QPushButton("Push Favorites.m3u to card")
        self.pull_btn.clicked.connect(self._pull)
        self.push_btn.clicked.connect(self._push)
        btn_row.addWidget(self.pull_btn)
        btn_row.addWidget(self.push_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

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
        from src.favorites import read_device_favorites
        sd = self.sd_edit.text().strip()
        if not sd:
            QMessageBox.warning(self, "No SD card path", "Set the SD card root first.")
            return
        sd_root = Path(sd).expanduser().resolve()
        if not sd_root.is_dir():
            QMessageBox.warning(self, "Not a folder", f"{sd_root} is not a folder.")
            return
        tracks = read_device_favorites(sd_root)
        self.list.clear()
        if not tracks:
            self.status.setText(
                "No favorites file found on the SD card. The Echo may keep "
                "favorites in internal flash only — use Push to seed one."
            )
            return
        for t in tracks:
            self.list.addItem(QListWidgetItem(str(t)))
        self.status.setText(f"Found {len(tracks)} favorites.")

    def _push(self) -> None:
        from src.favorites import write_playlist
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
        # We push only FLAC favorites — the Echo's M3U scanner picks tracks
        # by path, and the primary format is the canonical one. (If the
        # user has only an MP3 library on the card, change this.)
        favs = manifest.favorites(fmt="flac") or manifest.favorites()
        if not favs:
            QMessageBox.information(
                self, "No favorites",
                "No tracks are marked favorite in the manifest. "
                "Use the Library tab's star column to mark some first.",
            )
            return

        tracks = [Path(e.target) for e in favs]
        written = write_playlist(sd_root, tracks)
        skipped = sum(1 for t in tracks if sd_root not in t.resolve().parents)
        msg = f"Wrote {len(tracks) - skipped} tracks to {written.name}."
        if skipped:
            msg += (f"\n({skipped} favorited tracks live outside the SD card "
                    "root and were skipped — copy the library to the card "
                    "first, then push again.)")
        QMessageBox.information(self, "Pushed", msg)
        self.status.setText(msg)

    def note_favorites_changed(self) -> None:
        """Called when the Library tab updates a favorite — just hint to the
        user that a push would now have different content."""
        cur = self.status.text()
        suffix = "  [favorites changed — push to refresh the card]"
        if suffix not in cur:
            self.status.setText((cur + suffix).strip())
