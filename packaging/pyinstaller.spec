# PyInstaller spec for echo-library-builder GUI.
#
# Build from the repo root:
#   pyenv exec pyinstaller packaging/pyinstaller.spec
#
# The bundled ffmpeg is picked up from packaging/ffmpeg/<os>/ if present.
# Each OS's build script (build_linux.sh / build_macos.sh / build_windows.ps1)
# is responsible for placing the right binary there before invoking
# PyInstaller.

import sys
from pathlib import Path

# The .spec is exec()'d by PyInstaller — __file__ is unset, so use CWD.
PROJECT_ROOT = Path.cwd()
PACKAGING = PROJECT_ROOT / "packaging"

if sys.platform.startswith("win"):
    ffmpeg_dir = PACKAGING / "ffmpeg" / "windows"
    ffmpeg_binary = "ffmpeg.exe"
elif sys.platform == "darwin":
    ffmpeg_dir = PACKAGING / "ffmpeg" / "macos"
    ffmpeg_binary = "ffmpeg"
else:
    ffmpeg_dir = PACKAGING / "ffmpeg" / "linux"
    ffmpeg_binary = "ffmpeg"

binaries = []
ffmpeg_path = ffmpeg_dir / ffmpeg_binary
if ffmpeg_path.exists():
    # Drop alongside the executable so gui.ffmpeg_probe finds it first.
    binaries.append((str(ffmpeg_path), "."))

# Bundle config.yaml so first-run reads sensible defaults.
datas = [
    (str(PROJECT_ROOT / "config.yaml"), "."),
]

a = Analysis(
    [str(PROJECT_ROOT / "gui" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        # build_library.py is imported by gui.workers at runtime;
        # PyInstaller doesn't see the import via a string.
        "build_library",
        "src.config",
        "src.convert",
        "src.cover",
        "src.favorites",
        "src.layout",
        "src.manifest",
        "src.sanitize",
        "src.scan",
        "src.tags",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="echo-library-builder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# On macOS, wrap the exe in an .app bundle.
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="echo-library-builder.app",
        icon=None,
        bundle_identifier="com.snowsky.echo-library-builder",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
