"""Exposure calibration on the clear film base.

ISO and aperture stay as set on the camera. The film base is metered inside a rectangle and
exposed just below clipping, because the base is the brightest thing a negative lets through.

The model is linear:  signal = k * LED level * shutter seconds,  with k measured per channel.

RGB: one shutter is shared by the red, green and blue exposures, and each LED gets its own
level so all three land on the same target. That balance on the base is the white balance.
The shared shutter is the fastest one that keeps the weakest channel's LED at or below
LEVEL_SOLVE, which leaves room to trim upwards afterwards.

White: the same solve with one LED, metering whichever sensor channel is brightest.

RGB together: the light stays at its own colour default, read from it, and only the shutter is
calibrated: the slowest one at which the brightest channel's film base stays under the target. The
other channels land wherever the default and the film's mask put them. An own light, set by hand,
is calibrated the same way (`_shutter_only`).

Every shot is reported as a Step with a histogram of the whole frame, in stops below the
sensor's ceiling, so the window can show each colour walk onto the target (W4).
"""

from __future__ import annotations

import math
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .kinds import EXPOSURES, KINDS
from .paths import home
from .raw import FULL, RawError, clipped_fraction, decode_linear

TARGET = 0.90  # of full scale, for the 99.9th percentile of the base
TOLERANCE = 0.03
CLIP_BUDGET = 0.002  # share of base photosites allowed at the sensor ceiling
BASE_SHARE = 0.85  # pixels at least this bright, relative to the metered peak, are film base
FLAT_TARGET = 0.75  # for the bare light of a flat field: well exposed, nowhere near clipping
FLAT_MIN, FLAT_MAX = 0.35, 0.92  # ...and any unclipped shot in this window is good enough for it
LEVEL_MIN, LEVEL_MAX, LEVEL_SOLVE = 40, 255, 240
FIXED_LEVEL = 255  # a light that is not calibrated counts as this level, so level x seconds still measures an exposure
FACTORY_DEFAULT = (255, 255, 255)  # what a Scanlight whose colour default was never set reads (scanlight.py)
FACTORY_NOTE = (
    "This is the light's factory default (all LEDs at full). If you set your own in the Scanlight web app, it was not stored: "
    "set it there again, then recalibrate."
)
# The LEDs are pulse-width modulated; exposures shorter than 1/250 s catch too few pulses.
FASTEST_S, SLOWEST_S = 1 / 250, 2.0
# An own light's bare-light shot may go down to this: nothing is known about its pulses, and
# the bare light is far brighter than film. Not faster: at short times the shutter is a moving
# slit and exposes the frame a little unevenly, which a flat field would build into every scan.
FLAT_FASTEST_S = 1 / 1000
# An own light calibrated at this (calibration's fastest rung) may leave its flat field no shutter
# short enough; W4 says so, while a change still costs nothing.
BRIGHT_S = FASTEST_S  # 2 stops slower than FLAT_FASTEST_S: calibration landed on its fastest rung, so the light may be brighter still
BRIGHT_NOTE = (
    "Your light is bright: the flat field may not find a short enough shutter. Close the aperture a stop or dim the light now, "
    "and calibrate again."
)
# A third-stop shutter ladder, named as cameras name it: what a session's shortened shutter is told
# by when the camera's own labels are not at hand (the window's session text).
THIRDS = ("1/1000", "1/800", "1/640", "1/500", "1/400", "1/320", "1/250", "1/200", "1/160", "1/125", "1/100", "1/80", "1/60", "1/50",
          "1/40", "1/30", "1/25", "1/20", "1/15", "1/13", "1/10", "1/8", "1/6", "1/5", "1/4", "1/3", "0.4", "1/2", "0.6", "0.8", "1",
          "1.3", "1.6", "2")  # fmt: skip
OWN_GUESS_S = 1 / 30  # an own light's first probe: nothing is known about its brightness, the probe walks from here
PROBE_MIN, PROBE_MAX = 0.05, 0.95  # a probe must land in this window to measure k
# Starting guess for a colour negative at ISO 100, f/8 (level * seconds per channel).
START_EXPOSURE = {"R": 80.0, "G": 40.0, "B": 32.0, "W": 20.0}
# The histogram of a calibration shot: this many stops below the ceiling, in bins of a tenth of a
# stop. Darker pixels (the holder) are left out. Every second pixel of the half-size decode in
# each direction is plenty for its shape and keeps it to about 10 ms a shot.
HISTOGRAM_STOPS, HISTOGRAM_BINS, HISTOGRAM_STRIDE = 10.0, 100, 2


