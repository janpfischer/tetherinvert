"""Entry point of the frozen app: the macOS bundle and the Linux folder.

Opened from the Finder or a desktop launcher the app has no arguments, so it opens the window.
With arguments it is the ordinary command line, so the app also serves as `tetherinvert`:

    /Applications/TetherInvert.app/Contents/MacOS/tetherinvert status
    ~/TetherInvert/tetherinvert status

`--selftest` is for the build only: it checks the parts a frozen app loses silently. Above all
gphoto2's camera drivers (camlibs) and port drivers (iolibs), which libgphoto2 opens at run
time from a folder found next to gphoto2/__init__.py. If they are missing the app still starts
but never finds a camera.
"""

from __future__ import annotations

import os
import sys


def selftest() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import tempfile
    from pathlib import Path

    import numpy as np
    import tifffile

    from tetherinvert import __version__, lightroom
    from tetherinvert.camera import load_gphoto

    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(("ok    " if ok else "FAIL  ") + what)
        if not ok:
            failures.append(what)

    gp = load_gphoto()
    abilities = gp.CameraAbilitiesList()
    abilities.load()
    models = [abilities[i].model for i in range(abilities.count())]
    check(len(models) > 1000, f"camera drivers loaded: {len(models)} models (from {os.environ.get('CAMLIBS')})")
    check(any("A6700" in model for model in models), "the Sony A6700 is among them")
    ports = gp.PortInfoList()
    ports.load()
    check(any(ports[i].get_path().startswith("usb:") for i in range(ports.count())), f"USB port driver loaded (from {os.environ.get('IOLIBS')})")

    lua = sorted(path.name for path in lightroom.PLUGIN.glob("*.lua"))
    check("Watcher.lua" in lua and "Info.lua" in lua, f"Lightroom plug-in shipped: {len(lua)} files in {lightroom.PLUGIN}")

    from tetherinvert.tiffio import write_tiff

    with tempfile.TemporaryDirectory() as folder:
        path, image = Path(folder) / "t.tif", np.arange(3 * 300 * 40, dtype=np.uint16).reshape(300, 40, 3)
        write_tiff(path, image, 2.2, "self-test")  # zlib with predictor: needs imagecodecs' lazily loaded codecs
        check(bool((tifffile.imread(path) == image).all()), "a 16-bit TIFF is written and read back unchanged")

    import cv2
    import rawpy

    check(bool(cv2.__version__) and bool(rawpy.libraw_version), f"OpenCV {cv2.__version__}, LibRaw {'.'.join(map(str, rawpy.libraw_version))}")

    from PySide6.QtWidgets import QApplication

    from tetherinvert.window import mainwindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    check(app is not None and mainwindow.MainWindow is not None, "Qt and the window module load")

    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer

    from tetherinvert import about, keyboard

    check(QSvgRenderer(QByteArray(keyboard.svg().encode())).isValid(), "Qt's SVG module draws the keyboard shortcuts (Help › Keyboard Shortcuts)")

    from tetherinvert.window import style

    families = style.load_fonts()  # data files: left out of the app, the window falls back to the system's fonts without a word
    check({style.TEXT_FAMILY, style.NUMBER_FAMILY} <= families, f"the window's fonts load: {', '.join(sorted(families)) or 'none'}")
    check(not about.build().startswith("installed"), f"the build is known: {about.build()}")

    print(f"TetherInvert {__version__}: " + ("self-test passed" if not failures else f"{len(failures)} check(s) failed"))
    return 1 if failures else 0


def main() -> int:
    args = sys.argv[1:]
    if args == ["--selftest"]:
        return selftest()
    if not args or args == ["--sim"]:
        args += ["gui"]
    from tetherinvert.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    sys.exit(main())
