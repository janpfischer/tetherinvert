"""Simulated Scanlight and camera, for developing and testing without the rig.

The camera photographs a synthetic colour negative in a film holder: an opaque holder, a
clear rebate (film base with its orange mask) and a picture with a neutral step wedge along
the bottom. Stills are real Bayer DNG files, so the whole pipeline runs on them unchanged.

With TETHERINVERT_SIM_PICTURES set to a folder of ordinary photos, the frames of a roll show those
instead, frame n the nth by name, 0 the last and 00 the one before: for screenshots and demos. Only the scene changes; the film it is made into, the
holder and the light stay as they are.
"""

from __future__ import annotations

import contextlib
import functools
import io
import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import tifffile

from . import paths
from .kinds import EXPOSURES

SENSOR = (2000, 3000)  # rows, columns; RGGB
BLACK, WHITE = 512, 16383
RESPONSE = np.array([0.010, 0.045, 0.110], np.float32)  # clear-light signal per LED count per second, ISO 100
WHITE_RESPONSE = np.array([0.020, 0.032, 0.026], np.float32)
# What each sensor channel (row: R, G, B) reads of each LED (column: red, green, blue), uneven as on a
# real sensor: the green LED reaches the blue channel at 12 % and the red at 4 %, the blue LED green
# at 8 %, the red LED green at 2 %. A three-shot frame uses only each LED's own channel, so it does
# not see this; a shot with all three lit together (rgb-together) does.
CROSSTALK = np.array([[0.97, 0.04, 0.01], [0.02, 0.97, 0.08], [0.01, 0.12, 0.97]], np.float32)
COLOUR_DEFAULT = (200, 170, 230)  # the simulated light's stored colour default: uneven, as one set on real film is
LED_CURVE = 0.97  # LEDs are slightly concave
DMIN = np.array([0.25, 0.60, 0.90], np.float32)  # orange mask
GAMMA = np.array([0.60, 0.65, 0.70], np.float32)
SCENE_FLOOR = 0.002
LENS_BLUR = 0.8  # sensor pixels, standard deviation
WEDGE = (0.01, 0.025, 0.06, 0.12, 0.25, 0.45, 0.7, 0.9)  # scene luminance of the grey patches

FILM = (0.04, 0.03, 0.96, 0.97)  # top, left, bottom, right of the film in the holder window
PICTURE = (0.10, 0.07, 0.90, 0.93)
BASE_ROI = (0.035, 0.30, 0.025, 0.40)  # x, y, w, h of a patch on the clear rebate
PICTURE_RECT = (PICTURE[1], PICTURE[0], PICTURE[3] - PICTURE[1], PICTURE[2] - PICTURE[0])  # x, y, w, h
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".tif", ".tiff", ".png")

SHUTTERS = ("Bulb", "30", "4", "2", "1.6", "1.3", "1", "0.8", "0.6", "1/2", "0.4", "1/3", "1/4", "1/5", "1/6", "1/8",
            "1/10", "1/13", "1/15", "1/20", "1/25", "1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125", "1/160",
            "1/200", "1/250", "1/500", "1/4000")  # fmt: skip


def seconds(label: str) -> float:
    numerator, _, denominator = label.partition("/")
    return float(numerator) / float(denominator) if denominator else float(numerator)


def wedge_boxes() -> list[tuple[float, float, float, float]]:
    """(x, y, w, h) of each grey patch, as fractions of the frame."""
    top, left, bottom, right = PICTURE
    width = (right - left) / len(WEDGE)
    return [(left + i * width + 0.2 * width, bottom - 0.10, 0.6 * width, 0.07) for i in range(len(WEDGE))]


def pictures(folder: Optional[str]) -> list[Path]:
    """The photos in TETHERINVERT_SIM_PICTURES's folder, by name; none without one."""
    if not folder:
        return []
    found = sorted(p for p in Path(folder).expanduser().iterdir() if p.suffix.lower() in PHOTO_SUFFIXES and not p.name.startswith("."))
    if not found:
        raise ValueError(f"TETHERINVERT_SIM_PICTURES: no JPEG, TIFF or PNG in {folder}")
    return found


def photo(path: Path, height: int, width: int) -> np.ndarray:
    """A photo as linear RGB, cut to fill height × width. A portrait one goes in turned, as it lies
    on film."""
    pixels = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_ANYDEPTH)  # turned by its EXIF orientation
    if pixels is None:
        raise ValueError(f"cannot read the picture {path}")
    if pixels.shape[0] > pixels.shape[1]:
        pixels = cv2.rotate(pixels, cv2.ROTATE_90_COUNTERCLOCKWISE)
    scale = max(height / pixels.shape[0], width / pixels.shape[1])
    rows, columns = max(height, round(pixels.shape[0] * scale)), max(width, round(pixels.shape[1] * scale))
    pixels = cv2.resize(pixels, (columns, rows), interpolation=cv2.INTER_AREA)
    top, left = (rows - height) // 2, (columns - width) // 2
    value = cv2.cvtColor(pixels[top : top + height, left : left + width], cv2.COLOR_BGR2RGB).astype(np.float32) / np.iinfo(pixels.dtype).max
    return np.where(value <= 0.04045, value / 12.92, ((value + 0.055) / 1.055) ** 2.4).astype(np.float32)  # sRGB to linear


