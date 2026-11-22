"""16-bit TIFF output with an embedded ICC profile.

An untagged file is read as sRGB by most programs, Lightroom included. That is wrong for the
linear negative (it would show far too dark), so every file carries a profile that states its
tone curve: gamma 1.0 for the linear negative, gamma 2.2 for the flat positive.

The profiles are minimal ICC v2 matrix profiles built here, so no profile files ship with the
program. The primaries are a declaration, not a measurement: narrowband channels have no
standard colour space. "srgb" gives moderate colour, "prophoto" keeps the three channels
unmixed inside Lightroom, whose working space uses the same primaries.

Each file also carries its metadata as an XMP packet (tag 700, see metadata.py). Changing the
metadata later replaces only that tag, in a copy that is then renamed over the file: the
pixels are not decoded or compressed again, and a .tif is never half-written.
"""

from __future__ import annotations

import os
import shutil
import struct
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import tifffile

from . import __version__

XMP = 700  # the TIFF tag of the XMP packet
D50 = (0.9642, 1.0, 0.8249)
# Red, green and blue colorants as XYZ, adapted to the D50 white every ICC profile uses.
PRIMARIES = {
    "srgb": ((0.4361, 0.2225, 0.0139), (0.3851, 0.7169, 0.0971), (0.1431, 0.0606, 0.7141)),
    "prophoto": ((0.7977, 0.2880, 0.0), (0.1352, 0.7119, 0.0), (0.0313, 0.0001, 0.8249)),
}


def _fixed(value: float) -> bytes:
    return struct.pack(">i", int(round(value * 65536)))


def _xyz(values) -> bytes:
    return b"XYZ \0\0\0\0" + b"".join(_fixed(v) for v in values)


def _curve(gamma: float) -> bytes:
    if gamma == 1.0:
        return b"curv\0\0\0\0" + struct.pack(">I", 0)  # no entries = identity
    return b"curv\0\0\0\0" + struct.pack(">IH", 1, int(round(gamma * 256))) + b"\0\0"


def _description(text: str) -> bytes:
    ascii_text = text.encode("ascii", "replace") + b"\0"
    return b"desc\0\0\0\0" + struct.pack(">I", len(ascii_text)) + ascii_text + b"\0" * (4 + 4 + 2 + 1 + 67)


def _profile(colour_space: bytes, tags: list[tuple[bytes, bytes]]) -> bytes:
    """Assemble header, tag table and tag data. Identical data is stored once and shared."""
    table_size = 4 + 12 * len(tags)
    offsets: dict[bytes, int] = {}
    body = b""
    entries = b""
    for signature, data in tags:
        if data not in offsets:
            offsets[data] = 128 + table_size + len(body)
            body += data + b"\0" * (-len(data) % 4)
        entries += signature + struct.pack(">II", offsets[data], len(data))
    now = time.gmtime()
    header = struct.pack(">I4sI4s4s4s", 128 + table_size + len(body), b"\0\0\0\0", 0x02100000, b"mntr", colour_space, b"XYZ ")
    header += struct.pack(">6H", now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec)
    header += b"acsp" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8 + b"\0" * 4
    header += b"".join(_fixed(v) for v in D50) + b"\0" * 4
    header += b"\0" * (128 - len(header))
    return header + struct.pack(">I", len(tags)) + entries + body


def rgb_profile(gamma: float, primaries: str = "srgb") -> bytes:
    red, green, blue = PRIMARIES[primaries]
    curve = _curve(gamma)
    return _profile(
        b"RGB ",
        [
            (b"desc", _description(f"TetherInvert {primaries} gamma {gamma:g}")),
            (b"cprt", b"text\0\0\0\0" + b"No copyright, use freely\0"),
            (b"wtpt", _xyz(D50)),
            (b"rXYZ", _xyz(red)),
            (b"gXYZ", _xyz(green)),
            (b"bXYZ", _xyz(blue)),
            (b"rTRC", curve),
            (b"gTRC", curve),
            (b"bTRC", curve),
        ],
    )


