"""Flat-field correction: divide out uneven light and lens falloff.

At the start of a scanning session the bare light is photographed once per colour, without
film. What the sensor sees then is the light and the lens alone: how much dimmer each part of
the frame is than the brightest part. Every scan of the session is divided by that picture.

The picture is stored small and smooth (the unevenness is smooth; dust and noise are not
wanted in it) as `flatfield.npz` inside the roll, so a roll can always be rendered again
exactly as it was.

The correction is anchored where the preset was calibrated: there the scan stays as exposed,
brighter regions are scaled down to it, dimmer ones up. The film base measured at calibration
is then the film base everywhere in the frame.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import cv2
import numpy as np

FILE = "flatfield.npz"
MAP_LONG_EDGE = 512
BLUR_SIGMA = 3.0  # in map pixels
LIT_SHARE = 0.3  # of the brightest value: darker than this is holder, not light
MIN_RELATIVE = 0.25  # never brighten by more than 4x
MAX_TEXTURE = 0.05  # bare light is smooth; more structure than this means film is in the way


class FlatFieldError(RuntimeError):
    pass


def relative_map(plane: np.ndarray) -> tuple[np.ndarray, float]:
    """One exposure of the bare light -> (brightness relative to the brightest part, texture).

    Inside the lit area the map is 1.0 at the brightest part and smaller elsewhere. Outside it
    (the holder) it continues the nearest lit value, so nothing odd happens at the edge.
    """
    height, width = plane.shape[:2]
    scale = MAP_LONG_EDGE / max(height, width)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    small = cv2.resize(plane.astype(np.float32), size, interpolation=cv2.INTER_AREA)
    lit = (small > LIT_SHARE * np.percentile(small, 99.5)).astype(np.uint8)
    lit = cv2.erode(lit, np.ones((5, 5), np.uint8))  # stay off the holder's edge, where pixels are half lit
    if lit.sum() < 0.05 * lit.size:
        raise FlatFieldError("Almost nothing in the frame is lit. Is the light on and uncovered?")
    weight = lit.astype(np.float32)
    # Blur only across lit pixels, so the dark holder does not bleed into the map.
    smooth = cv2.GaussianBlur(small * weight, (0, 0), BLUR_SIGMA) / np.maximum(cv2.GaussianBlur(weight, (0, 0), BLUR_SIGMA), 1e-3)
    inside = lit > 0
    texture = _structure(cv2.GaussianBlur(small, (0, 0), 1.0), inside)
    relative = np.clip(smooth / smooth[inside].max(), MIN_RELATIVE, 1.0)
    # Continue the map outwards: every unlit pixel takes the value of the nearest lit one.
    _, nearest = cv2.distanceTransformWithLabels(1 - lit, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    lookup = np.ones(int(nearest.max()) + 1, np.float32)
    lookup[nearest[inside]] = relative[inside]
    return lookup[nearest].astype(np.float32), texture


def _structure(image: np.ndarray, inside: np.ndarray) -> float:
    """How much of the lit area is not explained by a smooth surface, as a share of its level.

    Light and lens falloff vary gently across the frame, so a low-order surface fits them
    almost perfectly. A picture on film does not fit at all.
    """
    rows, cols = np.nonzero(inside)
    step = max(1, len(rows) // 20000)
    rows, cols = rows[::step], cols[::step]
    x, y = cols / image.shape[1] - 0.5, rows / image.shape[0] - 0.5
    terms = np.stack([x**i * y**j for i in range(4) for j in range(4 - i)], axis=1)  # up to third order
    values = image[rows, cols].astype(np.float64)
    fitted = terms @ np.linalg.lstsq(terms, values, rcond=None)[0]
    return float(np.std(values - fitted) / max(values.mean(), 1e-6))


@dataclass
class FlatField:
    kind: str  # kinds.KINDS, like the preset it was taken with
    maps: np.ndarray  # height x width x 3, float32, relative brightness per colour
    created: str = ""

    @staticmethod
    def build(kind: str, planes: Sequence[np.ndarray], own: bool = False) -> "FlatField":
        """From the bare-light exposures: three single planes (R, G, B), or one RGB image when a
        single exposure lit every channel (white, rgb-together): a map per sensor channel from it."""
        if len(planes) == 1:
            planes = [planes[0][..., channel] for channel in range(3)]
        maps, textures = zip(*(relative_map(plane) for plane in planes), strict=True)
        if max(textures) > MAX_TEXTURE:
            if own:  # an own light dimmed by pulse width shows bands at the bare light's short shutter
                raise FlatFieldError(
                    f"The light does not look bare: {max(textures):.0%} of it is structure, not smooth falloff. Either film is in the "
                    "holder, or the light flickers at this shutter speed: run it at full brightness."
                )
            raise FlatFieldError(
                f"The light does not look bare: {max(textures):.0%} of it is structure, not smooth falloff. "
                "Take the film out of the holder and capture the flat field again."
            )
        return FlatField(kind, np.stack(maps, axis=-1), time.strftime("%Y-%m-%d %H:%M"))

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / (FILE + ".part.npz")
        np.savez_compressed(partial, maps=self.maps.astype(np.float32), meta=np.array(json.dumps({"kind": self.kind, "created": self.created})))
        partial.replace(folder / FILE)

    @staticmethod
    def load(folder: Path) -> Optional["FlatField"]:
        try:
            with np.load(folder / FILE, allow_pickle=False) as data:
                meta = json.loads(str(data["meta"]))
                return FlatField(meta["kind"], data["maps"].astype(np.float32), meta.get("created", ""))
        except (OSError, ValueError, KeyError):
            return None

    def falloff(self) -> float:
        """How much dimmer the dimmest lit part is than the brightest, worst colour: 0.12 = 12 %."""
        return float(1.0 - self.maps.min())

    def at(self, roi: Sequence[float]) -> tuple[float, float, float]:
        """Relative brightness per colour inside a rectangle (x, y, w, h as fractions)."""
        height, width = self.maps.shape[:2]
        x, y, w, h = roi
        x0, y0 = min(int(x * width), width - 1), min(int(y * height), height - 1)
        area = self.maps[y0 : max(int(np.ceil((y + h) * height)), y0 + 1), x0 : max(int(np.ceil((x + w) * width)), x0 + 1)]
        middle = np.median(area.reshape(-1, 3), axis=0)
        return float(middle[0]), float(middle[1]), float(middle[2])

    def correct(self, plane: np.ndarray, channel: int, anchor: float) -> np.ndarray:
        """Even out one uint16 plane of colour `channel`, keeping the level where the map reads `anchor`."""
        height, width = plane.shape[:2]
        if abs(width / height - self.maps.shape[1] / self.maps.shape[0]) > 0.02:
            raise FlatFieldError("The flat field was taken with a different frame shape than this scan.")
        relative = cv2.resize(self.maps[..., channel], (width, height), interpolation=cv2.INTER_LINEAR)
        corrected = plane.astype(np.float32) * (anchor / relative)
        return np.clip(corrected + 0.5, 0, 65535).astype(np.uint16)