def scene(height: int, width: int, seed: int, picture_file: Optional[Path] = None) -> np.ndarray:
    """The photographed scene as linear RGB, 0 where the film was never exposed, -1 on the holder.
    Coloured discs on a gradient above a grey wedge, or the photo `picture_file`."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    y, x = yy / height, xx / width
    film = (y >= FILM[0]) & (y < FILM[2]) & (x >= FILM[1]) & (x < FILM[3])
    inside = (y >= PICTURE[0]) & (y < PICTURE[2]) & (x >= PICTURE[1]) & (x < PICTURE[3])
    if picture_file is None:
        picture = np.stack([0.03 + 0.5 * x * (1 - 0.5 * y)] * 3, axis=-1)
        for _ in range(12):
            cx, cy, radius = rng.uniform(0.1, 0.9), rng.uniform(0.15, 0.7), rng.uniform(0.04, 0.14)
            colour = rng.uniform(0.02, 0.9, 3).astype(np.float32)
            picture[(x - cx) ** 2 + ((y - cy) * height / width) ** 2 < radius**2] = colour
    else:
        rows, columns = np.flatnonzero(inside.any(axis=1)), np.flatnonzero(inside.any(axis=0))
        picture = np.zeros((height, width, 3), np.float32)
        picture[rows[0] : rows[-1] + 1, columns[0] : columns[-1] + 1] = photo(picture_file, len(rows), len(columns))
    picture *= 1.0 + 0.05 * rng.standard_normal((height, width, 3)).astype(np.float32)  # grain, separate per dye layer
    if picture_file is None:
        for (bx, by, bw, bh), level in zip(wedge_boxes(), WEDGE, strict=True):
            picture[(x >= bx) & (x < bx + bw) & (y >= by) & (y < by + bh)] = level
    out = np.full((height, width, 3), -1.0, np.float32)
    out[film] = 0.0
    out[inside] = np.clip(picture[inside], SCENE_FLOOR, 1.0)
    return out


def transmittance(positive: np.ndarray) -> np.ndarray:
    """What the negative of that scene lets through, per channel."""
    density = DMIN + GAMMA * np.log10(np.clip(positive, SCENE_FLOOR, 1.0) / SCENE_FLOOR)
    passed = np.power(10.0, -density).astype(np.float32)
    passed[positive[..., 0] < 0] = 1e-4  # the holder
    return passed


@functools.lru_cache(maxsize=4)
def illumination(height: int, width: int, falloff: tuple[float, float, float]) -> np.ndarray:
    """Uneven light and lens falloff per LED colour: brightest near the centre, dimmer towards
    the corners by `falloff`, and tilted a little from left to right."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    x, y = xx / width - 0.5, yy / height - 0.5
    radius_squared = (x * x + y * y) / 0.5
    tilt = 1.0 + 0.06 * x
    return np.stack([(1.0 - strength * radius_squared) * tilt for strength in falloff], axis=-1).astype(np.float32)


@contextlib.contextmanager
def film_removed(camera):
    """Stands in for the operator taking the film out. Does nothing with a real camera."""
    had_film = getattr(camera, "film_loaded", None)
    if had_film is not None:
        camera.film_loaded = False
    try:
        yield
    finally:
        if had_film is not None:
            camera.film_loaded = had_film


class SimLight:
    def __init__(self) -> None:
        self.color = (0, 0, 0, 0)
        self.default = COLOUR_DEFAULT  # what default_rgb() reads; a test may change it, as the web app would

    def set(self, r: int = 0, g: int = 0, b: int = 0, w: int = 0) -> None:
        if w and (r or g or b):
            raise ValueError("white cannot be lit together with R, G or B")
        self.color = (int(r), int(g), int(b), int(w))

    def off(self) -> None:
        self.color = (0, 0, 0, 0)

    def info(self, timeout: float = 2.0) -> tuple[int, int]:
        return 7, 3

    def default_rgb(self, timeout: float = 2.0) -> tuple[int, int, int]:
        return self.default

    def describe(self) -> str:
        return "Simulated Scanlight v4b"

    def has_white(self) -> bool:
        return True

    def temperature(self) -> Optional[float]:
        return 35.0

    def close(self) -> None:
        self.off()


