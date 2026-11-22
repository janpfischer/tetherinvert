"""Presets, rolls, and the two operations on them: scan a frame, render a frame.

On disk a roll is a folder:

    MyRoll/
        roll.json              preset used, picture rectangle, metadata, frames and their measured points
        raw/MyRoll_001_R.ARW   the exposures, kept so every frame can be rendered again
        scans/MyRoll_001.tif   flat positive, the master
        negative/MyRoll_001.tif  linear negative (optional)
        prints/MyRoll_001.tif  print: the flat positive developed with the frame's base and corrections
        preview/MyRoll_001.npz small linear copy and its points, for the window (see preview.py)
        MyRoll_contact.pdf     the contact sheet (contact.py)

Presets live in one file, ~/.tetherinvert/presets.json (override the folder with TETHERINVERT_HOME).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import numpy as np

from . import development, invert, metadata, orient, paths, preview
from .calibrate import THIRDS, TARGET, TOLERANCE, Calibration, Calibrator, Roi, Step, WrongColour, channels, true_seconds, usable_ladder
from .flatfield import FlatField
from .kinds import EXPOSURES, KINDS
from .merge import merge_triplet
from .paths import home
from .raw import FULL, clipped_fraction, decode_linear
from .tiffio import rewrite_tiff, set_xmp, write_tiff

SETTLE_S = 0.4  # after switching the LED, before the shutter
MIN_RAW_BYTES = 1_000_000
# Share of a colour's photosites in the picture area at the sensor ceiling above which a scan is
# reported as clipped. Calibration leaves the base about 0.15 stop of headroom, so a third of a stop
# more light (an f-stop ring moved, a thinner film strip) can clip the base in green over half the
# picture area, and the prints come out dark. A well-exposed frame stays near 0.003 %.
CLIP_WARN = 0.001
FRAMING_DIM = 8  # live view lights all three LEDs at once, so each runs this much dimmer
# A roll without a flat field does not know where the light is brightest, so every LED is lowered
# by this much (a quarter stop). On the tested rig the film base is a quarter stop brighter in the
# middle of the frame than in the film base rectangle at its left edge, and calibration leaves only
# about 0.15 stop of headroom, so every frame would clip in green. Exposing the film base less far
# to the right is the better loss.
NO_FLAT_MARGIN = 2 ** -0.25
BARE_LIGHT_GUESS = 12  # the uncovered light is roughly this much brighter than through a film base
BARE_FILE = "bare_light.json"  # per preset, the bare-light exposure that worked: next session starts there
PICTURES = "presets"  # beside presets.json: per preset, the live view around its film base rectangle at calibration (the window's Presets page)
MISSING = {  # a device that could not be opened, or a job refused without it: what the operator does about it
    "camera": "The camera is not connected.",
    "light": "The Scanlight is not connected. Plug in its data port (the left USB-C).",
}
NEEDS_SCANLIGHT = {  # a Scanlight preset with no Scanlight to drive: none found, or TetherInvert told to leave it alone
    "none": "This preset needs the Scanlight, and none is connected. Plug in its data port (the left USB-C).",
    "switched off": "This preset needs the Scanlight, and TetherInvert was started with --no-light (or TETHERINVERT_NO_LIGHT=1): start it without.",
}
NO_LIGHT_LABEL = "none: own light"


def tidy_aperture(text: str) -> str:
    """'8', 'f8', 'F/5.6' or '5,6' -> 'f/8', 'f/5.6'. Anything without a number is kept as typed."""
    found = re.search(r"[0-9]+(?:[.,][0-9]+)?", text)
    return f"f/{float(found.group().replace(',', '.')):g}" if found else text.strip()


# ----- presets -----


@dataclass(frozen=True)
class Preset:
    """Everything needed to expose one film stock the same way every time."""

    name: str
    kind: str  # kinds.KINDS: "rgb" three exposures, "rgb-together" one with all three lit, "white" one under the white LED, own-… for an own light
    levels: tuple[int, int, int, int]  # r, g, b, w; for rgb-together the light's colour default, read at calibration; 0 for an own light
    shutter: str  # own-rgb: green's, which the live view is left on
    roi: tuple[float, float, float, float]  # where on the frame the base was metered
    iso: str = ""
    aperture: str = ""  # empty for a lens without electronic aperture
    base: tuple[float, float, float] = (0.0, 0.0, 0.0)  # film base signal per channel, 0..65535
    camera: str = ""
    created: str = ""
    by_hand: tuple[str, ...] = ()  # "iso", "aperture", "light": what TetherInvert cannot set, so the operator must
    light_note: str = ""  # an own light: what it is and how it is set, as the operator noted it ("Kaiser slimlite plano, full")
    shutters: Optional[dict] = None  # own-rgb: a shutter per colour, {"R": "1/8", "G": "1/4", "B": "1/2"}; None = `shutter` for every exposure

    def shutter_for(self, letter: str) -> str:
        """The shutter an exposure is shot at, before a session lowers anything."""
        return (self.shutters or {}).get(letter, self.shutter)

    def settings(self) -> list[tuple[str, str, bool]]:
        """(name, value, set by hand) for everything a scan with this preset depends on.

        A setting the program cannot apply is the operator's to check before scanning. That is
        typically the aperture of a lens without electronic contacts.
        """
        kind = KINDS[self.kind]
        if kind.scanlight:
            light, by_hand = kind.leds(self.levels) + (" (the light's default)" if kind.from_light else ""), False
        else:  # the operator's to set, like the f-stop
            light, by_hand = self.light_note or "not recorded", True
        shutter = " · ".join(f"{letter} {self.shutters[letter]}" for letter in kind.exposures) if self.shutters else self.shutter
        return [
            ("ISO", self.iso or "not recorded", "iso" in self.by_hand or not self.iso),
            ("Shutter", f"{shutter} s", False),
            ("Aperture", self.aperture or "not recorded", "aperture" in self.by_hand or not self.aperture),
            ("Light", light, by_hand),
        ]

    def framing_light(self) -> Optional[tuple[int, int, int, int]]:
        """The Scanlight dimmed for the live view; None for an own light: nothing is sent then."""
        if not KINDS[self.kind].scanlight:
            return None
        r, g, b, w = (max(1, level // FRAMING_DIM) if level else 0 for level in self.levels)
        return r, g, b, w


def preset_from(entry: dict) -> Preset:
    """A preset as stored in presets.json or in a roll."""
    tuples = {key: tuple(entry[key]) for key in ("levels", "base", "roi", "by_hand")}
    return Preset(**{**entry, **tuples})


class PresetStore:
    """The film presets, by name. Names are kept as typed and compared without case, so no two
    presets differ only in case: `Gold200` replaces `gold200`, and `--preset GOLD200` finds it."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else home() / "presets.json"

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".part")
        partial.write_text(json.dumps(data, indent=2))
        partial.replace(self.path)

    def names(self) -> list[str]:
        return sorted(self._read(), key=str.casefold)

    def find(self, name: str) -> Optional[str]:
        """The stored spelling of a preset's name, compared without case; None if there is none."""
        return next((stored for stored in self._read() if stored.casefold() == name.strip().casefold()), None)

    def get(self, name: str) -> Preset:
        data, stored = self._read(), self.find(name)
        if stored is None:
            raise KeyError(f"no preset named {name!r}; known: {', '.join(self.names()) or 'none'}")
        return preset_from(data[stored])

    def newest(self) -> Optional[Preset]:
        """The preset calibrated last: the calibration wizard starts its by-hand settings from it."""
        data = self._read()
        return preset_from(max(data.values(), key=lambda entry: entry.get("created", ""))) if data else None

    def save(self, preset: Preset) -> None:
        """Store a preset, replacing one whose name differs at most in case; the new spelling stays. The
        picture of the one replaced goes: it shows where the old calibration metered."""
        data = {name: entry for name, entry in self._read().items() if name.casefold() != preset.name.casefold()}
        data[preset.name] = asdict(preset)
        self._write(data)
        self.picture(preset.name).unlink(missing_ok=True)

    def delete(self, name: str) -> None:
        data, stored = self._read(), self.find(name)
        if stored is not None:
            del data[stored]
            self._write(data)
            self.picture(stored).unlink(missing_ok=True)

    def picture(self, name: str) -> Path:
        """Where a preset's film base picture is kept: the live view around its rectangle, which the
        window saves when it calibrates. A preset calibrated on the command line has none. Named
        without case, as presets are compared, and quoted, so any name is a file name."""
        return self.path.parent / PICTURES / f"{quote(name.strip().casefold(), safe='')}.jpg"


