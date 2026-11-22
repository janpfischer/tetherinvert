"""The calibration wizard's rules: its steps, the ways in, what each step needs before
Next, what the viewer shows, and what W4 says while a calibration runs and once it is over.

No Qt, like corrections.py: the window keeps a Wizard, tells it what it cannot know itself
(whether the camera is connected, which settings it does not report, the film base rectangle
drawn), passes on the operator's answers, and shows it (wizardpanel.py). The tests drive it
without a window.

A wizard is a list of steps, and each way in is its own list: a new preset starts at Film (W1),
Recalibrate… at Camera (W2) with the preset's name and film type fixed. A prompted kind (own-rgb)
gets a step per colour before Calibrate, once W1 has chosen it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from . import kinds
from .calibrate import BRIGHT_NOTE, BRIGHT_S, FACTORY_DEFAULT, FACTORY_NOTE, TARGET, Histogram, Step as Shot, true_seconds
from .raw import FULL

FILM, CAMERA, FILM_BASE, CALIBRATE = "film", "camera", "film base", "calibrate"
EDITING, RUNNING, FAILED, DONE = "editing", "running", "failed", "done"  # the calibration: not started, running, failed or stopped, saved


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    base_tool: bool = False  # the film base rectangle is shown and drawn in this step, and only in it
    shoot: bool = False  # a prompted colour (own-rgb): the operator sets the light to it, then Shoot


STEPS = {
    FILM: Step(FILM, "Film"),
    CAMERA: Step(CAMERA, "Camera"),
    FILM_BASE: Step(FILM_BASE, "Film base", base_tool=True),
    CALIBRATE: Step(CALIBRATE, "Calibrate"),
}
# own-rgb's prompted steps between the film base and Calibrate, one per colour, keyed by its letter.
COLOUR_STEPS = {letter: Step(letter, f"{kinds.EXPOSURES[letter].name.capitalize()} only", shoot=True) for letter in "RGB"}


# A preset's kind is fixed when it is calibrated; W1 offers kinds.KINDS and W4 follows it.
# The settings a camera may not report, which W2 asks for: what it asks, an example, and what to
# call it when it is missing. A manual lens reports no f-stop, so W2 asks for it.
QUESTIONS = {
    "iso": ("Which ISO is the camera set to?", "100", "the ISO"),
    "aperture": ("Which f-stop is set on the lens?", "f/8", "the f-stop"),
}
CHANNELS = "RGB"  # sensor channel 0, 1, 2


@dataclass
class Wizard:
    steps: list[Step]
    name: str = ""
    kind: str = "rgb"
    fixed: bool = False  # Recalibrate…: name and film type are the preset's
    values: dict[str, str] = field(default_factory=dict)  # the by-hand settings as entered in W2, or as filled in
    rect: Optional[tuple[float, float, float, float]] = None  # the film base rectangle
    at: int = 0
    state: str = EDITING
    stopping: bool = False  # Stop was pressed; the calibration ends at its next shot
    problem: str = ""  # why the calibration failed, or that it was stopped
    saved: str = ""  # the preset's name, once saved
    shots: list[Shot] = field(default_factory=list)  # this calibration's progress, as the hardware thread reported it
    colour_shots: dict[str, dict] = field(default_factory=dict)  # own-rgb: each colour's ColourShot (as a dict), once kept
    shooting: str = ""  # the colour being shot now (own-rgb)
    shot_problem: str = ""  # why the last colour shot was refused
    # What the window tells it:
    camera: bool = False  # a camera is connected and has reported its settings
    unknown: tuple[str, ...] = ()  # of QUESTIONS, those the camera does not report
    scanlight: bool = True  # a Scanlight is connected: without one, W1 offers only an own light

    # ----- the ways in -----

    @classmethod
    def new_preset(cls, newest=None, rect=None) -> Wizard:
        """New preset…: from W1. The by-hand settings start from the preset calibrated last
        (the lens as it was last set at the stand), the rectangle from the one drawn last."""
        return cls([STEPS[key] for key in (FILM, CAMERA, FILM_BASE, CALIBRATE)], values=filled_in(newest), rect=rect)

    @classmethod
    def recalibrate(cls, preset) -> Wizard:
        """Recalibrate… on a chosen preset: from W2, its name and film type fixed, its own by-hand
        settings and rectangle."""
        made = cls(
            [STEPS[key] for key in (CAMERA, FILM_BASE, CALIBRATE)], name=preset.name, kind=preset.kind, fixed=True,
            values=filled_in(preset), rect=tuple(preset.roi),
        )  # fmt: skip
        made._shape()
        return made

    # ----- growing it -----

    def insert(self, before: str, step: Step) -> None:
        """A step before another."""
        self.steps.insert(self.index(before), step)

    def append(self, step: Step) -> None:
        """A step after the last."""
        self.steps.append(step)

    def index(self, key: str) -> int:
        return [step.key for step in self.steps].index(key)

    def _shape(self) -> None:
        """The colour steps there for a prompted kind (own-rgb), and only for one: the kind is chosen in W1."""
        plain = [step for step in self.steps if not step.shoot]
        if kinds.KINDS[self.kind].prompted:
            at = [step.key for step in plain].index(CALIBRATE)
            plain[at:at] = [COLOUR_STEPS[letter] for letter in kinds.KINDS[self.kind].exposures]
        self.steps = plain

    @property
    def light_note(self) -> str:
        """An own light's description and setting, as noted in W2: stored in the preset."""
        return self.values.get("light", "").strip()

    # ----- where it is -----

    @property
    def step(self) -> Step:
        return self.steps[self.at]

    def heading(self) -> str:
        """e.g. 'New preset · Gold 200 · colour', 'Recalibrate · hp5 · black and white'."""
        parts = ["Recalibrate" if self.fixed else "New preset"]
        if self.name.strip() and self.step.key != FILM:
            parts += [self.name.strip(), kinds.KINDS[self.kind].short]
        return " · ".join(parts)

    def position(self) -> str:
        """e.g. 'Step 2 of 4 · Camera'."""
        return f"Step {self.at + 1} of {len(self.steps)} · {self.step.title}"

    def by_hand(self) -> dict[str, str]:
        """The settings the camera does not report, as stated: they go into the preset."""
        return {key: self.values.get(key, "").strip() for key in self.unknown}

    # ----- moving -----

    def missing(self) -> str:
        """What keeps Next from going on in this step, said so the operator knows what to do; ''
        when nothing does."""
        key = self.step.key
        if key == FILM and not self.name.strip():
            return "Name the film."
        if key == FILM and kinds.KINDS[self.kind].scanlight and not self.scanlight:
            return "No Scanlight is connected: connect its data port (the left USB-C), or choose Own light."
        if key == CAMERA and not kinds.KINDS[self.kind].scanlight and self.camera and not self.light_note:
            return "Note which light it is and how it is set."
        if key == CAMERA:
            if not self.camera:
                return "The camera is not connected: connect it and switch it on."
            empty = [key for key in self.unknown if not self.values.get(key, "").strip()]
            if empty:
                return f"Fill in {' and '.join(QUESTIONS[key][2] for key in empty)}."
        if key == FILM_BASE and self.rect is None:
            return "Draw the film base rectangle on clear rebate in the live view."
        return ""

    def next_label(self) -> str:
        """The forward button's label, '' where there is none (Calibrate running or failed)."""
        if self.step.key == CALIBRATE:
            return "Finish" if self.state == DONE else ""
        if self.step.shoot:
            return "Shoot"
        return "Calibrate" if self.steps[self.at + 1].key == CALIBRATE else "Next"

    def next(self) -> bool:
        """On to the next step; into Calibrate, that starts the calibration. Refused while
        something is missing. Returns whether it moved."""
        if self.state != EDITING or self.step.shoot or self.missing() or self.at + 1 >= len(self.steps):
            return False
        if self.step.key == FILM:
            self._shape()
        self.at += 1
        if self.step.key == CALIBRATE:
            self.state, self.stopping, self.problem, self.shots = RUNNING, False, "", []
        return True

    def can_back(self) -> bool:
        return self.at > 0 and self.state in (EDITING, FAILED) and not self.shooting

    def back(self) -> bool:
        """One step back, keeping what was entered; from a failed or stopped calibration, back to
        the rectangle to try again."""
        if not self.can_back():
            return False
        self.at -= 1
        self.state, self.problem = EDITING, ""
        return True

    def can_cancel(self) -> bool:
        """Cancel saves nothing, so it is there until the calibration runs; afterwards the preset is saved."""
        return self.state in (EDITING, FAILED) and not self.shooting

    # ----- own-rgb's colour steps -----

    def shoot(self) -> Optional[str]:
        """Shoot in a colour step: the colour to shoot, or None when it cannot (not a colour step, or a shot under way)."""
        if self.state != EDITING or not self.step.shoot or self.shooting:
            return None
        self.shooting, self.shot_problem = self.step.key, ""
        return self.shooting

    def shot(self, letter: str, shot: Optional[dict], problem: str) -> bool:
        """A colour's shot came back: kept (on to the next step; after the last, Calibrate runs),
        or refused with `problem` (a wrong colour) and asked for again. Returns whether it moved on."""
        if letter != self.shooting:
            return False
        self.shooting = ""
        if problem or shot is None:
            self.shot_problem = problem or "The shot did not work."
            return False
        self.colour_shots[letter], self.shot_problem = shot, ""
        self.at += 1
        if self.step.key == CALIBRATE:
            self.state, self.stopping, self.problem = RUNNING, False, ""  # the colour shots' histograms stay for W4
        return True

    # ----- the calibration (W4) -----

    def record(self, shot: Shot) -> None:
        if self.state == RUNNING or self.shooting:
            self.shots.append(shot)

    def failed(self, problem: str) -> None:
        if self.state == RUNNING:
            self.state, self.problem, self.stopping = FAILED, problem, False

    def done(self, name: str) -> None:
        if self.state == RUNNING:
            self.state, self.saved, self.stopping = DONE, name, False

    def stages(self) -> list[tuple[str, str]]:
        """W4's list of stages: (text, 'done', 'now' or 'todo'). A kind whose levels come from the
        light has no trimming: the shutter is all there is to find."""
        reached = [shot.stage for shot in self.shots]
        shutter = next((shot.shutter for shot in self.shots if shot.stage == "shutter"), "")
        kind = kinds.KINDS[self.kind]
        if kind.prompted:  # own-rgb: shot at the colour steps; Calibrate only saves
            done = [letter for letter in kind.exposures if letter in self.colour_shots]
            shutters = " · ".join(f"{letter} {self.colour_shots[letter]['shutter']}" for letter in done)
            rows = [
                ("Shoot red, green and blue, each by hand", "done" if len(done) == len(kind.exposures) else "now" if done or self.shooting else "todo"),
                (f"A shutter per colour: {shutters} s" if done else "A shutter per colour", "done" if len(done) == len(kind.exposures) else "todo"),
                (f"Saved as {self.saved}" if self.saved else "Save the preset", "done" if self.saved else "todo"),
            ]  # fmt: skip
            return rows if self.state != FAILED else [(text, "todo" if state == "now" else state) for text, state in rows]
        several = not kind.single
        trimmed = "trim" in reached
        rows = [
            ("Probe red, green, blue" if several else f"Probe {kind.colours}", "done" if shutter else "now" if reached else "todo"),
            (f"One shutter for all colours: {shutter} s" if shutter and several else f"Shutter: {shutter} s" if shutter else "Find the shutter",
             "done" if shutter else "todo"),
            (f"Trim {kind.colours} onto {TARGET:.0%}", "done" if self.saved else "now" if trimmed else "todo"),
            (f"Saved as {self.saved}" if self.saved else "Save the preset", "done" if self.saved else "todo"),
        ]  # fmt: skip
        if kind.from_light:
            del rows[2]
        if self.state == FAILED:  # the stage it stopped in is not under way any more
            rows = [(text, "todo" if state == "now" else state) for text, state in rows]
        return rows

    def histogram(self) -> tuple[dict[int, Histogram], str]:
        """What W4's histogram shows: each sensor channel from the last shot meant for it (red
        from the red shot, and so on; all three from a white shot), and a caption for the last shot."""
        latest: dict[int, Histogram] = {}
        caption = ""
        for shot in self.shots:
            if shot.histogram is None:
                continue
            for channel in shot.histogram.counts:
                latest[channel] = shot.histogram
            stage = shot.stage if shot.stage in ("probe", "check") else "trim"
            level = f", level {shot.level}" if shot.level else ""  # a light that stays as it is has none of its own
            caption = f"Last shot: {colour_name(shot.colour)} {stage}{level}, {shot.shutter} s"
        return latest, caption

    def clipping(self) -> str:
        """W4's clipping line, from each colour's last shot: those were kept."""
        last = {shot.colour: shot.histogram.clipped for shot in self.shots if shot.histogram is not None}
        worst = max(last, key=last.get, default=None)
        if worst is None or last[worst] <= 0:
            return "Nothing clipped."
        return f"{last[worst]:.2%} of the film base clipped in {colour_name(worst)}."