class SimOwnLight:
    """The operator's own light, which TetherInvert never drives: it has no `set`. A test (or the
    hooks below, standing in for the operator) sets it with `show`: white (a white panel, through
    WHITE_RESPONSE), all three narrowband colours at once (crosstalk as CROSSTALK says), one of
    them alone, or off. `brightness` scales it; with `flicker` it is dimmed by pulse width, which
    shows as bands across the frame at shutters faster than 1/250 s."""

    SHOWN = {"white": (0, 0, 0, 255), "colour": (255, 255, 255, 0), "R": (255, 0, 0, 0), "G": (0, 255, 0, 0), "B": (0, 0, 255, 0), "off": (0, 0, 0, 0)}

    def __init__(self, brightness: float = 1.0) -> None:
        self.color = (0, 0, 0, 0)
        self.brightness = brightness
        self.flicker = False

    def show(self, what: str) -> None:
        self.color = self.SHOWN[what]


def set_by_hand(camera, kind: str, letter: str) -> None:
    """Stands in for the operator setting their own light before a shot: red only, or as it is
    for one exposure (all colours for own-colour, white for own-bw). Does nothing with a real
    camera, or a simulated one under the Scanlight, like `film_removed`."""
    lamp = getattr(camera, "_light", None)
    if isinstance(lamp, SimOwnLight):
        lamp.show(letter if letter in "RGB" else "white" if kind == "own-bw" else "colour")


class Bench:
    """What physically lights the film on the simulated rig. The light is opened once and kept;
    every camera opened on the bench photographs through it, so a camera reopened after a
    dropout sees the same light the window has been setting."""

    def __init__(self, own: bool = False) -> None:
        self.light = SimLight()  # the Scanlight; with --no-light TetherInvert leaves it alone (`tetherinvert light` still drives it)
        self.lamp = SimOwnLight() if own else None  # with --no-light: the operator's light, which lights the film instead
        if self.lamp is not None:
            self.lamp.show("colour")  # switched on, as the operator would before framing

    def camera(self) -> "SimCamera":
        camera = SimCamera(self.lamp or self.light, falloff=(0.20, 0.15, 0.25))  # uneven light, so the flat field has work to do
        camera.manual_lens = os.environ.get("TETHERINVERT_SIM_MANUAL_LENS") == "1"
        camera.pictures = pictures(os.environ.get("TETHERINVERT_SIM_PICTURES"))
        return camera


