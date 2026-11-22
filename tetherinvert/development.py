"""A frame's development: everything its print is made with, and the frame developed with it.

A development is the frame's base (invert.BASES), its points (its own, or for a matched roll the
roll's shared ones), its corrections and its orientation. The window, the contact sheet and the
print all show a frame by its development, through `picture`, so what is seen is what is printed.

- `of_roll` works out every frame's development together: the shared points are pooled once,
  per kind of frame. The window passes its corrections not saved yet; nothing else is read from
  it. It also says which prints are due: missing, or made otherwise than the development says
  now (`made_with` is what `Frame.printed` stores, so a print stays current across versions).
- `picture` develops the frame from the source its size calls for: the preview (`preview.py`, the
  camera's frame, 1600 px) up to the screen's size, the flat TIFF (written turned) at full size.
  Either way the picture comes out the way the operator has it: turned and mirrored, and with
  the picture area outlined or cut out. One develop step for both sources (`base_positive`,
  then `invert.edit`), so the preview is the print made small.
- Developing the base is the slow part (about 57 ms at the screen's size), the corrections are
  fast (about 20 ms, 40 with saturation). The base of the last few frames shown is kept, so a key
  held on a frame only does the corrections. A preview written again (a render) is noticed by
  its file's time and size.

No Qt here.
"""

from __future__ import annotations

import functools
import threading
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Iterator, Mapping, Optional

import numpy as np
import tifffile

from . import invert, orient, paths, preview

SCREEN = preview.EDGE  # px on the long side: the preview as it is stored, the size kept between pictures
KEPT = 4  # frames whose base is kept: the one being corrected and the ones next to it, not the roll


class Undeveloped(Exception):
    """The frame cannot be developed: not rendered yet, or its source is not on disk. The message
    is a sentence for the operator."""


def _pairs(adjust: Optional[dict]) -> tuple:
    return tuple(invert.adjustment(adjust).items())


@dataclass(frozen=True)
class Development:
    """Everything a frame's print is made with. Frozen and hashable: it crosses to the window's
    preview thread as it is, and an answer made from one is still current when it equals the
    frame's development now."""

    place: paths.Place  # whose frame: where the roll is and the name its files have
    number: int
    base: str  # invert.BASES
    points: Optional[invert.Bounds]  # own, or the roll's shared ones; None until the frame is rendered
    corrections: tuple  # invert.adjustment as (key, value) pairs, in NEUTRAL's order
    turn: int = 0
    mirror: bool = False
    rendered: Optional[float] = None  # when its scan and preview were written

    @property
    def adjust(self) -> dict:
        return dict(self.corrections)

    @property
    def orientation(self) -> dict:
        return {"turn": self.turn, "mirror": self.mirror}

    def corrected(self, adjust: Optional[dict]) -> "Development":
        """The same frame with other corrections; None: none (C held)."""
        return replace(self, corrections=_pairs(adjust))

    @property
    def made_with(self) -> dict:
        """What the print depends on, as `Frame.printed` keeps it: its corrections and base, the
        points (they change when the roll is matched or the frame rendered again) and its flat
        file. Not the orientation: a turned frame's print is turned with it."""
        p = self.points
        used = [[round(v, 5) for v in values] for values in (p.base, p.dense, p.thin or p.base)] if p else None
        return {**self.adjust, "develop": self.base, "points": used, "rendered": self.rendered}


class Developments(Mapping[int, Development]):
    """A roll's developments by frame number, every frame's, in the roll's order; `due`: the frames
    whose print has to be written."""

    def __init__(self, developments: dict[int, Development], due: frozenset[int]) -> None:
        self._developments = developments
        self.due = due

    def __getitem__(self, number: int) -> Development:
        return self._developments[number]

    def __iter__(self) -> Iterator[int]:
        return iter(self._developments)

    def __len__(self) -> int:
        return len(self._developments)


def of_roll(roll, corrections: Optional[Mapping[int, Optional[dict]]] = None) -> Developments:
    """Every frame's development (`roll` is a session.Roll). `corrections` stand in for the saved
    ones of the frames they name (None: none). Reads no pixels.

    A print is due when it is missing or was made with something else. A frame that cannot be
    printed (not rendered yet, or its flat TIFF or preview gone) is not due."""
    corrections = corrections or {}
    place = paths.Place(roll.folder, roll.name)
    own = {f.number: invert.Bounds.from_json(f.bounds) if f.bounds else None for f in roll.frames}
    shared = {}
    if roll.matched:
        for kind in {f.kind for f in roll.frames}:
            noted = [own[f.number] for f in roll.frames if f.kind == kind and own[f.number] is not None]
            shared[kind] = invert.pool(noted) if noted else None
    developments, due = {}, set()
    for frame in roll.frames:
        points = shared[frame.kind] if roll.matched and own[frame.number] is not None else own[frame.number]
        o = orient.normal(frame.orientation)
        adjust = corrections[frame.number] if frame.number in corrections else frame.adjust
        dev = Development(place, frame.number, frame.develop or invert.BASES[0], points, _pairs(adjust), o["turn"], o["mirror"], frame.rendered)
        developments[frame.number] = dev
        printable = points is not None and preview.path(place, frame.number).exists() and paths.tiff(place, frame.number, "flat").exists()
        if printable and (frame.printed != dev.made_with or not paths.tiff(place, frame.number, "print").exists()):
            due.add(frame.number)
    return Developments(developments, frozenset(due))