def gray_profile(gamma: float) -> bytes:
    return _profile(
        b"GRAY",
        [
            (b"desc", _description(f"TetherInvert gray gamma {gamma:g}")),
            (b"cprt", b"text\0\0\0\0" + b"No copyright, use freely\0"),
            (b"wtpt", _xyz(D50)),
            (b"kTRC", _curve(gamma)),
        ],
    )


def write_tiff(
    path: Path, image: np.ndarray, gamma: float, description: str, primaries: str = "srgb", when: Optional[float] = None, xmp: Optional[str] = None
) -> None:
    """Write uint16 RGB (H x W x 3) or grayscale (H x W), compressed without loss, with `xmp` as
    its XMP packet."""
    if image.dtype != np.uint16:
        raise ValueError("write_tiff expects uint16")
    profile = gray_profile(gamma) if image.ndim == 2 else rgb_profile(gamma, primaries)
    _write(Path(path), image, profile, description, time.strftime("%Y:%m:%d %H:%M:%S", time.localtime(when)), xmp.encode() if xmp else None)


def read_xmp(path: Path) -> Optional[str]:
    """A TIFF's XMP packet, or None."""
    with tifffile.TiffFile(path) as tif:
        tag = tif.pages[0].tags.get(XMP)
        return bytes(tag.value).decode("utf-8") if tag is not None else None


def rewrite_tiff(path: Path, change: Callable[[np.ndarray], np.ndarray], xmp: Optional[str] = None) -> None:
    """A TIFF written here, with its pixels changed (turned): its description, colour profile,
    date and metadata (unless `xmp` replaces it) stay exactly as they were, so it is the same
    file the other way up."""
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        pixels, description = page.asarray(), page.description
        profile = page.tags["InterColorProfile"].value
        stamp = page.tags["DateTime"].value if "DateTime" in page.tags else time.strftime("%Y:%m:%d %H:%M:%S")
        packet = xmp.encode() if xmp is not None else bytes(page.tags[XMP].value) if XMP in page.tags else None
    _write(Path(path), np.ascontiguousarray(change(pixels)), profile, description, stamp, packet)


def set_xmp(path: Path, xmp: str) -> None:
    """Replace a TIFF's XMP packet; the pixels are left as they are. The tag is overwritten in a
    copy (tifffile appends the value when it is longer than the old one) and the copy is renamed
    over the file. A file without the tag is written once more, pixels unchanged."""
    path = Path(path)
    partial = path.with_name(path.name + ".part")
    shutil.copyfile(path, partial)
    try:
        with tifffile.TiffFile(partial, mode="r+") as tif:
            tag = tif.pages[0].tags.get(XMP)
            if tag is not None:
                tag.overwrite(xmp.encode())
        if tag is None:
            partial.unlink()
            rewrite_tiff(path, lambda pixels: pixels, xmp)
            return
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def _write(path: Path, image: np.ndarray, profile: bytes, description: str, stamp: str, xmp: Optional[bytes] = None) -> None:
    gray = image.ndim == 2
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    tifffile.imwrite(
        partial,
        image,
        photometric="minisblack" if gray else "rgb",
        compression="zlib",
        compressionargs={"level": 1},  # the noise in 16-bit scans barely compresses; higher levels only cost time
        predictor=True,
        rowsperstrip=256,
        maxworkers=os.cpu_count(),  # strips are compressed in parallel when imagecodecs is installed
        iccprofile=profile,
        description=description.encode("ascii", "replace").decode(),
        software=f"TetherInvert {__version__}",
        datetime=stamp,
        metadata=None,
        extratags=[(XMP, 1, len(xmp), xmp, True)] if xmp else [],  # BYTE, as the XMP specification has it for TIFF
    )
    partial.replace(path)