# ----- rolls -----

_ROLL_LOCK = threading.Lock()  # one change at a time in this process; LOCK_FILE does it between processes
LOCK_FILE = ".roll.lock"  # in the roll folder, hidden; Lightroom watches only scans/ and prints/, so never sees it
ROLL_SETTINGS = ("rect", "crop", "flatfield", "compare", "matched", "per_channel", "primaries")  # what set_settings takes
SHEET_DEFAULTS = {"rebate": True, "sprockets": False}  # the contact sheet's options for a roll that set none: film strips, no holes


@dataclass
class Frame:
    number: int
    files: list[str]  # relative to the roll folder, one per exposure of its kind: [R, G, B], [RGB] or [W]
    preset: str
    kind: str
    base: list[float]  # copied from the preset, so a roll renders without the preset store
    roi: list[float]  # where the preset's base was metered; anchors the flat field
    bounds: Optional[dict] = None  # this frame's own measured points
    adjust: Optional[dict] = None  # the operator's corrections for the print (invert.NEUTRAL keys); None = none
    rendered: Optional[float] = None  # when its TIFFs were last written: a print made before is out of date
    printed: Optional[dict] = None  # what the print on disk was made with: corrections and `rendered`
    orientation: Optional[dict] = None  # how its files are turned and mirrored (orient.py), as set when it was scanned; None = upright
    develop: Optional[str] = None  # the base its print is developed from (invert.BASES); None = flat
    metadata: Optional[dict] = None  # the operator's: aperture, shutter, date, note, keywords, keywords_left_out (metadata.py)
    light: Optional[str] = None  # the light it was scanned under (its describe()), for the files' TetherInvert Scan
    scanned: Optional[float] = None  # when it was scanned


