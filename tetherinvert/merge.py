"""Assemble one frame from its red, green and blue exposures.

Each output channel comes from the exposure lit in that colour, so no channel ever shared
the sensor with another. Green and blue are registered to red first: film and camera drift
by a fraction of a pixel between exposures, which would show as colour fringes.

The shift is estimated on edge strength, not on brightness. The three dye layers record
different pictures (a red object is dense in one layer and thin in another), but their edges
sit in the same places.
"""

from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

COARSE_WIDTH = 1024  # whole frame, reduced: robust, but only good to about half a pixel
FINE_SIZE = 2048  # centre crop at full resolution: good to a tenth or two of a pixel


def _edges(plane: np.ndarray) -> np.ndarray:
    smooth = cv2.GaussianBlur(np.log1p(plane.astype(np.float32)), (0, 0), 1.0)
    return cv2.magnitude(cv2.Sobel(smooth, cv2.CV_32F, 1, 0), cv2.Sobel(smooth, cv2.CV_32F, 0, 1))


def _correlate(reference: np.ndarray, moving: np.ndarray) -> tuple[float, float]:
    window = cv2.createHanningWindow((reference.shape[1], reference.shape[0]), cv2.CV_32F)
    (dx, dy), _ = cv2.phaseCorrelate(_edges(reference), _edges(moving), window)
    return dx, dy


def estimate_shift(reference: np.ndarray, moving: np.ndarray) -> tuple[float, float]:
    """Sub-pixel (dx, dy) of `moving` relative to `reference`."""
    height, width = reference.shape[:2]
    scale = max(1.0, width / COARSE_WIDTH)
    size = (int(round(width / scale)), int(round(height / scale)))
    dx, dy = _correlate(cv2.resize(reference, size, interpolation=cv2.INTER_AREA), cv2.resize(moving, size, interpolation=cv2.INTER_AREA))
    coarse = (dx * scale, dy * scale)
    if scale == 1.0:
        return coarse
    h, w = min(height, FINE_SIZE), min(width, FINE_SIZE)
    y, x = (height - h) // 2, (width - w) // 2
    fine = _correlate(reference[y : y + h, x : x + w], moving[y : y + h, x : x + w])
    # The centre crop is the better measurement unless it has nothing to hold on to (open sky).
    agrees = max(abs(fine[0] - coarse[0]), abs(fine[1] - coarse[1])) <= max(2.0, 1.5 * scale)
    return fine if agrees else coarse


def merge_triplet(
    red: np.ndarray,
    green: np.ndarray,
    blue: np.ndarray,
    align: bool = True,
    shifts: Optional[list[tuple[float, float]]] = None,
) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Returns the merged H x W x 3 image and the (dx, dy) applied to green and blue.
    Pass `shifts` from an earlier merge of the same exposures to register them identically."""
    known = list(shifts) if shifts is not None else None
    if not (red.shape == green.shape == blue.shape):
        raise ValueError(f"the three exposures differ in size: {red.shape}, {green.shape}, {blue.shape}")
    height, width = red.shape[:2]
    merged = np.empty_like(red)
    merged[..., 0] = red[..., 0]
    shifts = []
    for index, exposure in ((1, green), (2, blue)):
        plane = exposure[..., index]
        if known is not None:
            dx, dy = known[index - 1]
        else:
            dx, dy = estimate_shift(red[..., 0], plane) if align else (0.0, 0.0)
        # An estimate this large means the correlation failed, not that the film jumped.
        if max(abs(dx), abs(dy)) > max(16.0, 0.02 * width):
            dx, dy = 0.0, 0.0
        if abs(dx) > 0.02 or abs(dy) > 0.02:
            matrix = np.float32([[1, 0, -dx], [0, 1, -dy]])
            plane = cv2.warpAffine(plane, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        merged[..., index] = plane
        shifts.append((float(dx), float(dy)))
    return merged, shifts