class CalibrationError(RuntimeError):
    """Calibration cannot reach the target. `advice` says which way to move ISO or aperture."""

    def __init__(self, message: str, advice: str = "") -> None:
        super().__init__(f"{message} {advice}".strip())
        self.advice = advice


OVER = "Too bright even at the shortest exposure: close the aperture or lower the ISO."
UNDER = "Too dark even at the longest exposure: open the aperture or raise the ISO."
OWN_ADVICE = {  # an own light's: it is the operator's to turn up or down
    "over": "Too bright even at the shortest exposure: turn the light down or close the aperture.",
    "under": "Too dark even at the longest exposure: turn the light up or open the aperture.",
}
FLAT_TOO_BRIGHT = "Close the aperture or dim the light, then calibrate again, or start the session without a flat field."


class WrongColour(CalibrationError):
    """A shot the operator was prompted to light in one colour alone (own-rgb) that was not: refused, nothing kept from it."""


@dataclass(frozen=True)
class Roi:
    """A rectangle in fractions of the frame, so it holds at any resolution."""

    x: float
    y: float
    w: float
    h: float

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        x0 = min(max(int(round(self.x * width)), 0), width - 1)
        y0 = min(max(int(round(self.y * height)), 0), height - 1)
        x1 = min(max(int(round((self.x + self.w) * width)), x0 + 1), width)
        y1 = min(max(int(round((self.y + self.h) * height)), y0 + 1), height)
        return x0, y0, x1, y1


@dataclass(frozen=True)
class Channel:
    """One exposure of a frame, as calibration shoots it."""

    name: str  # the exposure's file letter (kinds.EXPOSURES)
    led: Optional[int]  # the LED (0 r, 1 g, 2 b, 3 w) whose level is calibrated; None: the light stays at `levels`, only the shutter moves
    sensor: Optional[int]  # sensor channel to meter; None = whichever is brightest
    levels: tuple[int, int, int, int] = (0, 0, 0, 0)  # a light that stays as it is
    alone: bool = False  # the operator lights this colour alone (own-rgb): every shot is checked for it

    def light(self, level: int) -> tuple[int, int, int, int]:
        """LED level -> (r, g, b, w)."""
        if self.led is None:
            return self.levels
        values = [0, 0, 0, 0]
        values[self.led] = level
        return values[0], values[1], values[2], values[3]

    def nominal(self, levels: tuple[int, int, int, int]) -> int:
        """This exposure's level in a preset's levels, as level x seconds counts it."""
        return FIXED_LEVEL if self.led is None else levels[self.led]


def channels(kind: str, levels: tuple[int, int, int, int] = (0, 0, 0, 0)) -> tuple[Channel, ...]:
    """A kind's exposures (kinds.py) as calibration shoots them; `levels` lights a kind whose light is not calibrated."""
    entry, found = KINDS[kind], []
    for letter in entry.exposures:
        exposure = EXPOSURES[letter]
        fixed = entry.fixed or len(exposure.leds) != 1
        sensor = exposure.sensors[0] if len(exposure.sensors) == 1 else None
        found.append(Channel(letter, None if fixed else exposure.leds[0], sensor, exposure.light(levels) if fixed else (0, 0, 0, 0), entry.prompted))
    return tuple(found)


RGB = channels("rgb")
WHITE = channels("white")


@dataclass(frozen=True)
class Reading:
    signal: float  # 99.9th percentile of the metered channel inside the rectangle
    base: tuple[float, float, float]  # typical film base value per channel
    clipped: float  # share of the rectangle at the sensor ceiling


@dataclass(frozen=True)
class Histogram:
    """A calibration shot as the window shows it: pixels per bin from HISTOGRAM_STOPS below the
    sensor's ceiling up to it, for the sensor channels the shot is meant for (its own colour's,
    or all three under white light), and where the film base landed in each."""

    counts: dict[int, np.ndarray]  # sensor channel (0 R, 1 G, 2 B) -> pixels per bin, darkest first
    base: dict[int, float]  # sensor channel -> the film base, in stops below the ceiling (0 or less)
    clipped: float  # share of the base rectangle at the ceiling, in the metered channel