@dataclass
class Roll:
    folder: Path
    name: str = ""  # every file is named after it (paths.stem); given when roll.json is first written: the folder's name then, kept when the folder is moved or renamed
    rect: Optional[list[float]] = None  # picture area x, y, w, h; None = whole frame
    per_channel: bool = True
    primaries: str = "srgb"
    crop: bool = False  # write only the picture area
    flatfield: bool = True  # apply the roll's flat field, when it has one
    compare: bool = False  # also write every file without the flat field, to compare
    matched: bool = False  # the prints developed with points shared across the roll, so its colour and density match
    preset: Optional[dict] = None  # the preset the roll is scanned with, as it was when it was started: continuing uses this copy
    next: Optional[int] = None  # the number the next scan gets, when set (the first-frame question, the counter); None = after the last frame
    metadata: dict = field(default_factory=dict)  # the operator's roll fields (metadata.ROLL_FIELDS), written into every TIFF
    sheet: dict = field(default_factory=dict)  # the contact sheet's options as set: "rebate", "sprockets" (sheet_options adds the defaults)
    frames: list[Frame] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name or Path(self.folder).name  # a new roll takes its folder's name

    @staticmethod
    def open(folder: Path) -> "Roll":
        """The roll as it is on disk now; a folder without roll.json gives an empty one. Reading
        takes no lock: roll.json is only ever replaced by a rename, so this sees the old file or
        the new one, never half of one."""
        try:
            return Roll._parse(Path(folder), (Path(folder) / "roll.json").read_text())
        except (OSError, ValueError):
            return Roll(Path(folder))

    @staticmethod
    def _parse(folder: Path, text: str) -> "Roll":
        data = json.loads(text)
        return Roll(folder, **{**data, "frames": [Frame(**frame) for frame in data["frames"]]})

    # ----- the one way roll.json is written -----
    #
    # The scan thread, the render thread, the window and the command line all change rolls. Every
    # change is made to the roll as it is on disk at that moment, touches only what it was asked to
    # change, and leaves the caller's object as what was written: a Match the roll click made while
    # a frame is exposed survives the scan adding the frame.

    def _change(self, change: Callable[["Roll"], None]) -> None:
        """Read roll.json, apply `change` to that, write it, and make this object what was written,
        every field. Under the thread lock and a file lock on .roll.lock in the folder, which the
        window and the command line both take, so neither loses the other's change."""
        self.folder.mkdir(parents=True, exist_ok=True)
        with _ROLL_LOCK, open(self.folder / LOCK_FILE, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file is closed
            fresh = Roll.open(self.folder)
            change(fresh)
            text = json.dumps({key: value for key, value in asdict(fresh).items() if key != "folder"}, indent=2)  # the folder is where it is
            partial = self.folder / "roll.json.part"
            partial.write_text(text)
            partial.replace(self.folder / "roll.json")
        vars(self).update(vars(Roll._parse(self.folder, text)))  # as written: tuples are lists now, as they are for every reader

    @staticmethod
    def start(folder: Path, preset: Preset, flatfield: bool) -> "Roll":
        """A session starts or continues: from now on the folder is this roll, even before its first
        frame. A roll keeps the preset copy it has (it is scanned with that); the flat-field choice
        is this session's."""

        def change(roll: Roll) -> None:
            roll.preset = roll.preset or asdict(preset)
            roll.flatfield = flatfield

        roll = Roll(Path(folder))
        roll._change(change)
        return roll

    def set_settings(self, **settings) -> None:
        """Some of the roll settings (ROLL_SETTINGS); frames, counter, metadata, the sheet's options
        and the preset copy have their own changes. Stored at once, so no setting lives only on an
        object in memory, to be lost or written back later over a newer one."""
        unknown = sorted(set(settings) - set(ROLL_SETTINGS))
        if unknown:
            raise TypeError(f"not a roll setting: {', '.join(unknown)} (they are {', '.join(ROLL_SETTINGS)})")
        self._change(lambda roll: vars(roll).update(settings))

    def add_frame(self, frame: Frame, preset: Preset) -> None:
        """A scanned frame, replacing one with its number. A roll that has no preset copy yet (one
        scanned from the command line) gets `preset`'s in the same change."""

        def change(roll: Roll) -> None:
            roll.preset = roll.preset or asdict(preset)
            for old in roll.frames:
                if old.number == frame.number:  # a retake or rescan is the same picture again: its corrections, base and metadata stay
                    frame.adjust = old.adjust if frame.adjust is None else frame.adjust
                    frame.develop = old.develop if frame.develop is None else frame.develop
                    frame.metadata = old.metadata if frame.metadata is None else frame.metadata
            roll.frames = sorted([f for f in roll.frames if f.number != frame.number] + [frame], key=lambda f: f.number)

        self._change(change)

    def note_bounds(self, frame: Frame) -> None:
        """After a render: the frame's points, and what its print was written with. Only onto the
        frame as it was scanned: a rescan meanwhile has the same number and file names, and a render
        of the old exposures finishing late must not write its points onto the new frame."""

        def change(roll: Roll) -> None:
            for other in roll.frames:
                if other.number == frame.number and other.scanned == frame.scanned:
                    other.bounds, other.rendered, other.printed = frame.bounds, frame.rendered, frame.printed

        self._change(change)

    def update_frame(self, number: int, **fields) -> None:
        """Some fields of one frame (its corrections, its orientation); nothing else is touched."""

        def change(roll: Roll) -> None:
            for other in roll.frames:
                if other.number == number:
                    for name, value in fields.items():
                        setattr(other, name, value)

        self._change(change)

    def remove_frame(self, number: int) -> None:
        """The frame's entry, out of roll.json; its files are delete_frame's business. Deleting
        the last frame puts the counter back on its number, so the next scan takes it again."""

        def change(roll: Roll) -> None:
            if number == roll.last_number():
                roll.next = None
            roll.frames = [f for f in roll.frames if f.number != number]

        self._change(change)

    def set_metadata(self, fields: dict) -> None:
        """The roll's metadata fields; nothing else is touched. The files are retag's business."""
        self._change(lambda roll: setattr(roll, "metadata", dict(fields)))

    def sheet_options(self) -> dict:
        """The contact sheet's options, those not set taking their defaults (SHEET_DEFAULTS)."""
        return {**SHEET_DEFAULTS, **self.sheet}

    def set_sheet(self, options: dict) -> None:
        """The contact sheet's options (`rebate`, `sprockets`); nothing else is touched."""
        self._change(lambda roll: setattr(roll, "sheet", {**roll.sheet, **options}))

    def set_next(self, number: Optional[int]) -> None:
        """The number the next scan gets (the counter); None: the one after the last frame."""
        self._change(lambda roll: setattr(roll, "next", number))

    def free_number(self, number: int, step: int) -> Optional[int]:
        """The window's counter, one step: see `free_number`."""
        return free_number({f.number for f in self.frames}, number, step)

    def next_number(self) -> int:
        """What the next scan is numbered: the counter when set, else the last frame's + 1, and 1
        on a roll that has none (the first-frame question was skipped)."""
        if self.next is not None:
            return self.next
        return self.last_number() + 1 if self.frames else 1

    def frame(self, number: int) -> Frame:
        for frame in self.frames:
            if frame.number == number:
                return frame
        raise KeyError(f"roll {self.name} has no frame {paths.label(number)}")

    def last_number(self) -> int:
        return max((frame.number for frame in self.frames), default=0)

    def scanned_with(self) -> Optional[Preset]:
        """The roll's own preset, the copy it was started with; None for a roll not started yet."""
        return preset_from(self.preset) if self.preset else None

    @staticmethod
    def state(folder: Optional[Path]) -> str:
        """What a session on this folder would be: 'new' (the name is free), 'existing' (a roll:
        continue it), 'taken' (a folder that is not a roll) or 'none' (no folder given)."""
        if folder is None:
            return "none"
        folder = Path(folder)
        if not folder.exists():
            return "new"
        return "existing" if (folder / "roll.json").exists() else "taken"


def free_number(taken: set[int], number: int, step: int) -> Optional[int]:
    """The nearest number from `number + step` on, in that direction, that no frame has: where the
    counter goes on − or +. It never lands on a taken number, so it cannot turn a scan into a
    replacement; replacing a frame is only ever asked for by name (Rescan frame N). None when
    nothing is free down to 00; upwards there always is."""
    candidate = number + step
    while candidate >= paths.FIRST:
        if candidate not in taken:
            return candidate
        candidate += step
    return None


# ----- scanning -----


class ScanError(RuntimeError):
    pass


class NoLight:
    """The light slot without a Scanlight: the operator's own light lights the film, and
    TetherInvert controls only the camera. The same surface as Scanlight; setting it does nothing."""

    color = (0, 0, 0, 0)

    def set(self, r: int = 0, g: int = 0, b: int = 0, w: int = 0) -> None:
        pass

    def off(self) -> None:
        pass

    def info(self, timeout: float = 2.0) -> tuple[int, int]:
        raise RuntimeError(NEEDS_SCANLIGHT["none"])

    def default_rgb(self, timeout: float = 2.0) -> tuple[int, int, int]:
        raise RuntimeError(NEEDS_SCANLIGHT["none"])

    def describe(self) -> str:
        return NO_LIGHT_LABEL

    def has_white(self) -> bool:
        return False

    def temperature(self) -> Optional[float]:
        return None

    def close(self) -> None:
        pass


def no_light() -> bool:
    """`--no-light` (TETHERINVERT_NO_LIGHT=1): TetherInvert never opens the Scanlight's port, so another
    program (`tetherinvert light`, the Scanlight web app) can drive it as an own light."""
    return os.environ.get("TETHERINVERT_NO_LIGHT") == "1"


def bench(sim: bool):
    """The simulated rig (`sim.Bench`), or None on the real one. Whoever opens devices makes one
    and passes it to both `open_light` and `open_camera`: the simulated camera has to know what
    lights the film, which is the one thing that ties the two together. With --no-light the
    bench's own lamp lights it."""
    if sim or os.environ.get("TETHERINVERT_SIM") == "1":
        from .sim import Bench

        return Bench(own=no_light())
    return None


def open_light(rig=None, drive: bool = False):
    """The Scanlight, or the bench's light; NoLight under --no-light, unless `drive` (the one
    command meant to drive the light, `tetherinvert light`). Opened, kept and lost apart from the
    camera: a light that drops out must not cost the camera, which on macOS can take minutes to
    get back."""
    if no_light() and not drive:
        return NoLight()
    if rig is not None:
        return rig.light
    from .scanlight import Scanlight

    return Scanlight(os.environ.get("TETHERINVERT_PORT") or None)


def open_camera(rig=None):
    """The camera, opened, or a simulated one looking at the bench's light."""
    if rig is not None:
        return rig.camera()
    from .camera import GphotoCamera

    camera = GphotoCamera()
    camera.open()
    return camera


def open_device(rig, which: str, drive: bool = False):
    """`open_light` or `open_camera`; a failure says which device is missing and what to do."""
    try:
        return open_light(rig, drive) if which == "light" else open_camera(rig)
    except Exception as exc:
        raise RuntimeError(f"{MISSING[which]} ({exc})") from exc


@dataclass(frozen=True)
class ColourShot:
    """One colour of an own-rgb calibration (Scanner.calibrate_colour)."""

    letter: str
    shutter: str
    base: float  # the film base in this colour's own channel, 0..65535


class Scanner:
    """Drives the light and the camera. Both are passed in, so the simulator fits as well."""

    def __init__(self, light, camera, log: Callable[[str], None] = lambda message: None, settle_s: float = SETTLE_S) -> None:
        self.light, self.camera = light, camera
        self._log, self._settle_s = log, settle_s

    def unknown_settings(self) -> list[str]:
        """Of ISO and aperture, those the camera does not report, so they must be asked for."""
        return [key for key in ("iso", "aperture") if not self.camera.get(key)]

    def lamp(self, kind: str):
        """What a job of this kind drives: the Scanlight, or for an own light nothing at all, even
        with a Scanlight connected (not even the framing light). A Scanlight kind without one is
        refused (NEEDS_SCANLIGHT)."""
        if not KINDS[kind].scanlight:
            return NoLight()
        if isinstance(self.light, NoLight):
            raise ScanError(NEEDS_SCANLIGHT["switched off" if no_light() else "none"])
        return self.light

    def calibrate(
        self,
        name: str,
        roi: Roi,
        kind: str = "rgb",
        cancelled: Callable[[], bool] = lambda: False,
        by_hand: Optional[dict[str, str]] = None,
        progress: Callable[[Step], None] = lambda step: None,
        light_note: str = "",
    ) -> Preset:
        """Meter the film base inside `roi` and return the preset (not yet saved).

        `by_hand` gives the settings the camera cannot report (see unknown_settings), as the
        operator stated them, e.g. {"aperture": "f/8"}. They are stored with the preset. So is
        `light_note`, an own light's description and setting.
        `progress` gets a Step after every shot and once the shutter is solved (calibrate.py).
        own-rgb is calibrated colour by colour instead (`calibrate_colour`, then `assemble`).
        """
        if KINDS[kind].prompted:
            raise ScanError("this preset is calibrated colour by colour, each shot prompted: calibrate_colour, then assemble")
        stated = self._stated(by_hand)
        with tempfile.TemporaryDirectory(prefix="tetherinvert-") as scratch:
            calibrator = Calibrator(self.lamp(kind), self.camera, Path(scratch), self._settle_s, self._log, cancelled, progress)
            calibrator.stated = stated
            result: Calibration = calibrator.run(roi, kind)
        return self._preset(name, kind, roi, stated, result.levels, result.shutter, result.base, light_note)

    def calibrate_colour(
        self,
        roi: Roi,
        letter: str,
        kind: str = "own-rgb",
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[Step], None] = lambda step: None,
    ) -> "ColourShot":
        """One colour of an own-rgb preset, the operator's light set to it alone at full (or the
        setting noted): its shutter and its film base. Each is its own job, prompted; a shot of
        another colour is refused (WrongColour). `assemble` makes the preset of the three."""
        self._stated(None)  # off Auto ISO, before the first shot
        with tempfile.TemporaryDirectory(prefix="tetherinvert-") as scratch:
            calibrator = Calibrator(self.lamp(kind), self.camera, Path(scratch), self._settle_s, self._log, cancelled, progress)
            shutter, reading = calibrator.calibrate_colour(roi, kind, letter)
        return ColourShot(letter, shutter, reading.base[EXPOSURES[letter].sensors[0]])

    def assemble(
        self, name: str, roi: Roi, kind: str, shots: dict, by_hand: Optional[dict[str, str]] = None, light_note: str = ""
    ) -> Preset:
        """The preset of a kind calibrated colour by colour (own-rgb), from its ColourShots: a
        shutter per colour (green's also as `shutter`, which the live view is left on), the film
        base per channel from its own colour's shot."""
        letters = KINDS[kind].exposures
        missing = [letter for letter in letters if letter not in shots]
        if missing:
            raise ScanError(f"not every colour has been shot yet: {', '.join(missing)}")
        shot = {letter: shots[letter] if isinstance(shots[letter], ColourShot) else ColourShot(**shots[letter]) for letter in letters}
        base = tuple(shot[letter].base for letter in letters)
        shutters = {letter: shot[letter].shutter for letter in letters}
        return self._preset(name, kind, roi, self._stated(by_hand), (0, 0, 0, 0), shutters["G"], (base[0], base[1], base[2]), light_note, shutters)

    def _stated(self, by_hand: Optional[dict[str, str]]) -> dict[str, str]:
        """The by-hand settings as stated, tidied; and the camera taken off Auto ISO."""
        stated = {key: value.strip() for key, value in (by_hand or {}).items()}
        if stated.get("aperture"):
            stated["aperture"] = tidy_aperture(stated["aperture"])
        iso = self.camera.get("iso") or ""
        fixed = [choice for choice in self.camera.choices("iso") if choice.isdigit()]
        if not iso.isdigit() and fixed:  # Auto ISO would change the exposure behind our back
            lowest = min(fixed, key=int)
            if self.camera.set("iso", lowest):
                self._log(f"ISO was on {iso or 'auto'}; set to {lowest}")
        return stated

    def _preset(self, name, kind, roi, stated, levels, shutter, base, light_note, shutters=None) -> Preset:
        own = not KINDS[kind].scanlight
        return Preset(
            name=name,
            kind=kind,
            levels=levels,
            shutter=shutter,
            iso=self.camera.get("iso") or stated.get("iso", ""),
            aperture=self.camera.get("aperture") or stated.get("aperture", ""),
            by_hand=tuple(self.unknown_settings()) + (("light",) if own else ()),
            base=base,
            camera=getattr(self.camera, "model", ""),
            created=time.strftime("%Y-%m-%d %H:%M"),
            roi=(roi.x, roi.y, roi.w, roi.h),
            light_note=light_note.strip() if own else "",
            shutters=shutters,
        )

    def _apply(self, preset: Preset) -> None:
        """Put the camera on the preset's ISO and aperture, where it can be set from here."""
        for key, value in (("iso", preset.iso), ("aperture", preset.aperture)):
            if value and key not in preset.by_hand and not self.camera.set(key, value):
                self._log(f"warning: could not set {key} to {value}; using the camera's current value")

    def capture_flatfield(
        self, roll: Roll, preset: Preset, cancelled: Callable[[], bool] = lambda: False, prompt: Optional[Callable[[str, str], None]] = None
    ) -> FlatField:
        """Photograph the bare light (no film in the holder) and store it as the roll's flat field.

        own-rgb: `prompt(letter, problem)` before each colour's shot, as for `scan`; a shot of the
        wrong colour is asked for again. The window takes them as separate jobs (`flat_colour`,
        then `finish_flatfield`)."""
        planes, worked = {}, {}
        for letter in KINDS[preset.kind].exposures if KINDS[preset.kind].prompted else (None,):
            problem = ""
            while True:
                if letter is not None:
                    if prompt is None:
                        raise ScanError("this preset's flat field is shot colour by colour, each shot prompted")
                    prompt(letter, problem)
                try:
                    found, exposure = self.flat_colour(preset, letter, cancelled)
                    break
                except WrongColour as exc:
                    problem = str(exc)
            planes.update(found)
            worked.update(exposure)
        return self.finish_flatfield(roll, preset, planes, worked)

    def flat_colour(self, preset: Preset, letter: Optional[str] = None, cancelled: Callable[[], bool] = lambda: False) -> tuple[dict, dict]:
        """The bare-light shots of one colour (`letter`, own-rgb), or of every exposure (None):
        {letter: plane} and {letter: the exposure that worked}. Light, aperture and ISO stay as
        they are; for an own light only the shutter moves, down to FLAT_FASTEST_S."""
        self._apply(preset)
        lamp = self.lamp(preset.kind)
        # First guess per exposure: what worked for this preset last time, else the preset's
        # exposure cut by roughly what a film base swallows.
        before = _bare_memory().get(preset.name, {})
        exposures = [channel for channel in channels(preset.kind, preset.levels) if letter is None or channel.name == letter]
        start = {
            channel.name: before.get(channel.name) or max(channel.nominal(preset.levels), 1) * true_seconds(preset.shutter_for(channel.name)) / BARE_LIGHT_GUESS
            for channel in exposures
        }
        with tempfile.TemporaryDirectory(prefix="tetherinvert-") as scratch:
            calibrator = Calibrator(lamp, self.camera, Path(scratch), self._settle_s, self._log, cancelled)
            try:
                planes, worked = calibrator.bare_light(preset.kind, start, preset.levels, letters=tuple(channel.name for channel in exposures))
            finally:
                framing = preset.framing_light()
                if framing is not None:
                    lamp.set(*framing)
                self.camera.set("shutter", preset.shutter)
        self._log(f"flat field: {calibrator.shots} exposure{'s' if calibrator.shots != 1 else ''}" + (f" in {EXPOSURES[letter].name}" if letter else ""))
        return dict(zip((channel.name for channel in exposures), planes, strict=True)), worked

    def finish_flatfield(self, roll: Roll, preset: Preset, planes: dict, worked: dict) -> FlatField:
        """The flat field from every exposure's bare-light plane, stored in the roll."""
        flat = FlatField.build(preset.kind, [planes[letter] for letter in KINDS[preset.kind].exposures], own=not KINDS[preset.kind].scanlight)
        flat.save(roll.folder)
        roll.set_settings(flatfield=True)  # here, so the window and `tetherinvert flatfield` both make the folder a roll that uses it
        try:  # only a head start for the next session; never worth failing over
            memory = _bare_memory()
            memory[preset.name] = worked
            home().mkdir(parents=True, exist_ok=True)
            (home() / BARE_FILE).write_text(json.dumps(memory, indent=2))
        except OSError:
            pass
        return flat

    @staticmethod
    def session_exposure(
        roll: Roll, preset: Preset, labels: Optional[list[str]] = None
    ) -> tuple[tuple[int, int, int, int], tuple[float, float, float]]:
        """LED levels and film base for scanning this roll with this preset.

        The preset puts the film base just under the sensor's ceiling where it was metered,
        usually on the rebate at the edge of the frame. With a flat field we know how much
        brighter the light is elsewhere, and lower each LED by that much: then the brightest part
        of the frame is what sits just under the ceiling, and nothing can clip. Without one,
        every LED is lowered by NO_FLAT_MARGIN.

        A kind with one exposure (white, rgb-together) lights every sensor channel at once, so
        all its LEDs are lowered by one factor, the smallest of the channels': that keeps the
        balance of the light's colour default, and the brightest channel still cannot clip.

        An own light has no LEDs to lower: its shutters are shortened (`session_shutters`, on
        the camera's ladder `labels`), and the film base is scaled by the true ratio of the times,
        per colour for own-rgb.
        """
        share = Scanner._brightest(roll, preset)
        if not KINDS[preset.kind].scanlight:
            ratios = [1.0, 1.0, 1.0]
            for letter, shutter in Scanner.session_shutters(roll, preset, labels).items():
                for channel in EXPOSURES[letter].sensors:
                    ratios[channel] = true_seconds(shutter) / true_seconds(preset.shutter_for(letter))
            base = tuple(value * ratio for value, ratio in zip(preset.base, ratios, strict=True))
            return preset.levels, (base[0], base[1], base[2])
        one = KINDS[preset.kind].single
        factors = [min(share)] * 4 if one else [share[0], share[1], share[2], share[0]]
        r, g, b, w = (max(1, round(level * factor)) if level else 0 for level, factor in zip(preset.levels, factors, strict=True))
        scaled = (r, g, b, w)
        if one:
            lit = max(range(4), key=lambda index: preset.levels[index])
            used = [scaled[lit] / preset.levels[lit]] * 3
        else:
            used = [(new / old if old else share[i]) for i, (new, old) in enumerate(zip(scaled[:3], preset.levels[:3], strict=True))]
        base = tuple(value * factor for value, factor in zip(preset.base, used, strict=True))
        return scaled, (base[0], base[1], base[2])

    @staticmethod
    def _brightest(roll: Roll, preset: Preset) -> tuple[float, float, float]:
        """Per channel, how bright the film base rectangle is against the brightest part of the
        frame: from the flat field, or NO_FLAT_MARGIN without one."""
        flat = usable_flat(roll, preset.kind)
        return flat.at(preset.roi) if flat is not None else (NO_FLAT_MARGIN,) * 3

    @staticmethod
    def session_shutters(roll: Roll, preset: Preset, labels: Optional[list[str]] = None) -> dict[str, str]:
        """The shutter for each exposure of a frame in this roll. A Scanlight kind lowers its LEDs
        instead and keeps the preset's. An own light has no LEDs: where the brightest part of
        the frame would rise above the target, the shutter is shortened in whole rungs of the
        camera's ladder (`labels`; a third-stop one when not given), each colour's on its own."""
        shutters = {letter: preset.shutter_for(letter) for letter in KINDS[preset.kind].exposures}
        share = Scanner._brightest(roll, preset)
        if KINDS[preset.kind].scanlight:
            return shutters
        ladder = usable_ladder(labels or list(THIRDS))
        limit = TARGET * (1 + TOLERANCE) * FULL
        for letter, shutter in shutters.items():
            longest = true_seconds(shutter) * min(limit * share[c] / max(preset.base[c], 1.0) for c in EXPOSURES[letter].sensors)
            if longest < true_seconds(shutter) * 0.99:
                fitting = [label for label in ladder if true_seconds(label) <= longest * 1.01]
                shutters[letter] = fitting[-1] if fitting else ladder[0]
        return shutters

    def scan(
        self,
        roll: Roll,
        preset: Preset,
        retake: bool = False,
        progress: Callable[[str], None] = lambda step: None,
        orientation: Optional[dict] = None,
        number: Optional[int] = None,
        prompt: Optional[Callable[[str, str], None]] = None,
    ) -> Frame:
        """Expose one frame with the preset and record it in the roll. It is numbered `number`, or
        by the roll's counter; a number the roll has already is a rescan and replaces that frame.
        `retake` replaces the last frame. `orientation` (orient.py) is how its files will be turned
        and mirrored. Afterwards the counter counts on from the last frame.

        own-rgb: `prompt(letter, problem)` is called before each shot and returns once the
        operator has set the light to that colour; a shot of the wrong colour is asked for again,
        with the reason as `problem`. The window takes the shots as separate jobs (`begin_scan`)."""
        shots = self.begin_scan(roll, preset, retake, orientation, number)
        try:
            problem = ""
            while not shots.complete:
                letter = shots.next_letter
                if shots.prompted:
                    if prompt is None:
                        raise ScanError("this preset is shot colour by colour, each shot prompted")
                    prompt(letter, problem)
                progress(letter)
                try:
                    shots.shoot()
                    problem = ""
                except WrongColour as exc:
                    problem = str(exc)
            return shots.finish()
        finally:
            shots.cancel()  # after finish, nothing is left to remove

    def begin_scan(
        self, roll: Roll, preset: Preset, retake: bool = False, orientation: Optional[dict] = None, number: Optional[int] = None
    ) -> "FrameScan":
        """A frame about to be exposed (see `scan`): its number decided, the camera set."""
        if retake and roll.frames:
            number = roll.last_number()
        elif number is None:
            number = roll.next_number()
        if number < paths.FIRST:
            raise ScanError(f"there is no frame {number} on a film: it starts at 00")
        lamp = self.lamp(preset.kind)
        # The preset's exposure is forced before every frame, so a bumped dial cannot spoil a scan.
        if not self.camera.set("shutter", preset.shutter):
            raise ScanError(f"the camera did not accept shutter {preset.shutter}; is it in manual mode?")
        self._apply(preset)
        labels = self.camera.choices("shutter")
        levels, base = self.session_exposure(roll, preset, labels)  # the preset's, never the light's default as it is now
        if levels != preset.levels:
            self._log(f"light at {KINDS[preset.kind].leds(levels)}: lowered from the preset so the brightest part of the frame stays under the ceiling")
        shutters = self.session_shutters(roll, preset, labels)  # the same rungs session_exposure scaled the film base by
        if shutters != {letter: preset.shutter_for(letter) for letter in shutters}:
            now = " · ".join(f"{letter} {shutter}" for letter, shutter in shutters.items())
            self._log(f"shutter at {now} s: shortened from the preset so the brightest part of the frame stays under the ceiling")
            fastest = usable_ladder(labels)[0] if usable_ladder(labels) else None
            if fastest in shutters.values():  # no rung short enough: the scan may clip, and the clipping check will say so
                self._log(f"even at {fastest} s the brightest part of the frame may clip: close the aperture or dim the light")
        return FrameScan(self, lamp, roll, preset, number, retake, orientation, levels, base, shutters)


