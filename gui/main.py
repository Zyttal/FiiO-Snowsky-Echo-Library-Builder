"""QApplication entry point for the echo-library-builder GUI.

Run with `python -m gui` from the project root, or via the installed
`echo-gui` entry point.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `import build_library` work whether launched as `python -m gui` from
# the repo root or as a PyInstaller-bundled binary that sits alongside it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QTabWidget,
)

from gui.build_tab import BuildTab
from gui.download_tab import DownloadTab
from gui.ffmpeg_probe import find_ffmpeg, install_hint
from gui.library_tab import LibraryTab
from gui.upload_tab import UploadTab


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("echo-library-builder")
        self.resize(1100, 720)

        self.ffmpeg_path = find_ffmpeg()

        tabs = QTabWidget()
        self.build_tab = BuildTab(self.ffmpeg_path)
        self.library_tab = LibraryTab()
        self.download_tab = DownloadTab()
        self.upload_tab = UploadTab()
        tabs.addTab(self.download_tab, "Download")
        tabs.addTab(self.build_tab, "Build")
        tabs.addTab(self.library_tab, "Library")
        tabs.addTab(self.upload_tab, "Upload to Device")
        self.setCentralWidget(tabs)

        # Cross-tab wiring: build completion refreshes the library tree
        # and pre-fills the Upload tab's library root; finished downloads
        # hint that the next build will pick up new files; playlist
        # changes from the Library tab refresh the Upload tab's lists.
        self.build_tab.build_finished.connect(self.library_tab.reload_if_loaded)
        self.build_tab.build_finished.connect(self.upload_tab.set_library_root)
        self.library_tab.playlists_changed.connect(self.upload_tab._reload)
        self.upload_tab.library_changed.connect(self.library_tab.reload_if_loaded)
        self.download_tab.download_finished.connect(self._on_downloads_finished)

    def _on_downloads_finished(self, dest_root: Path) -> None:
        # Pre-fill the Build tab's source field so the user can immediately
        # build what they just downloaded.
        if not self.build_tab.source_edit.text():
            self.build_tab.source_edit.setText(str(dest_root))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.ffmpeg_path is None:
            QMessageBox.warning(
                self,
                "ffmpeg not found",
                install_hint(),
            )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("echo-library-builder")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