@dataclass(frozen=True, eq=False)
class Picture:
    """A frame as its print will be."""

    values: np.ndarray  # float32 0..1, (h, w, 3) or (h, w) for black and white; may be read-only (the kept base itself)
    outline: Optional[tuple[float, float, float, float]]  # the picture area on it (x, y, w, h); None when only that is shown

    @functools.cached_property
    def rgb(self) -> np.ndarray:
        """8-bit RGB for the screen; black and white in all three channels."""
        shown = (self.values * np.float32(255) + np.float32(0.5)).astype(np.uint8)
        return np.ascontiguousarray(np.repeat(shown[..., None], 3, axis=2) if shown.ndim == 2 else shown)


@dataclass(frozen=True, eq=False)
class _Base:
    """A frame's base before its corrections, turned as the operator has it, with what one step
    of density does to each channel and where contrast pivots."""

    values: np.ndarray
    k: np.ndarray
    pivot: float


def _base(log_image: np.ndarray, dev: Development) -> _Base:
    positive = invert.base_positive(dev.base, log_image, dev.points).astype(np.float32) * np.float32(1 / invert.FULL)
    k, pivot = invert.edit_factors(dev.points, dev.base, positive.shape[-1] if positive.ndim == 3 else 1)
    values = orient.image(positive, dev.orientation)  # corrections act on each pixel alone, so turning first changes nothing
    values.flags.writeable = False  # invert.edit hands it back as it is without corrections, and it may be kept
    return _Base(values, k, pivot)


_kept: OrderedDict[tuple, tuple[_Base, preview.Preview]] = OrderedDict()
_kept_lock = threading.Lock()


def _from_preview(dev: Development, edge: int, cut: bool) -> tuple[_Base, preview.Preview]:
    file = preview.path(dev.place, dev.number)
    try:
        stat = file.stat()
    except FileNotFoundError:
        raise Undeveloped(f"frame {paths.label(dev.number)} has no preview ({file.name}); render it again first") from None
    keep = edge == SCREEN and not cut
    key = (str(dev.place.folder), dev.place.name, dev.number, dev.base, dev.points, dev.turn, dev.mirror, stat.st_mtime_ns, stat.st_size)
    if keep:
        with _kept_lock:
            if key in _kept:
                _kept.move_to_end(key)
                return _kept[key]
    found = preview.load(dev.place, dev.number)
    if found is None:
        raise OSError(f"the preview of frame {paths.label(dev.number)} could not be read")  # being written: asked for again when it is done
    image = found.image
    if cut and not found.cropped:
        image = preview.crop(image, found.rect)
    made = _base(invert.to_log(preview.reduce(image, edge)), dev), found
    if keep:
        with _kept_lock:
            _kept[key] = made
            while len(_kept) > KEPT:
                _kept.popitem(last=False)
    return made


def _from_flat(dev: Development, cut: bool) -> tuple[_Base, preview.Preview]:
    """From the flat TIFF, at full size. It is written turned, and with the points its preview
    keeps (the same render wrote both), which give its log density back."""
    file = paths.tiff(dev.place, dev.number, "flat")
    if not file.exists():
        raise Undeveloped(f"frame {paths.label(dev.number)} has no flat TIFF ({file.name}) to print from; render it again first")
    found = preview.load(dev.place, dev.number)
    if found is None:
        raise Undeveloped(f"frame {paths.label(dev.number)} has no preview ({preview.path(dev.place, dev.number).name}); render it again first")
    log_image = invert.from_flat(tifffile.imread(file), found.bounds)
    if cut and not found.cropped and found.rect:
        log_image = preview.crop(orient.image_back(log_image, dev.orientation), found.rect)
        log_image = orient.image(log_image, dev.orientation)
    turned = replace(dev, turn=0, mirror=False)  # the file is turned already
    return _base(log_image, turned), found


def picture(dev: Development, edge: Optional[int] = SCREEN, *, cut: bool = False) -> Picture:
    """The frame as its print will be, the way the operator has it. Up to `SCREEN` px on its long
    side it is developed from the preview; with `edge` None at full size from the flat TIFF (about
    2 s for 24 MP), as the print is written. `cut`: only the picture area, where the source holds
    the whole frame; otherwise `outline` says where it is on the picture.

    Raises Undeveloped for a frame not rendered yet or without its source, OSError for a file
    being replaced at that moment. Safe on any thread."""
    if dev.points is None:
        raise Undeveloped(f"frame {paths.label(dev.number)} is not rendered yet")
    if edge is not None and edge > SCREEN:
        raise ValueError(f"a picture larger than {SCREEN} px is made at full size (edge=None), from the flat TIFF")
    base, found = _from_flat(dev, cut) if edge is None else _from_preview(dev, edge, cut)
    values = invert.edit(base.values, base.k, base.pivot, dev.adjust)
    outline = None if cut or found.cropped or found.rect is None else orient.rect(found.rect, dev.orientation)
    return Picture(values, outline)