def colour_check(path: Path, sensor: int, area: Roi) -> None:
    """Raise WrongColour unless `sensor`'s channel is the brightest in `area` of this RAW: the
    operator was asked for that colour alone. A wrong colour would spoil the frame unnoticed."""
    image = decode_linear(path, half=True)
    x0, y0, x1, y1 = area.pixels(image.shape[1], image.shape[0])
    means = image[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32).mean(axis=0)
    if int(np.argmax(means)) != sensor:
        name = EXPOSURES["RGB"[sensor]].name
        raise WrongColour(f"That did not look like {name} only.", f"Set your light to {name} only, then shoot again.")


class FrameScan:
    """One frame being exposed (Scanner.begin_scan): one shot per exposure of its kind, into a
    staging folder under raw/, and only a complete frame is moved into place (`finish`), so a
    cancelled or failed frame leaves nothing behind (`cancel`). Each shot can be its own job: the
    window shoots an own-rgb frame colour by colour, as the operator sets the light."""

    def __init__(self, scanner: "Scanner", lamp, roll: Roll, preset: Preset, number: int, retake: bool, orientation, levels, base, shutters) -> None:
        self._scanner, self._lamp, self.roll, self.preset = scanner, lamp, roll, preset
        self.number, self._retake, self._orientation = number, retake, orientation
        self._levels, self._base, self._shutters = levels, base, shutters
        self.prompted = KINDS[preset.kind].prompted
        self._letters = list(KINDS[preset.kind].exposures)
        raw_dir = roll.folder / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        self._raw_dir = raw_dir
        self._staging: Optional[Path] = Path(tempfile.mkdtemp(prefix=".scan-", dir=raw_dir))
        self._staged: list[Path] = []

    def use(self, scanner: "Scanner") -> None:
        """The devices as they are connected for this shot's job (one may have been reopened between shots)."""
        self._scanner = scanner
        self._lamp = scanner.lamp(self.preset.kind)

    @property
    def complete(self) -> bool:
        return len(self._staged) == len(self._letters)

    @property
    def next_letter(self) -> str:
        return self._letters[len(self._staged)]

    def shoot(self) -> Path:
        """The next exposure. A prompted one is checked for its colour first, and refused (WrongColour) if wrong."""
        letter, camera = self.next_letter, self._scanner.camera
        shutter = self._shutters[letter]
        if camera.get("shutter") != shutter and not camera.set("shutter", shutter):
            raise ScanError(f"the camera did not accept shutter {shutter}; is it in manual mode?")
        self._lamp.set(*EXPOSURES[letter].light(self._levels))
        time.sleep(self._scanner._settle_s)
        path = camera.capture(self._staging / f"{paths.stem(self.roll.name, self.number)}_{letter}")
        if path.stat().st_size < MIN_RAW_BYTES:
            path.unlink(missing_ok=True)
            raise ScanError(f"{path.name} is only {path.stat().st_size} bytes; the camera misfired")
        if self.prompted:
            try:
                colour_check(path, EXPOSURES[letter].sensors[0], Roi(*self.roll.rect) if self.roll.rect else Roi(0.0, 0.0, 1.0, 1.0))
            except WrongColour:
                path.unlink(missing_ok=True)
                raise
        self._staged.append(path)
        return path

    def finish(self) -> Frame:
        """Move the complete frame into place and record it in the roll."""
        if not self.complete:
            raise ScanError(f"frame {paths.label(self.number)} is not complete: {self.next_letter} is still to be shot")
        files = []
        for path in self._staged:
            final = self._raw_dir / path.name
            os.replace(path, final)
            files.append(str(final.relative_to(self.roll.folder)))
        self.cancel()
        preset = self.preset
        frame = Frame(self.number, files, preset.name, preset.kind, list(self._base), list(preset.roi))
        own = not KINDS[preset.kind].scanlight
        frame.light, frame.scanned = (preset.light_note or None) if own else _describe(self._lamp), time.time()
        frame.orientation = orient.normal(self._orientation) if self._orientation else None
        self.roll.add_frame(frame, preset)  # a roll without a preset copy keeps this one
        if not self._retake:
            self.roll.set_next(None)  # used: the next scan comes after the last frame (after a rescan too)
        return frame

    def cancel(self) -> None:
        """Remove what was staged, and put light and camera back for framing. Safe to call twice."""
        if self._staging is None:
            return
        shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None
        framing = self.preset.framing_light()
        if framing is not None:
            self._lamp.set(*framing)
        if self.prompted:
            self._scanner.camera.set("shutter", self.preset.shutter)  # the live view as calibrated, not on blue's shutter


