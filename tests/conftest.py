"""Qt runs offscreen in the tests, on a screen as small as the smallest the window is made for: a
13-inch MacBook Air at its default 1440 × 900, less the menu bar. Qt's own offscreen screen is
800 × 800, smaller than the window's minimum."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCREEN = (1440, 875)

_config = Path(tempfile.mkdtemp(prefix="tetherinvert-screen-")) / "screen.json"
_config.write_text(json.dumps({"screens": [{"name": "smallest", "x": 0, "y": 0, "width": SCREEN[0], "height": SCREEN[1], "logicalDpi": 96}]}))
os.environ.setdefault("QT_QPA_PLATFORM", f"offscreen:configfile={_config}")
