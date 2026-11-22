"""What each kind of preset means: one table.

A preset's kind names the film and the light together, and every frame and flat field carries the
kind it was made with. Everything that depends on the kind asks this table, the wizard's W1 too;
no code compares kind strings.

The Scanlight has three ways to light a frame, and an own light the same three:

                     colour, 3 shots    colour, 1 shot    black and white
    Scanlight        rgb                rgb-together      white
    Own light        own-rgb            own-colour        own-bw

An own light is set by the operator, never by TetherInvert, which then controls only the camera:
nothing is sent to a Scanlight for an own kind, not even a framing light. With own-rgb the
operator switches the light to each colour for every shot, prompted (`prompted`).

An exposure is named by its file letter (Roll_001_R.ARW): which LEDs it lights, and which sensor
channels it is for. No Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

Levels = tuple[int, int, int, int]  # r, g, b, w


@dataclass(frozen=True)
class Exposure:
    leds: tuple[int, ...]  # which of r, g, b, w it lights
    sensors: tuple[int, ...]  # the sensor channels (0 R, 1 G, 2 B) it is for: metered, flat-fielded, checked for clipping
    name: str  # how the window names it

    def light(self, levels: Levels) -> Levels:
        """The light for this exposure: the levels of its own LEDs, the others off."""
        r, g, b, w = (level if index in self.leds else 0 for index, level in enumerate(levels))
        return r, g, b, w


EXPOSURES = {
    "R": Exposure((0,), (0,), "red"),
    "G": Exposure((1,), (1,), "green"),
    "B": Exposure((2,), (2,), "blue"),
    "W": Exposure((3,), (0, 1, 2), "white"),
    "RGB": Exposure((0, 1, 2), (0, 1, 2), "colour default"),
    "O": Exposure((), (0, 1, 2), "own light"),  # one exposure under the operator's light, lit as it is
}


@dataclass(frozen=True)
class Kind:
    label: str  # W1's choice
    note: str  # under it: what the choice means at the stand
    short: str  # in a heading or a sentence: "New preset · Portra · colour"
    film: str  # how a preset's info names the film
    colours: str  # how W4's stages name what is probed
    exposures: tuple[str, ...]  # per frame, in the order they are shot (EXPOSURES)
    colour: bool  # colour film: C, M, Y and saturation apply, the files are RGB, the contact sheet's edge print is orange
    scanlight: bool  # TetherInvert drives the Scanlight; for an own light it does not, and needs only the camera
    from_light: bool  # the levels are read from the light (its colour default) and only the shutter is calibrated
    group: str = "Scanlight"  # W1 asks for this first, then for one of its three kinds

    @property
    def prompted(self) -> bool:
        """The operator sets the light to each colour by hand, prompted before every shot (own-rgb)."""
        return not self.scanlight and not self.single

    @property
    def fixed(self) -> bool:
        """No LED level is calibrated: the light stays as it is (its colour default, or the operator's), only the shutter moves."""
        return self.from_light or not self.scanlight

    @property
    def single(self) -> bool:
        """One exposure lights every sensor channel: nothing to merge, one bare-light shot, one factor for all LEDs."""
        return len(self.exposures) == 1

    def leds(self, levels: Levels) -> str:
        """The LEDs at these levels, as the window and the log say them."""
        r, g, b, w = levels
        if not self.scanlight:
            return "set by hand"
        if not self.colour:
            return f"white {w}"
        return f"R {r}  G {g}  B {b}" + (" together" if self.single else "")

    def lit(self, levels: Optional[Levels]) -> str:
        """How a frame was lit, for the files' TetherInvert Scan Light (after the light's own name, or
        an own light's note, which says it all for one exposure)."""
        if not self.scanlight:
            return "" if self.single else "red, green and blue one by one"
        if not self.colour:
            return "white LED"
        if not self.single:
            return "red, green and blue LEDs"
        if levels is None:
            return "red, green and blue LEDs together"
        r, g, b, _w = levels
        return f"colour default R {r} G {g} B {b} together"


KINDS = {
    "rgb": Kind(
        "Colour: R, G, B one by one (3 shots)", "Three exposures: red, green, blue. Each LED gets its own level, so the film base is white.",
        "colour", "Colour film", "each colour", ("R", "G", "B"), colour=True, scanlight=True, from_light=False,
    ),
    "rgb-together": Kind(
        "Colour: the light's colour default (1 shot)",
        "One exposure, red, green and blue lit together at the default stored in the light. Only the brightest channel's film base "
        "reaches 90 %; crosstalk between the colours is not corrected.",
        "one-shot colour", "Colour film, one shot", "the colour default", ("RGB",), colour=True, scanlight=True, from_light=True,
    ),
    "white": Kind(
        "Black and white: white LED", "One exposure under the white LED", "black and white", "Black-and-white film", "the white light", ("W",),
        colour=False, scanlight=True, from_light=False,
    ),
    "own-rgb": Kind(
        "Colour: R, G, B by hand (3 shots)",
        "Three exposures. Before each you set your light to red, green or blue only, at full (or the setting you note). Each "
        "colour gets its own shutter.",
        "own light, colour by hand", "Colour film, own light, R, G, B by hand", "each colour", ("R", "G", "B"),
        colour=True, scanlight=False, from_light=False, group="Own light",
    ),
    "own-colour": Kind(
        "Colour: fixed light (1 shot)", "One exposure under your light as it is set: a white panel, an RGB light with all colours "
        "on, or a colour negative light. Crosstalk is not corrected.",
        "own light, colour", "Colour film, own light", "your light", ("O",), colour=True, scanlight=False, from_light=False, group="Own light",
    ),
    "own-bw": Kind(
        "Black and white", "One exposure under your light as it is set.", "own light, black and white", "Black-and-white film, own light",
        "your light", ("O",), colour=False, scanlight=False, from_light=False, group="Own light",
    ),
}  # fmt: skip
GROUPS = ("Scanlight", "Own light")  # W1's first question


def same_mode(kind: str, group: str) -> str:
    """The kind of `group` lit the same way: colour in three shots, in one, or black and white
    (W1, when the light is changed)."""
    ours = [name for name, entry in KINDS.items() if entry.group == KINDS[kind].group]
    return [name for name, entry in KINDS.items() if entry.group == group][ours.index(kind)]


def colour(kind: Optional[str]) -> bool:
    """Whether a frame of this kind is colour; True for one not known (None: no frame shown)."""
    return KINDS[kind].colour if kind in KINDS else True
