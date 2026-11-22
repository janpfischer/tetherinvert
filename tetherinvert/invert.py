"""From a linear scan of a negative to a flat positive.

Film density is logarithmic, so the work happens on log10 of the linear signal:

1. Find two points per channel: the film base (most light, scene black) and the densest part
   of the frame (least light, scene white).
2. Stretch each channel between its two points. This removes the orange mask and the
   different contrast of the three dye layers.
3. Invert and place the result with headroom at both ends:  out = LIFT + GAIN * (1 - value).
   Scene black lands at 0.10, scene white at 0.75. No contrast curve is applied; the image is
   meant to look flat.

The converted positive goes one step further, the step otherwise done by hand with a tone
curve per channel: its dark end is not the film base but the first tone the picture itself
holds (a third point, `thin`: where the negative is thinnest inside the picture area). That
tone becomes 0 in every channel. The light end stays at 0.75 or is taken up to WHITE_FULL.
Still no contrast curve.

The flat positive is the roll's master: every channel is a straight line of log density with
headroom at both ends, so the log density comes back from it exactly (`from_flat`). A print is
developed from it (`base_positive`, then `edit`; development.py): a base (the flat or the converted positive, with or without its
white point; more can be added to BASES), then the corrections of a Fujifilm Frontier SP-3000's
keys: density and colour steps, saturation, contrast (the SP-3000's gradation) and tone. A
density step is an exact shift of each channel's values. With no corrections the print is the
base; there is no curve of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

FULL = 65535
LIFT, GAIN = 0.10, 0.65
WHITE_FLAT = LIFT + GAIN  # where the flat positive puts scene white
WHITE_FULL = 0.98  # where the converted positive puts it when the white point is set too
GRID = 256  # the frame is metered on a block-median grid this many cells along its long edge
CLIP_PERCENT = 0.05  # ignored at each end of the metered values
LUMA = np.array([0.2126, 0.7152, 0.0722], np.float32)

# Corrections. One colour key step in log density; the D step keeps the SP-3000's ratio of key
# step widths for negatives (C, M, Y 8; D 15). Fine steps on purpose: one D step is about 1/16 stop.
STEP_COLOUR = 0.01
STEP_DENSITY = STEP_COLOUR * 15 / 8
# Ranges. More reach than the SP-3000's (contrast there: Soft 3 .. Hard 2, saturation -9 .. +9):
# it corrects a print that already has a curve, these a flat or converted positive that has none.
# "grad" is contrast; the key keeps the SP-3000's name, which is also how roll.json stores it.
KEYS = {"d": (-80, 80), "c": (-80, 80), "m": (-80, 80), "y": (-80, 80), "grad": (-18, 18), "sat": (-24, 24)}
GRADATION_STEP = 1.1**0.5  # slope factor per contrast step: 0.42 .. 2.36 over the range
SATURATION_STEP = 0.04  # share of colour added or taken per saturation step: 0.04 .. 1.96 over the range
# SP-3000 Tone Adjustment: slope factor below and above the middle of the file's range (shadows, highlights).
TONES = {
    "standard": (1.0, 1.0),
    "all hard": (1.15, 1.15),
    "all soft": (0.87, 0.87),
    "highlight hard": (1.0, 1.2),
    "highlight soft": (1.0, 0.83),
    "shadow hard": (1.2, 1.0),
    "shadow soft": (0.83, 1.0),
}
# What a print is developed from, per frame, before its corrections; the first is the default.
# "converted" puts the picture's own first tone at black in every channel (the step otherwise done
# by hand per channel in Lightroom) and leaves white at 75 %; "converted white" takes the densest
# tone up to WHITE_FULL as well.
BASES = ("flat", "converted", "converted white")
NEUTRAL = {"d": 0, "c": 0, "m": 0, "y": 0, "grad": 0, "sat": 0, "tone": "standard"}


@dataclass(frozen=True)
class Bounds:
    """log10 signal per channel: of the film base, of the densest scene tone, and of the
    thinnest tone inside the picture (the picture's own black)."""

    base: tuple[float, float, float]
    dense: tuple[float, float, float]
    thin: tuple[float, float, float]

    def to_json(self) -> dict:
        return {"base": list(self.base), "dense": list(self.dense), "thin": list(self.thin)}

    @staticmethod
    def from_json(data: dict) -> "Bounds":
        return Bounds(tuple(data["base"]), tuple(data["dense"]), tuple(data["thin"]))


def to_log(image: np.ndarray) -> np.ndarray:
    """uint16 linear -> float32 log10 of the 0..1 signal."""
    return np.log10(np.clip(image.astype(np.float32) / FULL, 1e-6, None))


def luminance(image: np.ndarray) -> np.ndarray:
    """Collapse a linear RGB scan of black-and-white film to one channel."""
    return (image.astype(np.float32) @ LUMA).round().astype(np.uint16)


def _grid(log_image: np.ndarray, rect: Optional[Sequence[float]]) -> np.ndarray:
    """The metered area as a small block-median grid. Medians drop dust and scratches, and a
    fixed grid makes the measurement independent of resolution."""
    height, width = log_image.shape[:2]
    if rect is not None:
        x, y, w, h = rect
        x0, y0 = int(round(x * width)), int(round(y * height))
        x1, y1 = int(round((x + w) * width)), int(round((y + h) * height))
        log_image = log_image[max(y0, 0) : max(y1, y0 + 1), max(x0, 0) : max(x1, x0 + 1)]
        height, width = log_image.shape[:2]
    block = int(np.ceil(max(height, width) / GRID))
    if block <= 1 or height < block or width < block:
        return log_image.reshape(height, width, -1)
    rows, cols = height // block, width // block
    cells = log_image[: rows * block, : cols * block].reshape(rows, block, cols, block, -1)
    return np.median(cells, axis=(1, 3))


def measure(
    log_image: np.ndarray,
    rect: Optional[Sequence[float]] = None,
    base: Optional[Sequence[float]] = None,
    per_channel: bool = True,
) -> Bounds:
    """Find the points for one frame.

    rect         x, y, w, h in fractions: the picture area. Anything outside (holder, sprocket
                 holes) would skew the measurement.
    base         linear base signal per channel (0..65535) from the calibration. When given it
                 is the film base; otherwise the frame's own brightest tones are taken.
    per_channel  False keeps one density range for all channels: only the base is balanced and
                 the colour of the highlights is left as the film recorded it.
    """
    values = _grid(log_image, rect).reshape(-1, log_image.shape[-1] if log_image.ndim == 3 else 1)
    channels = values.shape[1]
    low, top = np.percentile(values, [CLIP_PERCENT, 100 - CLIP_PERCENT], axis=0)
    high = top
    if base is not None:
        high = np.log10(np.clip(np.asarray(base, np.float32)[:channels] / FULL, 1e-6, None))
    # The picture's own black. Without a picture area the brightest thing in the frame is the
    # holder's window or a sprocket hole, so the film base has to stand in.
    thin = top if rect is not None else high
    if not per_channel and channels == 3:
        low = high - float(np.mean(high - low))
        thin = high - float(np.mean(high - thin))
    low = np.minimum(low, high - 0.05)  # a blank frame must not divide by nothing
    thin = np.maximum(thin, low + 0.05)

    def three(values) -> tuple[float, float, float]:
        padded = [float(v) for v in values] + [float(values[0])] * (3 - channels)
        return padded[0], padded[1], padded[2]

    return Bounds(three(high), three(low), three(thin))


def pool(bounds: Sequence[Bounds]) -> Bounds:
    """One set of points for a whole roll: the median over its frames, so that all frames get
    the same stretch and match each other."""
    base = np.median([b.base for b in bounds], axis=0)
    dense = np.median([b.dense for b in bounds], axis=0)
    thin = np.median([b.thin for b in bounds], axis=0)
    return Bounds(tuple(float(v) for v in base), tuple(float(v) for v in dense), tuple(float(v) for v in thin))


def flat_positive(log_image: np.ndarray, bounds: Bounds) -> np.ndarray:
    """Stretch, invert and place. Returns uint16, same shape as the input."""
    channels = log_image.shape[-1] if log_image.ndim == 3 else 1
    base = np.asarray(bounds.base[:channels], np.float32)
    dense = np.asarray(bounds.dense[:channels], np.float32)
    if log_image.ndim == 2:
        base, dense = base[0], dense[0]
    value = (log_image - dense) / (base - dense)  # 0 = densest (scene white), 1 = base (scene black)
    return (np.clip(LIFT + GAIN * (1.0 - value), 0.0, 1.0) * FULL + 0.5).astype(np.uint16)


def converted_positive(log_image: np.ndarray, bounds: Bounds, white: float = WHITE_FLAT) -> np.ndarray:
    """Stretch each channel between the picture's own black and the densest tone, and invert:
    the first tone the picture holds becomes 0, scene white becomes `white`. Linear in between,
    like the flat positive. Returns uint16, same shape as the input."""
    channels = log_image.shape[-1] if log_image.ndim == 3 else 1
    thin = np.asarray(bounds.thin[:channels], np.float32)
    dense = np.asarray(bounds.dense[:channels], np.float32)
    if log_image.ndim == 2:
        thin, dense = thin[0], dense[0]
    value = (thin - log_image) / (thin - dense)  # 0 = thinnest in the picture (scene black), 1 = densest (scene white)
    return (np.clip(white * value, 0.0, 1.0) * FULL + 0.5).astype(np.uint16)


def base_positive(base: str, log_image: np.ndarray, bounds: Bounds) -> np.ndarray:
    """A base (BASES) as uint16. One place decides, so the screen and the print cannot come to
    disagree about which is which."""
    if base == "flat":
        return flat_positive(log_image, bounds)
    if base == "converted":
        return converted_positive(log_image, bounds, WHITE_FLAT)
    if base == "converted white":
        return converted_positive(log_image, bounds, WHITE_FULL)
    raise ValueError(f"no base called {base!r}; one of {BASES}")


def from_flat(flat: np.ndarray, bounds: Bounds) -> np.ndarray:
    """A flat positive (uint16) written with `bounds`, back to log10 of the linear signal: exact
    within its headroom (15 % past the film base, 38 % past the densest tone)."""
    channels = flat.shape[-1] if flat.ndim == 3 else 1
    base = np.asarray(bounds.base[:channels], np.float32)
    dense = np.asarray(bounds.dense[:channels], np.float32)
    if flat.ndim == 2:
        base, dense = base[0], dense[0]
    value = np.float32(1.0) - (flat.astype(np.float32) * np.float32(1 / FULL) - np.float32(LIFT)) * np.float32(1 / GAIN)
    return dense + value * (base - dense)


def clipped_share(image: np.ndarray, rect: Optional[Sequence[float]] = None) -> float:
    """Share of the metered area at the sensor ceiling. Clipped base cannot be recovered."""
    height, width = image.shape[:2]
    if rect is not None:
        x, y, w, h = rect
        image = image[int(y * height) : int((y + h) * height), int(x * width) : int((x + w) * width)]
    return float(np.mean(image >= 0.995 * FULL)) if image.size else 0.0


def adjustment(adjust: Optional[dict]) -> dict:
    """A frame's corrections, complete and within range. Missing means none."""
    full = {**NEUTRAL, **(adjust or {})}
    for key, (low, high) in KEYS.items():
        full[key] = int(min(max(int(full[key]), low), high))
    if full["tone"] not in TONES:
        full["tone"] = "standard"
    return {key: full[key] for key in NEUTRAL}


def corrected(adjust: Optional[dict]) -> bool:
    return adjustment(adjust) != NEUTRAL


def edit_factors(bounds: Bounds, base: str, channels: int) -> tuple[np.ndarray, float]:
    """For a base developed with `bounds`: how far one step of log density moves each channel's
    values, and the middle of the base's range (where contrast pivots)."""
    if base == "flat":
        k = GAIN / (np.asarray(bounds.base, np.float32) - np.asarray(bounds.dense, np.float32))
        pivot = (LIFT + WHITE_FLAT) / 2
    else:
        white = WHITE_FULL if base == "converted white" else WHITE_FLAT
        k = white / (np.asarray(bounds.thin or bounds.base, np.float32) - np.asarray(bounds.dense, np.float32))
        pivot = white / 2
    return k[:channels].astype(np.float32), pivot


def edit(values: np.ndarray, k: np.ndarray, pivot: float, adjust: Optional[dict]) -> np.ndarray:
    """Apply the corrections to a flat or converted positive given as 0..1 floats.

    + D makes the picture denser (darker); + C, + M, + Y give more of that colour, which is
    less red, green or blue. Returns float32 0..1; unchanged values without corrections.
    """
    a = adjustment(adjust)
    if not corrected(a):
        return values
    if values.ndim == 3:
        keys = np.array([a["c"], a["m"], a["y"]], np.float32)
        values = values - k * (STEP_DENSITY * a["d"] + STEP_COLOUR * keys)
        if a["sat"]:
            mean = ((values[..., 0] + values[..., 1] + values[..., 2]) * np.float32(1 / 3))[..., None]  # faster than mean(axis=-1)
            values = mean + np.float32(1.0 + SATURATION_STEP * a["sat"]) * (values - mean)
    else:
        values = values - k[0] * np.float32(STEP_DENSITY * a["d"])
    shadows, highlights = TONES[a["tone"]]
    if a["grad"] or a["tone"] != "standard":
        away = values - np.float32(pivot)
        steep = np.float32(GRADATION_STEP ** a["grad"])
        values = np.float32(pivot) + away * steep * np.where(away < 0, np.float32(shadows), np.float32(highlights))
    return np.clip(values, 0.0, 1.0)