def colour_name(letter: str) -> str:
    """An exposure's letter as W4 says it: 'red', 'white', 'colour default'."""
    return kinds.EXPOSURES[letter].name if letter in kinds.EXPOSURES else letter


def filled_in(preset) -> dict[str, str]:
    """W2's fields from a preset: what it recorded for the settings the camera may not report, and its own light's note."""
    if preset is None:
        return {}
    return {key: value for key, value in (("iso", preset.iso), ("aperture", preset.aperture), ("light", preset.light_note)) if value}


def replace_note(name: str, kind: str, existing) -> str:
    """W1's note when the name is taken (compared without case): what calibrating replaces."""
    if existing is None:
        return ""
    other = kinds.KINDS.get(existing.kind)
    change = " Calibrating replaces it."
    if other and existing.kind != kind:
        change = f" It is for {other.film.lower()}; calibrating replaces it with a {kinds.KINDS[kind].short} preset."
    return f"A preset named “{existing.name}” exists.{change} Rolls scanned with it keep their own copy."


def result(preset) -> list[tuple[str, str]]:
    """W4's result: (label, value) per line, from the saved preset."""
    by_hand = lambda key, value: f"{value} by hand" if key in preset.by_hand else value  # noqa: E731
    exposure = [f"{preset.shutter} s"]
    exposure += [by_hand("iso", f"ISO {preset.iso}")] if preset.iso else []
    exposure += [by_hand("aperture", preset.aperture)] if preset.aperture else ["f-stop not recorded"]
    r, g, b, w = preset.levels
    kind = kinds.KINDS[preset.kind]
    if preset.shutters:  # own-rgb: a shutter per colour
        exposure[0] = " · ".join(f"{letter} {preset.shutters[letter]}" for letter in kind.exposures) + " s"
    base = " · ".join(f"{name} {value / FULL:.0%}" for name, value in zip(CHANNELS, preset.base, strict=True))
    if kind.scanlight:
        leds = f"R {r} · G {g} · B {b}" if kind.colour else f"White {w}"
        rows = [("Exposure", " · ".join(exposure)), ("LEDs", leds + (" from the light" if kind.from_light else "")), ("Film base", base)]
    else:
        rows = [("Exposure", " · ".join(exposure)), ("Light", preset.light_note or "not recorded"), ("Film base", base)]
    if kind.from_light and (r, g, b) == FACTORY_DEFAULT:
        rows.append(("Note", FACTORY_NOTE))
    fastest = min(true_seconds(shutter) for shutter in (preset.shutters or {"": preset.shutter}).values())
    if not kind.scanlight and fastest <= BRIGHT_S * 1.01:  # on calibration's fastest rung: the flat field may find no shutter short enough
        rows.append(("Note", BRIGHT_NOTE))
    return rows


def stops(share: float) -> float:
    """A share of full scale in stops below the ceiling, as the histogram's x axis runs."""
    return math.log2(max(share, 1e-9))


def scaled(counts: np.ndarray) -> np.ndarray:
    """Counts on the histogram's log scale, 0 to 1: a few clipped pixels still show beside a peak."""
    logs = np.log10(1.0 + counts.astype(np.float64))
    top = logs.max()
    return logs / top if top > 0 else logs
