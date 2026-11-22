"""What the Help menu shows: where the manual is, and which TetherInvert this is.

The build is said in words, so a tester can paste it into an issue: the app carries it in `_build.py`,
written by packaging/{macos,linux}/build.sh (commit, date, GitHub run); a source checkout asks git; anything
else is "installed from source". No Qt here, so it is tested without a window.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from . import __version__

REPOSITORY = "https://github.com/janpfischer/tetherinvert"
MANUAL_URL = f"{REPOSITORY}/blob/main/docs/manual/README.md"  # the manual describes main (docs/manual/README.md)


def build() -> str:
    """Which build this is, in words."""
    try:
        from ._build import BUILD  # written into the app by packaging/{macos,linux}/build.sh, not in git

        return BUILD
    except ImportError:
        pass
    root = Path(__file__).resolve().parent.parent
    if (root / ".git").exists():
        try:
            git = lambda *args: subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=2).stdout.strip()  # noqa: E731
            commit = git("rev-parse", "--short", "HEAD")
            changed = git("status", "--porcelain", "--untracked-files=no")
            if commit:
                return f"source checkout, commit {commit}" + (" with local changes" if changed else "")
        except (OSError, subprocess.SubprocessError):
            pass
    return "installed from source"


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def details() -> list[tuple[str, str]]:
    """The About box's lines, label and value: TetherInvert, its build, and what it runs on."""
    lines = [("Version", __version__), ("Build", build())]
    system = platform.mac_ver()[0]
    lines.append(("System", f"macOS {system} ({platform.machine()})" if system else platform.platform()))
    lines.append(("Python", sys.version.split()[0]))
    try:
        from PySide6 import __version__ as pyside
        from PySide6.QtCore import qVersion

        lines.append(("Qt", f"{qVersion()} (PySide6 {pyside})"))
    except ImportError:
        lines.append(("Qt", "not installed"))
    try:
        import gphoto2

        lines.append(("Camera library", f"libgphoto2 {gphoto2.gp_library_version(gphoto2.GP_VERSION_SHORT)[0]} (python-gphoto2 {gphoto2.__version__})"))
    except Exception as exc:  # no camera library is news here, not a failure
        lines.append(("Camera library", f"not usable ({exc})"))
    try:
        import rawpy

        lines.append(("RAW decoder", f"LibRaw {'.'.join(map(str, rawpy.libraw_version))} (rawpy {rawpy.__version__})"))
    except ImportError:
        lines.append(("RAW decoder", "not installed"))
    lines += [("NumPy", _version("numpy")), ("OpenCV", _version("opencv-python-headless")), ("tifffile", _version("tifffile"))]
    return lines


def text() -> str:
    """The details as plain text, for the clipboard: what an issue needs."""
    return f"TetherInvert {__version__}\n" + "\n".join(f"{label}: {value}" for label, value in details())
