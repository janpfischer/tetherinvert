"""The window (PySide6).

Imported lazily: `contact.py` uses the viewer's pictures, and the window's threads (workers.py) write
contact sheets, so importing the window here would import contact.py from inside itself.
"""

from __future__ import annotations


def main(sim: bool = False) -> int:
    from .mainwindow import main as run

    return run(sim)