@dataclass(frozen=True)
class Step:
    """How far a calibration has come: a shot taken (with its histogram), or the shutter solved."""

    stage: str  # "probe", "trim" or (a light that stays as it is) "check" for a shot; "shutter" once one shutter is found for all colours
    colour: str = ""  # the exposure's letter: "R", "G", "B", "W" or "RGB"
    shutter: str = ""
    level: int = 0  # 0 for a light that stays as it is
    histogram: Optional[Histogram] = None


def histogram(image: np.ndarray, channels: tuple[int, ...], base: tuple[float, float, float], clipped: float) -> Histogram:
    """The histogram of a decoded shot's whole frame, in stops below the ceiling."""
    edges = np.linspace(-HISTOGRAM_STOPS, 0.0, HISTOGRAM_BINS + 1)
    sample = image[::HISTOGRAM_STRIDE, ::HISTOGRAM_STRIDE]
    counts = {}
    for channel in channels:
        stops = np.log2(np.maximum(sample[..., channel].astype(np.float32), 1.0) / FULL)
        counts[channel] = np.histogram(np.minimum(stops, 0.0), bins=edges)[0]  # the ceiling in the last bin; darker than the range left out
    return Histogram(counts, {channel: min(0.0, math.log2(max(base[channel], 1.0) / FULL)) for channel in channels}, clipped)


@dataclass(frozen=True)
class Calibration:
    kind: str  # kinds.KINDS
    levels: tuple[int, int, int, int]  # r, g, b, w
    shutter: str
    base: tuple[float, float, float]  # mean base signal per sensor channel, 0..65535
    signals: dict[str, float]  # metered 99.9th percentile per channel


# ----- shutter ladder -----


def shutter_seconds(label: str) -> float:
    """'1/100', '0.4', '4/10' or '0.4000s' as seconds. Raises ValueError on 'Bulb' and the like."""
    text = re.sub(r"(?i)\s*(sec(onds?)?|s|\")$", "", label.strip())
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        if float(denominator) == 0:
            raise ValueError(label)
        return float(numerator) / float(denominator)
    return float(text)


def usable_ladder(labels: list[str], fastest: float = FASTEST_S) -> list[str]:
    """The body's shutter labels inside the usable range, fastest first."""
    parsed = {}
    for label in labels:
        try:
            value = shutter_seconds(label)
        except ValueError:
            continue
        if fastest * 0.99 <= value <= SLOWEST_S * 1.01:
            parsed[label] = value
    return sorted(parsed, key=parsed.get)


def true_seconds(label: str) -> float:
    """The exposure a shutter label stands for. Labels are rounded names on a third-stop
    ladder: '1/3' is 2^(-5/3) = 0.315 s, not 0.333 s. The difference reaches 7 %."""
    nominal = shutter_seconds(label)
    exact = 2.0 ** (round(math.log2(nominal) * 3) / 3)
    return exact if abs(exact / nominal - 1) <= 0.08 else nominal


# ----- the calibrator -----