class SimCamera:
    """Same surface as GphotoCamera. The next negative is in the holder when a still is named for
    another frame than the one before (Roll_002_R after Roll_001_B), the way an operator advances
    the film; stills not named for a frame (calibration, the flat field) never move it on."""

    model = "Simulated Camera"
    has_live_view = True

    def __init__(self, light: SimLight, shake: tuple[float, float] = (0.0, 0.0), falloff: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> None:
        self._light = light
        self.falloff = falloff  # per LED colour: how much dimmer the corners are than the centre
        self.film_loaded = True  # False: the holder is empty and the bare light shows
        self.manual_lens = False  # True: the lens has no contacts, the camera knows no aperture
        self.pictures: list[Path] = []  # photos the frames show in turn, instead of the synthetic scene
        self._settings = {"iso": "100", "shutter": "1/4", "aperture": "f/8"}
        self._options = {"iso": ["100", "200", "400", "800"], "shutter": list(SHUTTERS), "aperture": ["f/5.6", "f/8", "f/11"]}
        self._rng = np.random.default_rng(1)
        self._shooting: Optional[str] = None  # the frame the last still was named for (its stem without the exposure letter)
        self._cache: dict[tuple[int, int, int, Optional[Path]], np.ndarray] = {}
        self.frame = 1  # the negative in the holder: the roll's first
        self.shake = shake  # (dx, dy) in pixels: green is displaced by +shake, blue by -shake
        self.last_name = "capt_SIM00001.DNG"

    def open(self) -> None:
        pass

    def keeps_card_copy(self) -> bool:
        return False

    def close(self) -> None:
        pass

    def choices(self, key: str) -> list[str]:
        return [] if key == "aperture" and self.manual_lens else list(self._options[key])

    def get(self, key: str) -> Optional[str]:
        return None if key == "aperture" and self.manual_lens else self._settings[key]

    def set(self, key: str, value: str) -> bool:
        if value not in self.choices(key) or value == "Bulb":
            return False
        self._settings[key] = value
        return True

    def _film(self, height: int, width: int) -> np.ndarray:
        key = (height, width, self.frame if self.film_loaded else -1, self._picture())
        if key not in self._cache:
            self._cache = {k: v for k, v in self._cache.items() if k[2:] == key[2:]}
            film = transmittance(scene(height, width, self.frame, key[3]))
            if not self.film_loaded:
                film = np.where(film > 2e-4, 1.0, film).astype(np.float32)  # the holder stays, the film is out
            # No lens draws an edge sharper than a pixel; without this the mosaic aliases.
            self._cache[key] = cv2.GaussianBlur(film, (0, 0), LENS_BLUR * width / SENSOR[1])
        return self._cache[key]

    def _picture(self) -> Optional[Path]:
        """The photo on the negative in the holder: picture n on frame n (0 the last, 00 the one before), by
        the number the last still was named for, so a rescan or a scan from the command line shows the same
        one as the window's."""
        if not self.pictures:
            return None
        number = self._shooting.rpartition("_")[2] if self._shooting else ""
        return self.pictures[((paths.parse(number) if number.isdigit() else self.frame) - 1) % len(self.pictures)]

    def _signal(self, film: np.ndarray) -> np.ndarray:
        """Sensor signal as a fraction of full scale."""
        r, g, b, w = self._light.color
        leds = RESPONSE * 255.0 * np.power(np.array([r, g, b], np.float32) / 255.0, LED_CURVE)
        if any(self.falloff):
            film = film * illumination(film.shape[0], film.shape[1], self.falloff)
        through = film * leds  # each LED's light after the dye that absorbs it
        signal = through @ CROSSTALK.T
        signal += film * WHITE_RESPONSE * 255.0 * (w / 255.0) ** LED_CURVE
        aperture = float(self._settings["aperture"].lstrip("f/"))
        exposure = seconds(self._settings["shutter"])
        gain = exposure * float(self._settings["iso"]) / 100.0 * (8.0 / aperture) ** 2 * getattr(self._light, "brightness", 1.0)
        if getattr(self._light, "flicker", False) and exposure < 1 / 250 * 0.99:  # a few pulses of the light during the slit's travel
            rows = np.arange(film.shape[0], dtype=np.float32)[:, None, None] / film.shape[0]
            signal = signal * (1.0 + 0.3 * np.sin(2 * np.pi * 7 * rows))
        return signal * gain

    def report(self) -> list[tuple[str, str]]:
        return sorted((key, value) for key, value in self._settings.items() if self.get(key))

    def capture(self, stem: Path, check: bool = True) -> Path:
        r, g, b, w = self._light.color
        frame, _, letter = Path(stem).name.rpartition("_")
        if frame and letter in EXPOSURES:
            if self._shooting is not None and frame != self._shooting:
                self.frame += 1
            self._shooting = frame
        film = self._film(*SENSOR)
        if any(self.shake) and (g or b) and not r:  # the film drifts between the three exposures
            sign = 1.0 if g else -1.0
            matrix = np.float32([[1, 0, sign * self.shake[0]], [0, 1, sign * self.shake[1]]])
            film = cv2.warpAffine(film, matrix, (SENSOR[1], SENSOR[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        signal = self._signal(film)
        cfa = np.empty(SENSOR, np.float32)  # each photosite sees the scene at its own position
        cfa[0::2, 0::2], cfa[0::2, 1::2] = signal[0::2, 0::2, 0], signal[0::2, 1::2, 1]
        cfa[1::2, 0::2], cfa[1::2, 1::2] = signal[1::2, 0::2, 1], signal[1::2, 1::2, 2]
        counts = cfa * (WHITE - BLACK)
        counts += self._rng.standard_normal(SENSOR).astype(np.float32) * np.sqrt(np.clip(counts, 0, None) + 4.0)
        raw = np.clip(counts + BLACK, 0, WHITE).astype(np.uint16)
        tags = [  # the minimum a raw decoder needs to read a Bayer DNG
            (50706, "B", 4, (1, 4, 0, 0), True),  # DNGVersion
            (50708, "s", 0, self.model, True),  # UniqueCameraModel
            (33421, "H", 2, (2, 2), True),  # CFARepeatPatternDim
            (33422, "B", 4, (0, 1, 1, 2), True),  # CFAPattern: RGGB
            (50714, "H", 1, (BLACK,), True),  # BlackLevel
            (50717, "H", 1, (WHITE,), True),  # WhiteLevel
            (50721, "2i", 9, (1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1), True),  # ColorMatrix1: identity
            (50728, "2I", 3, (1, 1, 1, 1, 1, 1), True),  # AsShotNeutral
        ]
        buffer = io.BytesIO()
        tifffile.imwrite(buffer, raw, photometric=32803, subfiletype=0, extratags=tags, metadata=None)
        path = Path(str(stem) + ".dng")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.getvalue())
        return path

    def preview(self) -> Optional[bytes]:
        display = np.clip(4.0 * self._signal(self._film(426, 640)), 0.0, 1.0) ** (1 / 2.2)  # a camera brightens its live view
        ok, jpeg = cv2.imencode(".jpg", cv2.cvtColor((display * 255).astype(np.uint8), cv2.COLOR_RGB2BGR))
        return jpeg.tobytes() if ok else None
