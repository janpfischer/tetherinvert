"""Small pictures of a roll's frames, for the window's strip and viewer and the contact sheet.

`render()` leaves one per frame in `<roll>/preview/`: the merged linear image reduced to EDGE
pixels on its long side, with the points its flat TIFF was written with. It covers what the flat
TIFF covers: cropped to the picture area when the TIFF is (`cropped`). It is kept the way the
camera sees the frame. Every frame is shown developed from it (development.py), in a few
milliseconds; it keeps working after the RAW files are deleted.

The Lightroom plug-in lists only the folder it watches and adds only .tif files, so it never
sees these.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np

from . import invert, paths

EDGE = 1600  # px on the long side: about the viewer's size on a Retina screen, about 10 MB per frame (grain does not compress, so it is stored plainly)
FOLDER = "preview"


@dataclass
class Preview:
    image: np.ndarray  # linear uint16, (h, w, 3), or (h, w) for black-and-white
    bounds: invert.Bounds  # the points the frame's flat TIFF was written with
    cropped: bool = False  # cut to the picture area, like its TIFF
    rect: Optional[tuple[float, float, float, float]] = None  # the picture area at render, in the camera's whole frame


def crop(image: np.ndarray, rect: Optional[Sequence[float]]) -> np.ndarray:
    """The picture area x, y, w, h (fractions) of an image in the camera's frame; all of it for None."""
    if rect is None:
        return image
    height, width = image.shape[:2]
    x, y, w, h = rect
    return np.ascontiguousarray(image[int(y * height) : int((y + h) * height), int(x * width) : int((x + w) * width)])


def path(roll: paths.Place, number: int) -> Path:
    return Path(roll.folder) / FOLDER / f"{paths.stem(roll.name, number)}.npz"


def reduce(image: np.ndarray, edge: int = EDGE) -> np.ndarray:
    height, width = image.shape[:2]
    scale = edge / max(height, width)
    if scale >= 1:
        return image
    return cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)


def write(roll: paths.Place, number: int, image: np.ndarray, bounds: invert.Bounds, rect=None, cropped: bool = False) -> Path:
    """`image` is the whole frame; with `cropped` only `rect` of it is kept, as in the TIFF."""
    target = path(roll, number)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    with open(partial, "wb") as file:
        kept = reduce(crop(image, rect if cropped else None))
        np.savez(file, image=kept, base=bounds.base, dense=bounds.dense, thin=bounds.thin, cropped=cropped, rect=list(rect or []))
    partial.replace(target)  # the window may be reading the one before
    return target


def load(roll: paths.Place, number: int) -> Optional[Preview]:
    """The frame's preview, or None when there is none (not rendered yet) or it cannot be read."""
    try:
        with np.load(path(roll, number)) as data:
            points = (tuple(float(v) for v in data[key]) for key in ("base", "dense", "thin"))
            rect = tuple(float(v) for v in data["rect"]) if len(data["rect"]) == 4 else None
            return Preview(data["image"], invert.Bounds(*points), bool(data["cropped"]), rect)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None