class Calibrator:
    def __init__(
        self,
        light,
        camera,
        scratch: Path,
        settle_s: float = 0.4,
        log: Callable[[str], None] = lambda message: None,
        cancelled: Callable[[], bool] = lambda: False,
        progress: Callable[[Step], None] = lambda step: None,
    ) -> None:
        self._light, self._camera = light, camera
        self._scratch = Path(scratch)
        self._settle_s = settle_s
        self._log, self._cancelled, self._progress = log, cancelled, progress
        self._stage = "probe"  # what the shots are for, as each Step reports it
        self._ladder: list[str] = []
        self._last_image: Optional[np.ndarray] = None
        self.stated: dict[str, str] = {}  # settings the camera cannot report, as the operator gave them
        self.shots = 0
        self._own = False  # an own light: its advice is to turn the light up or down

    def _advice(self, which: str) -> str:
        return OWN_ADVICE[which] if self._own else OVER if which == "over" else UNDER

    def _what(self, channel: Channel) -> str:
        """How an error names the exposure: 'Red only:' for a colour the operator lit alone."""
        if channel.alone:
            return f"{EXPOSURES[channel.name].name.capitalize()} only:"
        return f"{channel.name} channel:" if channel.led is not None else f"{channel.name} exposure:"

    def run(self, roi: Roi, kind: str = "rgb", target: float = TARGET) -> Calibration:
        self._ladder = usable_ladder(self._camera.choices("shutter"))
        if not self._ladder:
            raise CalibrationError("The camera offers no settable shutter speeds.", "Put it in manual mode (M).")
        goal = target * FULL
        self._own = not KINDS[kind].scanlight
        if KINDS[kind].fixed:  # the light's colour default, or the operator's light as it is: the shutter alone
            levels = self._colour_default() if KINDS[kind].from_light else (0, 0, 0, 0)
            (channel,) = channels(kind, levels)
            try:
                shutter, reading = self._shutter_only(channel, roi, goal)
            finally:
                self._light.off()
            return Calibration(kind, levels, shutter, reading.base, {channel.name: reading.signal})
        exposures = channels(kind)
        try:
            scale = self._start_scale()
            needed = {}  # level * seconds that puts each channel on target
            self._stage = "probe"
            for channel in exposures:
                k = self._probe(channel, roi, START_EXPOSURE[channel.name] * scale)
                needed[channel.name] = goal / k
                self._log(f"{channel.name}: needs level x seconds = {needed[channel.name]:.1f}")

            shutter = self._shutter_for(max(needed.values()) / LEVEL_SOLVE)
            seconds = true_seconds(shutter)
            if max(needed.values()) / seconds > LEVEL_MAX / (1 - 2 * TOLERANCE):
                raise CalibrationError("Calibration failed.", self._advice("under"))
            self._progress(Step("shutter", shutter=shutter))
            self._stage = "trim"
            levels, signals, base = {}, {}, [0.0, 0.0, 0.0]
            for channel in exposures:
                start = int(np.clip(round(needed[channel.name] / seconds), LEVEL_MIN, LEVEL_MAX))
                level, reading = self._settle(channel, roi, start, shutter, goal)
                levels[channel.name], signals[channel.name] = level, reading.signal
                if channel.sensor is None:
                    base = list(reading.base)
                else:
                    base[channel.sensor] = reading.base[channel.sensor]
                self._log(f"{channel.name}: level {level}, base at {reading.signal / FULL:.1%}")
        finally:
            self._light.off()
        packed = [0, 0, 0, 0]
        for channel in exposures:
            packed[channel.led] = levels[channel.name]
        return Calibration(kind, (packed[0], packed[1], packed[2], packed[3]), shutter, (base[0], base[1], base[2]), signals)

    def calibrate_colour(self, roi: Roi, kind: str, letter: str, target: float = TARGET) -> tuple[str, Reading]:
        """One colour of a prompted kind (own-rgb), the operator's light set to it alone at full:
        its own shutter, the film base in its channel 0 to 1/3 stop under the target. A shot of
        another colour is refused (WrongColour)."""
        self._ladder = usable_ladder(self._camera.choices("shutter"))
        if not self._ladder:
            raise CalibrationError("The camera offers no settable shutter speeds.", "Put it in manual mode (M).")
        self._own = not KINDS[kind].scanlight
        (channel,) = [channel for channel in channels(kind) if channel.name == letter]
        return self._shutter_only(channel, roi, target * FULL)

    def _colour_default(self) -> tuple[int, int, int, int]:
        """The light's stored colour default as levels: what the buttons light, set in its web app."""
        r, g, b = self._light.default_rgb()
        if not (r or g or b):  # the firmware replaces an empty default, so this should not happen
            raise CalibrationError("The light's colour default has every LED off.", "Set a colour default in the Scanlight web app.")
        self._log(f"the light's colour default: R {r} G {g} B {b}")
        if (r, g, b) == FACTORY_DEFAULT:
            self._log(FACTORY_NOTE)
        return r, g, b, 0

    def _shutter_only(self, channel: Channel, roi: Roi, goal: float) -> tuple[str, Reading]:
        """The shutter for a light that stays as it is: the slowest one at which the brightest
        channel's film base is predicted at or under the target (within its tolerance), then a
        rung faster for as long as anything clips. On a third-stop ladder the film base lands 0 to
        1/3 stop under the target. Returns the shutter and the reading of the last shot."""
        self._stage = "probe"
        what = self._what(channel)
        lit = [START_EXPOSURE["RGBW"[led]] / level for led, level in enumerate(channel.levels) if level]
        guess = min(lit) if lit else OWN_GUESS_S
        k = self._probe(channel, roi, guess * self._start_scale() * FIXED_LEVEL)  # signal per level x second

        def slowest_fitting() -> int:
            fits = [index for index, label in enumerate(self._ladder) if k * FIXED_LEVEL * true_seconds(label) <= goal * (1 + TOLERANCE)]
            if not fits:
                raise CalibrationError(what, self._advice("over"))
            return fits[-1]  # the ladder runs fastest first

        at, backed_off = slowest_fitting(), False
        self._stage = "check"  # shots at the shutter found, until it holds
        reading = self._shoot(channel, roi, FIXED_LEVEL, self._ladder[at])
        for _ in range(len(self._ladder)):
            if reading.clipped > CLIP_BUDGET:
                if at == 0:
                    raise CalibrationError(what, self._advice("over"))
                at, backed_off = at - 1, True
            elif not backed_off:
                # Nothing clipped: measure again from this shot, which is far better exposed than a
                # dim probe (whose 99.9th percentile is mostly noise: an own light lands near 70 %).
                k = reading.signal / (FIXED_LEVEL * true_seconds(self._ladder[at]))
                if slowest_fitting() <= at:
                    break  # this rung is the slowest that fits, or the next is a gap in the camera's ladder
                at = slowest_fitting()
            else:
                break
            reading = self._shoot(channel, roi, FIXED_LEVEL, self._ladder[at])
        if reading.signal < goal * 2 ** (-1 / 3) * (1 - TOLERANCE):
            if backed_off:
                raise CalibrationError(
                    f"{what} part of the rectangle clips while the rest is under target.", "Move the rectangle to an evenly lit, clean patch of film base."
                )
            if at == len(self._ladder) - 1:
                raise CalibrationError(what, self._advice("under"))
        shutter = self._ladder[at]
        self._progress(Step("shutter", shutter=shutter))
        self._log(f"{what} shutter {shutter}, film base at {reading.signal / FULL:.1%} in the brightest channel")
        return shutter, reading

    def bare_light(
        self, kind: str, start: dict[str, float], levels: tuple[int, int, int, int] = (0, 0, 0, 0), target: float = FLAT_TARGET,
        letters: Optional[tuple[str, ...]] = None,
    ) -> tuple[list[np.ndarray], dict[str, float]]:
        """Photograph the uncovered light for a flat field. Each colour gets its own exposure
        (bare light is far brighter than film, and by a different factor per colour), found
        from `start`, a first guess in level x seconds.

        A flat field only records where the light is brighter and where dimmer, so any clean
        exposure will do: the first shot that is well exposed and unclipped is kept, with no
        trimming onto a target. A light that is not calibrated (`levels`, rgb-together) stays
        at its levels; only its shutter moves. Returns the planes R, G, B (or one RGB image for
        a single exposure) and the exposure that worked for each, to start from next time.

        An own light is shot down to FLAT_FASTEST_S, not FASTEST_S; `letters` shoots only
        some colours (own-rgb: one per prompt, the operator having set the light to it)."""
        self._own = not KINDS[kind].scanlight
        self._ladder = usable_ladder(self._camera.choices("shutter"), FLAT_FASTEST_S if self._own else FASTEST_S)
        if not self._ladder:
            raise CalibrationError("The camera offers no settable shutter speeds.", "Put it in manual mode (M).")
        planes, worked = [], {}
        self._stage = "bare"
        try:
            for channel in channels(kind, levels):
                if letters is not None and channel.name not in letters:
                    continue
                worked[channel.name] = self._bare_shot(channel, start[channel.name], target * FULL)
                planes.append(self._last_image if channel.sensor is None else self._last_image[..., channel.sensor].copy())
        finally:
            self._light.off()
        return planes, worked

    def _bare_shot(self, channel: Channel, exposure: float, goal: float) -> float:
        """Expose the bare light until one shot is usable; returns its level x seconds."""
        whole = Roi(0.0, 0.0, 1.0, 1.0)
        lowest, highest = self._reach(channel)
        for _ in range(8):
            level, shutter = self._split(float(np.clip(exposure, lowest, highest)), channel)
            reading = self._shoot(channel, whole, level, shutter)
            actual = level * true_seconds(shutter)
            clipped = reading.clipped > CLIP_BUDGET or reading.signal >= PROBE_MAX * FULL
            if not clipped and FLAT_MIN * FULL <= reading.signal <= FLAT_MAX * FULL:
                self._log(f"{channel.name}: bare light at level {level}, {shutter} s, {reading.signal / FULL:.0%} of full scale")
                return actual
            if clipped or reading.signal > goal:
                if actual <= lowest * 1.01:
                    raise CalibrationError(
                        f"{self._what(channel)} the bare light is too bright even at the shortest exposure.",
                        FLAT_TOO_BRIGHT if self._own else "Close the aperture or lower the ISO.",
                    )
                # A clipped reading says nothing about how far over it is; an unclipped one is exact.
                exposure = actual / 4 if clipped else actual * goal / reading.signal
            else:
                if actual >= highest * 0.99:
                    raise CalibrationError(
                        f"No usable light reached the sensor on the {channel.name} channel.",
                        "Check that the light is on and nothing covers the holder.",
                    )
                exposure = actual * min(16.0, goal / max(reading.signal, 0.002 * FULL))
        raise CalibrationError(f"The {channel.name} channel did not settle.", "Is the light stable?")

    # ----- steps -----

    def _start_scale(self) -> float:
        """Scale the starting guess from ISO 100, f/8 to what the camera is set to."""
        scale = 1.0
        iso = re.sub(r"[^0-9.]", "", self._camera.get("iso") or self.stated.get("iso", ""))
        if iso and float(iso) > 0:
            scale *= 100.0 / float(iso)
        aperture = re.search(r"[0-9]+(\.[0-9]+)?", self._camera.get("aperture") or self.stated.get("aperture", ""))
        if aperture and float(aperture.group()) > 0:
            scale *= (float(aperture.group()) / 8.0) ** 2
        return scale

    def _shutter_for(self, seconds: float) -> str:
        """The fastest shutter at least `seconds` long, or the slowest one there is."""
        for label in self._ladder:
            if true_seconds(label) >= seconds:
                return label
        return self._ladder[-1]

    def _reach(self, channel: Channel) -> tuple[float, float]:
        """The shortest and the longest exposure there is, in level x seconds."""
        if channel.led is None:
            return FIXED_LEVEL * true_seconds(self._ladder[0]), FIXED_LEVEL * true_seconds(self._ladder[-1])
        return LEVEL_MIN * true_seconds(self._ladder[0]), LEVEL_MAX * true_seconds(self._ladder[-1])

    def _split(self, exposure: float, channel: Channel) -> tuple[int, str]:
        """Turn level x seconds into an LED level and a shutter, clamped to what exists. A light
        that stays as it is gets the shutter nearest below the exposure, or the fastest."""
        if channel.led is None:
            fitting = [label for label in self._ladder if FIXED_LEVEL * true_seconds(label) <= exposure * 1.01]
            return FIXED_LEVEL, fitting[-1] if fitting else self._ladder[0]
        shutter = self._shutter_for(exposure / LEVEL_SOLVE)
        level = int(np.clip(round(exposure / true_seconds(shutter)), LEVEL_MIN, LEVEL_MAX))
        return level, shutter

    def _shoot(self, channel: Channel, roi: Roi, level: int, shutter: str) -> Reading:
        if self._cancelled():
            raise CalibrationError("Calibration cancelled.")
        self._light.set(*channel.light(level))
        time.sleep(self._settle_s)
        if not self._camera.set("shutter", shutter):
            raise CalibrationError(f"The camera did not accept shutter {shutter}.", "Put it in manual mode (M).")
        path = self._camera.capture(self._scratch / "calibration")
        self.shots += 1
        try:
            image = decode_linear(path, half=True)
        except RawError as exc:
            # The scratch folder is deleted afterwards; keep the one file that explains the failure.
            kept = home() / "failed" / f"{time.strftime('%Y%m%d-%H%M%S')}-{path.name}"
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, kept)
            raise CalibrationError(str(exc), f"The file was kept at {kept}.") from exc
        self._last_image = image
        x0, y0, x1, y1 = roi.pixels(image.shape[1], image.shape[0])
        patch = image[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
        peaks = np.percentile(patch, 99.9, axis=0)
        if channel.alone and int(np.argmax(np.median(patch, axis=0))) != channel.sensor:
            raise WrongColour(f"That did not look like {EXPOSURES[channel.name].name} only.", f"Set your light to {EXPOSURES[channel.name].name} only, then shoot again.")
        metered = int(np.argmax(peaks)) if channel.sensor is None else channel.sensor
        clipped = max(float(np.mean(patch[:, metered] >= 0.998 * FULL)), clipped_fraction(path, channel.sensor, roi))
        # The base is the brightest thing in the rectangle. A rectangle drawn a little too wide
        # also covers picture or holder, so only the pixels near the top count: their median is
        # the base, whatever else got in.
        on_base = patch[patch[:, metered] >= BASE_SHARE * peaks[metered]]
        base = np.median(on_base, axis=0) if len(on_base) else peaks
        reading = Reading(float(peaks[metered]), (float(base[0]), float(base[1]), float(base[2])), clipped)
        shown = (0, 1, 2) if channel.sensor is None else (channel.sensor,)
        reported = 0 if channel.led is None else level
        self._progress(Step(self._stage, channel.name, shutter, reported, histogram(image, shown, reading.base, clipped)))
        return reading

    def _probe(self, channel: Channel, roi: Roi, exposure: float) -> float:
        """Find an exposure that reads cleanly and return k = signal / (level x seconds)."""
        lowest, highest = self._reach(channel)
        for _ in range(10):
            exposure = float(np.clip(exposure, lowest, highest))
            level, shutter = self._split(exposure, channel)
            reading = self._shoot(channel, roi, level, shutter)
            actual = level * true_seconds(shutter)
            if reading.clipped > CLIP_BUDGET or reading.signal >= PROBE_MAX * FULL:
                if actual <= lowest * 1.01:
                    raise CalibrationError(self._what(channel), self._advice("over"))
                exposure = actual / 4
            elif reading.signal < PROBE_MIN * FULL:
                if actual >= highest * 0.99:
                    if reading.signal < 0.002 * FULL:
                        raise CalibrationError(
                            f"No light reached the sensor on the {channel.name} channel.",
                            "Check that the rectangle is on the clear film base and the light is on.",
                        )
                    raise CalibrationError(self._what(channel), self._advice("under"))
                exposure = actual * min(16.0, 0.5 * FULL / max(reading.signal, 0.002 * FULL))
            else:
                return reading.signal / actual
        raise CalibrationError(f"The {channel.name} channel did not settle.", "Is the light stable and the film still?")

    def _settle(self, channel: Channel, roi: Roi, level: int, shutter: str, goal: float) -> tuple[int, Reading]:
        """Shoot at the solved level, trim it onto the target, and back off if anything clips."""
        reading = self._shoot(channel, roi, level, shutter)
        for _ in range(12):
            if reading.clipped > CLIP_BUDGET:
                wanted = int(round(level * 0.9))
            elif abs(reading.signal - goal) > TOLERANCE * goal:
                wanted = int(round(level * goal / max(reading.signal, 1.0)))
            else:
                break
            wanted = int(np.clip(wanted, LEVEL_MIN, LEVEL_MAX))
            if wanted == level:
                break
            level = wanted
            reading = self._shoot(channel, roi, level, shutter)
        if reading.clipped > CLIP_BUDGET:
            raise CalibrationError(f"{channel.name} channel:", self._advice("over"))
        if reading.signal < (1 - 2 * TOLERANCE) * goal:
            if level >= LEVEL_MAX:
                raise CalibrationError(f"{channel.name} channel:", self._advice("under"))
            raise CalibrationError(
                f"{channel.name} channel: part of the rectangle clips while the rest is under target.",
                "Move the rectangle to an evenly lit, clean patch of film base.",
            )
        if reading.signal > (1 + 2 * TOLERANCE) * goal:
            raise CalibrationError(
                f"{channel.name} channel is too strong for the shutter the weakest channel needs.",
                "Close the aperture or lower the ISO so the shared shutter gets longer.",
            )
        return level, reading
