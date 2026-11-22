"""The window's keyboard shortcuts drawn on a Mac (ANSI) keyboard, as SVG.

Help › Keyboard Shortcuts draws it when it is opened (`svg()`), so nothing has to be shipped beside
the program; the manual's picture (docs/manual/img/keyboard.svg) is the same drawing, written by

    python -m tetherinvert.keyboard

and a test checks that the two agree. The correction keys come from the window's own table
(viewer.KEYS), so they cannot drift; the other keys are written out here (`KEYS`), from
the window's event filter (window/frames.py) and its shortcuts (Space, Esc, Home, in
window/mainwindow.py): change it with them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .window import viewer

MANUAL_COPY = Path(__file__).resolve().parent.parent / "docs" / "manual" / "img" / "keyboard.svg"
U, GAP, PAD = 64, 6, 28  # px: a key's unit, the gap between keys, the margin
GROUPS = {  # fill, outline, the legend's words
    "any": ("#dbe9f7", "#5b8fc7", "Anywhere in the window"),
    "orient": ("#e3f1dc", "#6aa84f", "Turn and mirror: the live view, or the frame shown"),
    "adjust": ("#fde3cf", "#e8590c", "Corrections, on a frame (Shift: the other way)"),
    "frame": ("#efe4f6", "#9467bd", "On a frame"),
}
CORRECTIONS = {"d": "D", "c": "C", "m": "M", "y": "Y", "grad": "Contrast", "tone": "Tone"}  # viewer.KEYS' names as the Develop column shows them
KEYS = {  # the key's name on the drawing: (group, what it does)
    "esc": ("any", "Live view"), "tab": ("any", "Last frame"), "space": ("any", "Scan"),
    "N": ("orient", "Turn"), "M": ("orient", "Mirror"),
    "Y": ("adjust", "Sat"),  # the key right of T, by its place (viewer.saturation_key); Y on this drawing's US layout
    "H": ("frame", "Hold"), "X": ("frame", "Reset"), "C": ("frame", "Compare"), "L": ("frame", "Metadata"),
    "delete": ("frame", "Delete frame"),
    "←": ("frame", "Frame"), "→": ("frame", "Frame"), "↑": ("frame", "Base"), "↓": ("frame", "Base"),
}  # fmt: skip
ROWS = [  # name, width in units, height in units
    [("esc", 1.5, 0.62)] + [(f"F{i}", 1.0909, 0.62) for i in range(1, 13)],
    [(k, 1, 1) for k in "`1234567890-="] + [("delete", 1.5, 1)],
    [("tab", 1.5, 1)] + [(k, 1, 1) for k in "QWERTYUIOP[]"] + [("\\", 1.5, 1)],
    [("caps lock", 1.75, 1)] + [(k, 1, 1) for k in "ASDFGHJKL;'"] + [("return", 2.25, 1)],
    [("shift", 2.25, 1)] + [(k, 1, 1) for k in "ZXCVBNM,./"] + [("shift", 2.75, 1)],
    [("fn", 1, 1), ("control", 1, 1), ("option", 1, 1), ("command", 1.25, 1), ("space", 5, 1), ("command", 1.25, 1), ("option", 1, 1)],
]
NOTES = [
    "Shift with a key: N turns left · G softer · the key right of T (Y; Z on a German keyboard) less saturation · T tone back · Shift+Q/W/E/R the same as A/S/D/F.",
    "Tab: on the live view, hold it to see the last frame scanned; on a frame, back to the live view.   C: hold to see the frame without its corrections.",
    "esc (or Home) goes back to the live view.   Space scans the next frame in a session.   Hold a correction key to run its value up.",
]


def used() -> dict[str, tuple[str, str]]:
    """Every key on the drawing that does something: the correction keys from viewer.KEYS, then KEYS."""
    keys = {}
    for key, (name, sign) in viewer.KEYS.items():
        words = CORRECTIONS[name]
        keys[chr(key.value)] = ("adjust", f"{words} {'+' if sign > 0 else '−'}" if len(words) == 1 else words)
    return {**keys, **KEYS}


def svg() -> str:
    """The drawing, as an SVG document."""
    keys, out = used(), []

    def rect(x, y, w, h, fill, stroke, r=7, line=1.2) -> None:
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{line}"/>')

    def text(x, y, words, size, weight=400, fill="#222") -> None:
        out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="start">{escape(words)}</text>')

    def key(x, y, w, h, label) -> None:
        if label not in keys:
            rect(x, y, w, h, "#f4f4f2", "#cfcfcb")
            text(x + 8, y + (h * 0.55 if h < U else 20), label, 11 if len(label) > 2 else 13, 400, "#9a9a96")
            return
        group, does = keys[label]
        fill, stroke, _ = GROUPS[group]
        rect(x, y, w, h, fill, stroke, line=2)
        if h < U * 0.7:  # a short key: name and action on one line
            text(x + 7, y + h / 2 + 5, label, 14 if len(label) == 1 else 13, 700, "#1a1a1a")
            text(x + (21 if len(label) == 1 else 34), y + h / 2 + 4, does, 10 if len(label) == 1 else 11, 500, "#333")
            return
        text(x + 8, y + 22, label, 17 if len(label) <= 2 else 13, 700, "#1a1a1a")
        text(x + 8, y + h - 10, does, 10 if w < U else 11, 500, "#333")

    y = PAD + 56
    for row in ROWS:
        x, tallest = PAD, 0
        for label, w, h in row:
            key(x, y, w * U - GAP, h * U - GAP, label)
            x += w * U
            tallest = max(tallest, h * U)
        y += tallest
    arrows, left, half = y - U, PAD + 12 * U, (U - GAP) / 2 - 1  # an inverted T in the bottom row's last three units
    key(left, arrows + half + 2, U - GAP, half, "←")
    key(left + U, arrows, U - GAP, half, "↑")
    key(left + U, arrows + half + 2, U - GAP, half, "↓")
    key(left + 2 * U, arrows + half + 2, U - GAP, half, "→")

    for i, (fill, stroke, words) in enumerate(GROUPS.values()):  # the legend, in two columns
        x, top = PAD + (i % 2) * 470, y + 20 + (i // 2) * 30
        rect(x, top, 22, 22, fill, stroke, r=5, line=2)
        text(x + 32, top + 16, words, 13, 500, "#333")
    line = y + 100
    for note in NOTES:
        text(PAD, line, note, 13, 400, "#444")
        line += 22
    width, height = 15 * U + 2 * PAD, line + PAD - 8
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" '
        "font-family=\"-apple-system, BlinkMacSystemFont, 'Helvetica Neue', Helvetica, Arial, sans-serif\">",
        "<title>TetherInvert keyboard shortcuts</title>",
        f'<rect width="{width:.0f}" height="{height:.0f}" rx="14" fill="#ffffff"/>',
        f'<text x="{PAD}" y="{PAD + 22}" font-size="22" font-weight="700" fill="#1a1a1a">TetherInvert keyboard shortcuts</text>',
        f'<text x="{PAD + 330}" y="{PAD + 22}" font-size="14" fill="#555">Keys do nothing while you type in a text field.</text>',
    ]
    return "\n".join(head + out + ["</svg>"]) + "\n"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else MANUAL_COPY
    target.write_text(svg())
    print(target)
