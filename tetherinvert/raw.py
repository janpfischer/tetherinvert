"""RAW decoding for measurement: sensor-native, linear, no white balance, 16-bit.

Each channel is scaled by a fixed reference (the sensor's linear ceiling), never by the
frame's own brightest pixel, so values from different exposures can be compared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

FULL = 65535


def _ceiling(raw) -> int:
    """The sensor's linear ceiling in raw counts. The per-channel table, where a body has one,
    is the maker's own calibration and can sit below the format's nominal white level."""
    nominal = int(raw.white_level or 0)
    table = [int(v) for v in (raw.camera_white_level_per_channel or []) if v]
    measured = min(table) if table else nominal
    return min(measured, nominal) if nominal else measured


class RawError(RuntimeError):
    """A file from the camera could not be decoded. The message says what the file is."""


def sniff(head: bytes) -> str:
    """What a file is, from its first bytes: 'TIFF' (ARW, NEF, DNG, CR2 are TIFF containers),
    'JPEG', 'HEIF', 'CR3', 'RAF', or 'unknown'."""
    if head[:2] == b"\xff\xd8":
        return "JPEG"
    if head[4:8] == b"ftyp":
        return "CR3" if head[8:11] == b"crx" else "HEIF"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "TIFF"
    if head[:8] == b"FUJIFILM":
        return "RAF"
    return "unknown"


def decoder_version() -> str:
    import rawpy

    return f"LibRaw {'.'.join(str(v) for v in rawpy.libraw_version)}, rawpy {rawpy.__version__}"


def explain(path: Path | str, reason: object = "") -> str:
    """Why a file would not decode, in terms of what to change."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        return f"{path.name} could not be read: {exc}"
    kind = sniff(head)
    said = reason.decode("ascii", "replace") if isinstance(reason, bytes) else str(reason)
    what = f"{path.name} ({size / 1e6:.1f} MB)"
    if kind in ("JPEG", "HEIF"):
        return (
            f"{what} holds a {kind} picture, not a RAW. Set the camera's image quality to RAW only, "
            "and in its PC Remote settings let RAW be the file that is sent to the computer."
        )
    if size < 100_000:
        return f"{what} is too small to be a RAW: the download from the camera was cut short. Check the cable and try again."
    if kind == "unknown":
        return f"{what} is not a file type the decoder knows (it starts with {head[:8].hex(' ')}). {decoder_version()} said: {said}"
    return (
        f"{what} looks like a RAW file, but the decoder cannot read it ({decoder_version()} said: {said}). "
        "This is usually the RAW file type: switch it in the camera menu (uncompressed, compressed, lossless L) "
        "and try again, or update the decoder with: pip install -U rawpy"
    )


def decode_linear(path: Path | str, half: bool = False) -> np.ndarray:
    """One RAW as a uint16 H x W x 3 array (R, G, B), linear, full scale = sensor ceiling.

    `half` averages each 2x2 Bayer cell into one pixel: four times faster, for metering only.
    Raises RawError, with an explanation, when the file cannot be decoded.
    """
    import rawpy

    try:
        with rawpy.imread(str(path)) as raw:
            ceiling = _ceiling(raw)
            black = min(raw.black_level_per_channel or [0])
            bayer = raw.raw_pattern is not None and raw.raw_pattern.shape[0] == 2
            rgb = raw.postprocess(
                gamma=(1, 1),
                no_auto_bright=True,
                adjust_maximum_thr=0.0,  # LibRaw would otherwise rescale bright frames by their own maximum
                user_sat=max(1, ceiling - black) if ceiling > 0 else None,
                use_camera_wb=False,
                user_wb=[1, 1, 1, 1],
                output_bps=16,
                output_color=rawpy.ColorSpace.raw,
                half_size=half and bayer,
                user_flip=0,
            )
    except rawpy.LibRawError as exc:
        raise RawError(explain(path, exc.args[0] if exc.args else exc)) from exc
    return np.ascontiguousarray(rgb[..., :3])


def clipped_fraction(path: Path | str, channel: Optional[int], roi) -> float:
    """Share of raw photosites at the sensor ceiling inside `roi` (anything with
    `pixels(width, height)`). `channel` 0/1/2 restricts it to R/G/B sites, None takes all.

    Read from the mosaic itself: demosaicing averages a clipped site with clean neighbours and hides it.
    """
    import rawpy

    with rawpy.imread(str(path)) as raw:
        mosaic = raw.raw_image_visible
        if mosaic.ndim != 2 or raw.raw_pattern is None:
            return 0.0  # not a mosaic (a reduced-size RAW): the demosaiced check has to do
        x0, y0, x1, y1 = roi.pixels(mosaic.shape[1], mosaic.shape[0])
        sites = mosaic[y0:y1, x0:x1]
        if channel is not None:
            letters = raw.color_desc.decode("ascii", "ignore")  # e.g. "RGBG"
            wanted = [i for i, letter in enumerate(letters) if letter == "RGB"[channel]]
            sites = sites[np.isin(raw.raw_colors_visible[y0:y1, x0:x1], wanted)]
        ceiling = _ceiling(raw)
        if sites.size == 0 or ceiling <= 0:
            return 0.0
        return float(np.mean(sites >= ceiling - 16))
