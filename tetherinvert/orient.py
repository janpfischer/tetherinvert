"""Which way up a frame is stored: turned in quarter turns, and mirrored.

Inside TetherInvert everything stays the way the camera sees it: the rectangles, the measured
points, the flat field, the RAW files and the previews. Only what reaches the operator is
turned: the TIFFs as they are written (and the prints made from them), and every picture on the
screen. A rectangle drawn on the turned live view is turned back before it is kept
(`rect_back`), so metering and cropping never need to know.

An orientation is {"turn": 0, 90, 180 or 270 (clockwise), "mirror": bool}: turned first, then
mirrored left to right. The window's buttons act on what is seen: `turned` and `mirrored` give
the orientation after one more quarter turn or one more mirror of the picture as it looks.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

UPRIGHT = {"turn": 0, "mirror": False}


def normal(orientation: Optional[dict]) -> dict:
    """Complete and in range; None is upright."""
    o = orientation or {}
    return {"turn": int(round(int(o.get("turn", 0)) / 90)) % 4 * 90, "mirror": bool(o.get("mirror", False))}


def image(pixels: np.ndarray, orientation: Optional[dict]) -> np.ndarray:
    """The picture as the operator has it: (h, w) or (h, w, channels), any type."""
    o = normal(orientation)
    if o == UPRIGHT:
        return pixels
    pixels = np.rot90(pixels, -o["turn"] // 90)  # negative: clockwise
    if o["mirror"]:
        pixels = pixels[:, ::-1]
    return np.ascontiguousarray(pixels)


def image_back(pixels: np.ndarray, orientation: Optional[dict]) -> np.ndarray:
    """A picture as the operator has it, back as the camera saw it: `image` undone."""
    o = normal(orientation)
    if o == UPRIGHT:
        return pixels
    if o["mirror"]:
        pixels = pixels[:, ::-1]
    return np.ascontiguousarray(np.rot90(pixels, o["turn"] // 90))


def reoriented(pixels: np.ndarray, old: Optional[dict], new: Optional[dict]) -> np.ndarray:
    """A picture stored one way up (`old`), stored the other (`new`)."""
    return image(image_back(pixels, old), new)


def point(x: float, y: float, orientation: Optional[dict]) -> tuple[float, float]:
    """A point of the camera's frame (fractions) to where it is seen."""
    o = normal(orientation)
    for _ in range(o["turn"] // 90):
        x, y = 1.0 - y, x
    return (1.0 - x if o["mirror"] else x), y


def point_back(x: float, y: float, orientation: Optional[dict]) -> tuple[float, float]:
    """Where a point seen is in the camera's frame."""
    o = normal(orientation)
    if o["mirror"]:
        x = 1.0 - x
    for _ in range(o["turn"] // 90):
        x, y = y, 1.0 - x
    return x, y


def _corners(rect: Sequence[float], move) -> tuple[float, float, float, float]:
    x, y, w, h = rect
    (x0, y0), (x1, y1) = move(x, y), move(x + w, y + h)
    return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)


def rect(rect: Sequence[float], orientation: Optional[dict]) -> tuple[float, float, float, float]:
    """A rectangle x, y, w, h of the camera's frame as it is seen."""
    return _corners(rect, lambda x, y: point(x, y, orientation))


def rect_back(rect: Sequence[float], orientation: Optional[dict]) -> tuple[float, float, float, float]:
    """A rectangle drawn on the picture as seen, in the camera's frame."""
    return _corners(rect, lambda x, y: point_back(x, y, orientation))


def turned(orientation: Optional[dict], clockwise: bool = True) -> dict:
    """One more quarter turn of the picture as it looks. Mirrored, a turn to the right is a
    quarter turn to the left of what the camera sees."""
    o = normal(orientation)
    step = 90 if clockwise != o["mirror"] else 270
    return {"turn": (o["turn"] + step) % 360, "mirror": o["mirror"]}


def mirrored(orientation: Optional[dict]) -> dict:
    o = normal(orientation)
    return {"turn": o["turn"], "mirror": not o["mirror"]}


# What the buttons and keys do, by name: the live view's setting and a scanned frame change alike.
CHANGES = {
    "left": lambda o: turned(o, clockwise=False),
    "right": lambda o: turned(o, clockwise=True),
    "mirror": mirrored,
}


def label(orientation: Optional[dict]) -> str:
    o = normal(orientation)
    turn = {0: "upright", 90: "turned right", 180: "upside down", 270: "turned left"}[o["turn"]]
    return turn + (", mirrored" if o["mirror"] else "")