def usable_flat(roll: Roll, kind: str) -> Optional[FlatField]:
    """The roll's flat field, if it uses one and it was taken under this kind's light."""
    flat = FlatField.load(roll.folder) if roll.flatfield else None
    return flat if flat is not None and flat.kind == kind else None


def _describe(light) -> Optional[str]:
    """The light as it names itself, for the files; a light that cannot say is left out."""
    try:
        return light.describe()
    except Exception:  # only a note in the files: never worth failing a scan over
        return None


def _bare_memory() -> dict:
    try:
        found = json.loads((home() / BARE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


# ----- rendering -----


def has_raws(roll: Roll, frame: Frame) -> bool:
    return all((roll.folder / name).exists() for name in frame.files)


def load_frame(roll: Roll, frame: Frame, plain_too: bool = False) -> tuple[np.ndarray, str, Optional[np.ndarray]]:
    """Decode a frame's exposures into one linear uint16 image.

    Returns the image, what was done, and (with `plain_too`, when a flat field was applied) the
    same frame without it, for comparison.
    """
    if not has_raws(roll, frame):
        raise ScanError(f"the RAW files of frame {paths.label(frame.number)} are gone, so it cannot be rendered again")
    raws = [roll.folder / name for name in frame.files]
    flat = usable_flat(roll, frame.kind)  # none taken under another light
    anchor = flat.at(frame.roi) if flat else (1.0, 1.0, 1.0)
    note = ", flat-field corrected" if flat else ""
    plain = None
    if len(raws) == 1:  # one exposure lit every channel: nothing to merge or register
        image = decode_linear(raws[0])
        if flat:
            plain = image.copy() if plain_too else None
            for channel in range(3):
                image[..., channel] = flat.correct(image[..., channel], channel, anchor[channel])
        how = "one white-light exposure" if not KINDS[frame.kind].colour else "one exposure, red, green and blue lit together"
        return image, how + note, plain
    with ThreadPoolExecutor(3) as pool:  # the decoder releases the interpreter lock
        exposures = list(pool.map(decode_linear, raws))
    shifts = None
    if flat:
        if plain_too:
            plain, shifts = merge_triplet(*exposures)  # registered once; the corrected frame reuses it
        # Each exposure is evened out in its own colour, before the three are put together.
        for channel, exposure in enumerate(exposures):
            exposure[..., channel] = flat.correct(exposure[..., channel], channel, anchor[channel])
    merged, shifts = merge_triplet(*exposures, shifts=shifts)
    moved = ", ".join(f"{dx:+.2f}/{dy:+.2f}" for dx, dy in shifts)
    return merged, f"red, green and blue exposures merged, green and blue registered by {moved} px" + note, plain


def _prepared(roll: Roll, frame: Frame, plain_too: bool = False) -> tuple[np.ndarray, str, Optional[list[float]], Optional[np.ndarray]]:
    """The frame as one linear image (single channel for black-and-white), with its film base,
    and on request the same frame without the flat field."""
    image, how, plain = load_frame(roll, frame, plain_too)
    base = list(frame.base) if any(frame.base) else None
    if not KINDS[frame.kind].colour:
        image = invert.luminance(image)
        plain = invert.luminance(plain) if plain is not None else None
        base = [float(np.dot(invert.LUMA, base))] if base else None
    return image, how, base, plain


def _measure(roll: Roll, frame: Frame, log_image: np.ndarray, base: Optional[list[float]]) -> invert.Bounds:
    """Measure the frame's own two points and note them in the roll, with what they depend on."""
    own = invert.measure(log_image, roll.rect, base, roll.per_channel)
    frame.bounds = {**own.to_json(), "rect": roll.rect, "per_channel": roll.per_channel, "flatfield": _flat_stamp(roll)}
    return own


def _current(roll: Roll, frame: Frame) -> bool:
    noted = frame.bounds
    same = noted is not None and noted.get("rect") == roll.rect and noted.get("per_channel") == roll.per_channel
    return same and noted.get("flatfield") == _flat_stamp(roll)


def _flat_stamp(roll: Roll) -> str:
    """Identifies the flat field a measurement was made with: '' for none."""
    flat = FlatField.load(roll.folder) if roll.flatfield else None
    return flat.created + flat.kind if flat else ""


def _tiff(roll: Roll, frame: Frame, kind: str, suffix: str = "") -> Path:
    """A TIFF of the frame; `suffix` names the comparison copy (`_noflat`)."""
    file = paths.tiff(roll, frame.number, kind)
    return file.with_name(f"{file.stem}{suffix}.tif")


def render(roll: Roll, frame: Frame, *, negative: bool = False) -> list[Path]:
    """Write the frame's flat positive, the roll's master (`scans/name.tif`): every print is developed
    from it later. With `negative`, also the merged image as it is, not inverted, gamma 1.0
    (`negative/name.tif`). Keyword only, so a stray positional argument cannot turn it on.

    With `roll.compare` and a flat field, each file is also written without the correction
    (`..._noflat.tif`): same registration, same points, so the flat field is the only
    difference between the pair. Without it, a comparison copy left from an earlier render of
    the same file is removed, because it would no longer match.
    """
    image, how, base, plain = _prepared(roll, frame, plain_too=roll.compare)
    when = (roll.folder / frame.files[0]).stat().st_mtime
    rect = roll.rect if roll.crop else None

    def out(pixels: np.ndarray) -> np.ndarray:
        """What goes into the file: cropped in the camera's frame, then turned as the frame was scanned."""
        return orient.image(preview.crop(pixels, rect), frame.orientation)

    versions = [(image, "", how)]
    if plain is not None:
        versions.append((plain, "_noflat", how.replace(", flat-field corrected", ", NOT flat-field corrected (comparison copy)")))
    log_image = invert.to_log(image)
    own = _measure(roll, frame, log_image, base)
    context = metadata.scan_context(roll)
    written = []
    for picture, suffix, done in versions:
        log_picture = log_image if picture is image else invert.to_log(picture)
        note = f"TetherInvert flat positive: {done}. Inverted between film base and densest tone, no contrast curve."
        target = _tiff(roll, frame, "flat", suffix)
        write_tiff(target, out(invert.flat_positive(log_picture, own)), 2.2, note, roll.primaries, when, metadata.xmp(roll, frame, "flat", suffix, context=context))
        written.append(target)
        if negative:
            note = f"TetherInvert linear negative: {done}. No white balance, no inversion, gamma 1.0."
            target = _tiff(roll, frame, "linear", suffix)
            write_tiff(target, out(picture), 1.0, note, roll.primaries, when, metadata.xmp(roll, frame, "linear", suffix, context=context))
            written.append(target)
    if plain is None:  # no comparison copy this time: one left from before would no longer match its partner
        for target in written:
            target.with_name(f"{target.stem}_noflat.tif").unlink(missing_ok=True)
    preview.write(roll, frame.number, image, own, rect=roll.rect, cropped=rect is not None)
    frame.rendered = time.time()
    Roll(roll.folder).note_bounds(frame)  # not `roll`: a change refreshes its caller, and render_roll's settings would change mid-roll
    return written


def clipping(roll: Roll, frame: Frame) -> Optional[str]:
    """A warning when the frame's RAW files are clipped inside the picture area (the whole frame
    without one), or None. Each exposure is checked on the photosites of the colours it is for
    (kinds.EXPOSURES): one LED's on its own colour, white light and rgb-together on the worst of
    the three (green, under the white LED). Clipped film base turns into crushed shadows, and no
    render brings it back."""
    area = Roi(*roll.rect) if roll.rect else Roi(0.0, 0.0, 1.0, 1.0)
    names = ("red", "green", "blue")
    found = []
    for name in frame.files:
        letter = Path(name).stem.rsplit("_", 1)[-1]
        sensors = [(sensor, names[sensor]) for sensor in EXPOSURES[letter].sensors] if letter in EXPOSURES else [(None, "all")]
        share, colour = max((clipped_fraction(roll.folder / name, sensor, area), colour) for sensor, colour in sensors)
        if share > CLIP_WARN:
            found.append(f"{colour} {share:.1%}")
    if not found:
        return None
    clipped = f"Frame {paths.label(frame.number)} is clipped ({', '.join(found)} of the picture area at the sensor ceiling)"
    if usable_flat(roll, frame.kind) is None:
        # The usual cause is not the f-stop: the light is brighter in the middle than at the film base rectangle.
        lowered = "the LEDs are lowered by a quarter stop" if KINDS[frame.kind].scanlight else "the shutter is shortened by a third of a stop or more"
        return (
            f"{clipped}, and the shadows will be crushed. Without a flat field {lowered}, and somewhere in the frame the light is "
            "brighter than that compared with the film base rectangle. Capture a flat field for this roll (it lowers the exposure as "
            "far as needed), or calibrate on rebate nearer the middle of the frame. Check the f-stop too."
        )
    return (
        f"{clipped}: more light came through than at calibration, and the shadows will be crushed. "
        "Check the f-stop; if it is right, recalibrate on this film."
    )


def render_roll(roll: Roll, *, negative: bool = False, progress: Callable[[int, int], None] = lambda done, total: None) -> list[Path]:
    """Render every frame again from its RAW files, with the picture area, crop and flat field
    as they are set now. Frames whose RAW files were cleaned up are left as they are."""
    frames = [frame for frame in roll.frames if has_raws(roll, frame)]
    if not frames:
        raise ScanError(f"roll {roll.name} has no RAW files left to render from")
    written = []
    for index, frame in enumerate(frames):
        written += render(roll, frame, negative=negative)
        progress(index + 1, len(frames))
    return written


def outputs(roll: Roll, frame: Frame) -> list[Path]:
    """The frame's scan on disk, its flat positive: what `clean_raws` needs before RAW files may go."""
    flat = paths.tiff(roll, frame.number, "flat")
    return [flat] if flat.exists() else []


# ----- prints -----
#
# A print is the frame's flat TIFF, the roll's master, developed with the frame's development
# (development.py): its base and corrections, with its own points or, for a matched roll, the
# roll's shared ones. No RAW file is needed. The window shows every frame by the same development,
# from the preview, so the file is what was seen. Prints go to `prints/`, every frame gets one.


def print_path(roll: Roll, frame: Frame) -> Path:
    return paths.tiff(roll, frame.number, "print")


def write_print(roll: Roll, dev: development.Development) -> Path:
    """Write the frame's print, `prints/name.tif`: its flat TIFF developed with `dev`, its development."""
    frame = roll.frame(dev.number)
    try:
        pixels = development.picture(dev, None).values
    except development.Undeveloped as exc:
        raise ScanError(str(exc)) from None
    source = paths.tiff(roll, frame.number, "flat")
    a = dev.adjust
    keys = " ".join(f"{key.upper()}{a[key]:+d}" for key in ("d", "c", "m", "y"))
    whose = "points shared across the roll" if roll.matched else "its own points"
    note = f"TetherInvert print: {source.name} as {dev.base} ({whose}), {keys}, contrast {a['grad']:+d}, saturation {a['sat']:+d}, tone {a['tone']}."
    target = print_path(roll, frame)
    made = dev.made_with
    packet = metadata.xmp(roll, frame, "print", made=made)
    write_tiff(target, (pixels * invert.FULL + 0.5).astype(np.uint16), 2.2, note, roll.primaries, source.stat().st_mtime, packet)
    frame.printed = made
    Roll(roll.folder).note_bounds(frame)  # not `roll`, as in render: write_prints keeps the settings it started with
    return target


class PrintsError(ScanError):
    """Some prints of a batch could not be written. The others were: `written`."""

    def __init__(self, written: list[Path], problems: list[str]) -> None:
        super().__init__(f"{len(problems)} print{'s' if len(problems) != 1 else ''} not written: {problems[0]}")
        self.written, self.problems = written, problems


def write_prints(roll: Roll, progress: Callable[[int, int], None] = lambda done, total: None) -> list[Path]:
    """Every print that is missing or out of date, in one go. One that cannot be written does
    not stop the others; a PrintsError afterwards says which failed and carries the ones written."""
    developments = development.of_roll(roll)
    due = [developments[frame.number] for frame in roll.frames if frame.number in developments.due]
    written, problems = [], []
    for index, dev in enumerate(due):
        try:
            written.append(write_print(roll, dev))
        except Exception as exc:  # a missing flat file, a frame being rendered again: the next frame is unaffected
            problems.append(str(exc))
        progress(index + 1, len(due))
    if problems:
        raise PrintsError(written, problems)
    return written


def frame_tiffs(roll: Roll, frame: Frame) -> list[tuple[Path, str, str]]:
    """Every TIFF of a frame that is on disk, with its kind ("flat", "linear", "print") and
    suffix ("" or "_noflat")."""
    found = []
    for kind in paths.FOLDERS:
        for suffix in ("", "_noflat"):
            file = _tiff(roll, frame, kind, suffix)
            if file.exists():
                found.append((file, kind, suffix))
    return found


def retag(roll: Roll, numbers: Optional[list[int]] = None, progress: Callable[[int, int], None] = lambda done, total: None) -> list[Path]:
    """Write the metadata as it is now into every TIFF of these frames (all: None), scans, prints
    and negatives alike. Only the XMP is replaced; the pixels are not touched."""
    context = metadata.scan_context(roll)
    frames = [frame for frame in roll.frames if numbers is None or frame.number in numbers]
    files = [(frame, *found) for frame in frames for found in frame_tiffs(roll, frame)]
    done = []
    for index, (frame, file, kind, suffix) in enumerate(files):
        made = frame.printed if kind == "print" else None
        try:
            set_xmp(file, metadata.xmp(roll, frame, kind, suffix, made=made, context=context))
            done.append(file)
        except FileNotFoundError:  # deleted meanwhile, with its frame: nothing left to write into
            pass
        progress(index + 1, len(files))
    return done


def frame_files(roll: Roll, frame: Frame) -> list[Path]:
    """Every file of a frame that is on disk: RAW files, TIFFs (comparison copies and the print
    included) and the preview."""
    tiffs = [file for file, _kind, _suffix in frame_tiffs(roll, frame)]
    files = [roll.folder / name for name in frame.files] + tiffs + [preview.path(roll, frame.number)]
    return [file for file in files if file.exists()]


def reorient_frame(roll: Roll, number: int, orientation: Optional[dict]) -> list[Path]:
    """Store a scanned frame another way up: each of its TIFFs (comparison copies and the print
    too) is turned from the way it was written, in place, and the frame records the new way. No
    RAW file is needed; the RAW files and the preview stay as the camera saw them."""
    frame = roll.frame(number)
    old, new = orient.normal(frame.orientation), orient.normal(orientation)
    if old == new:
        return []
    turned = [file for file in frame_files(roll, frame) if file.suffix == ".tif"]
    for file in turned:
        rewrite_tiff(file, lambda pixels: orient.reoriented(pixels, old, new))
    roll.update_frame(number, orientation=new)
    return turned


def delete_frame(roll: Roll, number: int) -> list[Path]:
    """Delete a frame: all its files and its entry in roll.json. Returns the files deleted.
    The numbers of the other frames stay; if it was the last, the next scan takes its number."""
    frame = roll.frame(number)
    gone = frame_files(roll, frame)
    for file in gone:
        file.unlink(missing_ok=True)
    roll.remove_frame(number)
    raw_dir = roll.folder / "raw"
    if raw_dir.is_dir() and not any(raw_dir.iterdir()):  # a scan under way has its staging folder there: then it stays
        raw_dir.rmdir()
    return gone


def clean_raws(roll: Roll, dry_run: bool = False) -> tuple[int, int, list[int]]:
    """Delete the RAW files of every frame that has a TIFF. Returns (files, bytes, frames kept
    because they have no TIFF yet). After this those frames can no longer be rendered again."""
    files, size, kept = 0, 0, []
    for frame in roll.frames:
        paths = [roll.folder / name for name in frame.files if (roll.folder / name).exists()]
        if not paths:
            continue
        if not outputs(roll, frame):
            kept.append(frame.number)
            continue
        for path in paths:
            files, size = files + 1, size + path.stat().st_size
            if not dry_run:
                path.unlink()
    raw_dir = roll.folder / "raw"
    if not dry_run and raw_dir.is_dir() and not any(raw_dir.iterdir()):
        raw_dir.rmdir()
    return files, size, kept
