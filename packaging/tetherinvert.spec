# PyInstaller spec for TetherInvert, on macOS (TetherInvert.app) and Linux (a folder). Run through
# packaging/macos/build.sh or packaging/linux/build.sh, which set the environment below.
#
#   TETHERINVERT_CODESIGN   macOS: codesign identity; unset = ad hoc ("-"), enough to run on the Mac that built it
#   TETHERINVERT_ARCH       macOS: arm64 (default) or x86_64; universal2 is not possible: the wheels are per architecture
#
# One folder, not one file: a one-file app unpacks some 400 MB to a temporary folder at every start.

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, get_package_paths

ROOT = Path(SPECPATH).parent
MACOS = sys.platform == "darwin"
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "tetherinvert" / "__init__.py").read_text()).group(1)
IDENTITY = os.environ.get("TETHERINVERT_CODESIGN") or None

# gphoto2's camera and port drivers are opened by libgphoto2 at run time (libltdl), so the
# import graph does not see them. They must keep their place below the package:
# gphoto2/__init__.py points CAMLIBS and IOLIBS at gphoto2/libgphoto2/{camlibs,iolibs}.
# collect_dynamic_libs alone misses them: on macOS it matches only lib*.so and *.dylib, and the
# drivers are called ptp2.so, usb1.so. Without them the app starts and never finds a camera.
GPHOTO2 = Path(get_package_paths("gphoto2")[1])
drivers = [(str(lib), str(lib.parent.relative_to(GPHOTO2.parent))) for lib in (GPHOTO2 / "libgphoto2").rglob("*.so")]
binaries = collect_dynamic_libs("gphoto2") + drivers
# The window's fonts and their licences (SIL OFL 1.1, which asks for the licence beside the fonts).
fonts = [(str(ROOT / "tetherinvert" / "window" / "fonts"), "tetherinvert/window/fonts")]
datas = collect_data_files("gphoto2") + [(str(ROOT / "tetherinvert" / "tetherinvert.lrplugin"), "tetherinvert/tetherinvert.lrplugin")] + fonts + [(str(ROOT / "LICENSE"), ".")]

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    # TetherInvert imports these inside functions. imagecodecs loads each codec on first use, so
    # without its submodules every TIFF write (zlib with predictor) fails in the app.
    hiddenimports=["tetherinvert.window.mainwindow", "tetherinvert.diagnose", "tetherinvert.sim"] + collect_submodules("imagecodecs"),
    excludes=["tkinter", "pytest", "lupa", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tetherinvert",
    console=False,
    target_arch=os.environ.get("TETHERINVERT_ARCH", "arm64") if MACOS else None,
    codesign_identity=IDENTITY if MACOS else None,
    entitlements_file=str(Path(SPECPATH) / "macos" / "entitlements.plist") if MACOS and IDENTITY else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="tetherinvert")
if MACOS:
    app = BUNDLE(
        coll,
        name="TetherInvert.app",
        icon=str(Path(SPECPATH) / "macos" / "tetherinvert.icns"),  # from docs/logo.svg by make_icon.sh
        bundle_identifier="io.github.janpfischer.tetherinvert",
        version=VERSION,
        info_plist={
            "CFBundleName": "TetherInvert",
            "CFBundleDisplayName": "TetherInvert",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSApplicationCategoryType": "public.app-category.photography",
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Copyright © 2026 janpfischer. GPL-3.0-or-later.",
        },
    )
