"""Roll and frame metadata, and the XMP packet every TIFF of a roll carries.

The operator enters what the program cannot know: the camera and lens the film was shot with,
when, the film and its development, author and copyright, keywords and notes for the roll; per
frame aperture, shutter, date, a note and keywords. Every field is optional, and an empty one is
not written. It is stored in roll.json (`Roll.metadata`, `Frame.metadata`) and written into every
TIFF of the roll (scans/, prints/, negative/) as XMP, tag 700.

Standard fields go where Lightroom and other programs read them. The film and its processing,
and the scanning rig, go into two namespaces of TetherInvert's own, so the scanning camera never
turns up as the camera: exiftool shows them as XMP-tetherinvertFilm and XMP-tetherinvertScan, and the
Lightroom plug-in shows them as its own fields. The namespace URIs are fixed for good: files
carry them. Their properties are simple elements with escaped text, because the plug-in reads
them with string patterns, not an XML parser.
"""

from __future__ import annotations

import math
import re
import time
from datetime import date
from fractions import Fraction
from typing import Optional
from xml.sax.saxutils import escape

from . import __version__, invert, kinds, paths

FILM_NS = "http://ns.tetherinvert.org/film/1.0/"
SCAN_NS = "http://ns.tetherinvert.org/scan/1.0/"
STANDARD_NS = {
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "exifEX": "http://cipa.jp/exif/1.0/",
    "aux": "http://ns.adobe.com/exif/1.0/aux/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "tetherinvertFilm": FILM_NS,
    "tetherinvertScan": SCAN_NS,
}

# The roll's fields: key in roll.json, label, the dialog's group. Shooting, rights and description
# are standard fields; film goes into TetherInvert Film.
ROLL_FIELDS = (
    ("camera", "Camera", "shooting"),
    ("lens", "Lens", "shooting"),
    ("shot_from", "Date shot", "shooting"),
    ("shot_to", "to", "shooting"),
    ("film", "Film stock", "film"),
    ("format", "Format", "film"),
    ("box_speed", "Box speed", "film"),
    ("ei", "Shot at EI", "film"),
    ("developer", "Developer or lab", "film"),
    ("developed", "Date developed", "film"),
    ("author", "Author", "rights"),
    ("copyright", "Copyright", "rights"),
    ("keywords", "Keywords", "description"),
    ("notes", "Notes", "description"),
)
# A frame's fields. Never prefilled from the scanning camera: its exposure is not the film's.
FRAME_FIELDS = (
    ("aperture", "Aperture"),
    ("shutter", "Shutter"),
    ("date", "Date"),
    ("note", "Note"),
    ("keywords", "Keywords"),
    ("keywords_left_out", "Roll keywords left out"),
)
LISTS = {"keywords", "keywords_left_out"}
# The film formats a roll can have (the contact sheet's layout, contact.py, is keyed by these). A
# roll without one is 135 on the sheet.
FORMATS = ("135", "120 6×4.5", "120 6×6", "120 6×7", "120 6×9")
DATES = {"shot_from", "shot_to", "developed", "date"}
SPEEDS = {"box_speed", "ei"}

# TetherInvert Film: XMP property and the roll field it holds. The plug-in's field list follows this
# (a test compares them).
FILM = (
    ("Stock", "film"), ("Format", "format"), ("BoxSpeed", "box_speed"), ("ExposureIndex", "ei"), ("Developer", "developer"),
    ("DateDeveloped", "developed"), ("DateShotTo", "shot_to"),
)  # fmt: skip
# TetherInvert Scan: what the program knows about the scan, filled in by itself.
SCAN = ("Roll", "Frame", "File", "Camera", "Light", "Preset", "Exposure", "FlatField", "Scanned", "Base", "Corrections", "Software")

FILE_KINDS = {"flat": "flat positive", "linear": "linear negative", "print": "print"}
BASE_NAMES = {"flat": "Flat", "converted": "Converted", "converted white": "Converted + white"}
CORRECTION_NAMES = {"d": "D", "c": "C", "m": "M", "y": "Y", "grad": "Contrast", "sat": "Saturation"}
LONGEST_SHUTTER_S = 30  # a bare number above this is taken for a fraction typed without "1/": refused, with advice
PADDING = 2048  # spaces in the packet, so programs that edit XMP in place (Lightroom) have room

_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?)?)?$")


# ----- the fields: checked and tidied as they are set -----


def clean(key: str, value) -> object:
    """A field as stored: text stripped, lists split on commas, dates, speeds, aperture and
    shutter checked and written one way. Empty is "" (or [] for a list): the field is cleared.
    Raises ValueError with what to type instead."""
    if key in LISTS:
        items = value.split(",") if isinstance(value, str) else list(value or [])
        return unique(str(item).strip() for item in items)
    text = str(value or "").strip()
    if not text:
        return ""
    if key in DATES:
        if not _DATE.match(text):
            raise ValueError(f"{text!r} is not a date: write it as 2026-10-03 (or 2026-10, 2026; a time as 2026-10-03 14:05)")
        text = text.replace(" ", "T")
        try:
            _parse_date(text)  # 2026-13-40 has the right shape
        except ValueError:
            raise ValueError(f"{text!r} is not a day of the calendar: write it as 2026-10-03") from None
        return text
    if key in SPEEDS:
        if not text.isdigit() or int(text) == 0:
            raise ValueError(f"{text!r} is not a film speed: write it as a number, 400")
        return str(int(text))
    if key == "aperture":
        found = re.fullmatch(r"(?:f\s*/?\s*)?([0-9]+(?:[.,][0-9]+)?)", text, re.IGNORECASE)
        if not found:
            raise ValueError(f"{text!r} is not an f-stop: write it as f/8 or 5.6")
        return f"f/{float(found.group(1).replace(',', '.')):g}"
    if key == "shutter":
        return _shutter(text)
    if key == "format":
        return _format(text)
    return text


def _format(text: str) -> str:
    """"6x6", "120 6×6", "6 x 6" -> "120 6×6"; "35mm", "135" -> "135"."""
    bare = re.sub(r"\s+", "", text.lower().replace("x", "×").replace("*", "×"))
    bare = bare.removeprefix("120").removesuffix("mm")
    if bare in ("135", "35", "kb"):
        return "135"
    for name in FORMATS[1:]:
        if bare == name.split(" ")[1]:
            return name
    raise ValueError(f"{text!r} is not a film format: one of {', '.join(FORMATS)}")


def _shutter(text: str) -> str:
    bare = re.sub(r"\s*(s|sec|\")$", "", text.strip(), flags=re.IGNORECASE)
    if re.fullmatch(r"1/[0-9]+", bare) and int(bare[2:]) > 0:
        return bare
    try:
        seconds = float(bare.replace(",", "."))
    except ValueError:
        seconds = 0.0
    if seconds <= 0:
        raise ValueError(f"{text!r} is not a shutter speed: write it as 1/125, or in seconds, 2")
    if seconds > LONGEST_SHUTTER_S:
        raise ValueError(f"{text!r} seconds is a long exposure: for a fraction of a second write 1/{bare}")
    fraction = Fraction(seconds).limit_denominator(8000)
    return f"1/{fraction.denominator}" if fraction.numerator == 1 and fraction.denominator > 1 else f"{seconds:g}"


def _parse_date(text: str) -> Optional[date]:
    """The day of a date field (None for a year or month alone); ValueError for no such day."""
    parts = text[:10].split("-")
    numbers = [int(part) for part in parts]
    if len(numbers) == 1:
        return None
    if len(numbers) == 2:
        date(numbers[0], numbers[1], 1)
        return None
    return date(*numbers)


def updated(fields: dict, changes: dict, frame: bool = False) -> dict:
    """`fields` with `changes` applied: each checked (clean), empty ones removed. Unknown keys are
    refused, so a mistyped name on the command line does not vanish into roll.json."""
    known = {key for key, *_ in (FRAME_FIELDS if frame else ROLL_FIELDS)}
    result = dict(fields or {})
    for key, value in changes.items():
        if key not in known:
            raise ValueError(f"no {'frame' if frame else 'roll'} field {key!r}; known: {', '.join(sorted(known))}")
        value = clean(key, value)
        if value in ("", []):
            result.pop(key, None)
        else:
            result[key] = value
    return result


def unique(items) -> list[str]:
    """Without empty ones and repeats, in their order."""
    seen, result = set(), []
    for item in items:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    return result


def keywords(roll: dict, frame: Optional[dict]) -> list[str]:
    """A frame's keywords: the roll's, but those it leaves out, then its own; no repeats."""
    frame = frame or {}
    left_out = {k.lower() for k in frame.get("keywords_left_out", [])}
    return unique([k for k in roll.get("keywords", []) if k.lower() not in left_out] + list(frame.get("keywords", [])))


def frame_date(roll: dict, frame: Optional[dict]) -> str:
    """When the frame was shot: its own date, else the roll's first day."""
    return (frame or {}).get("date") or roll.get("shot_from", "")


# ----- words for the window -----


def push(box_speed: str, ei: str) -> str:
    """"push +1", "pull −2/3", "box speed": how the EI stands to the box speed. Shown, not stored."""
    try:
        stops = math.log2(int(ei) / int(box_speed))
    except (TypeError, ValueError, ZeroDivisionError):
        return ""
    thirds = round(stops * 3)
    if thirds == 0:
        return "box speed"
    whole, rest = divmod(abs(thirds), 3)
    amount = " ".join(filter(None, [str(whole) if whole else "", f"{rest}/3" if rest else ""]))
    return f"push +{amount}" if thirds > 0 else f"pull −{amount}"


def dates(first: str, last: str) -> str:
    """"3–5 Oct 2026", "28 Sep – 2 Oct 2026", "3 Oct 2026"; anything but whole days as typed."""
    try:
        one, two = _parse_date(first) if first else None, _parse_date(last) if last else None
    except ValueError:
        one = two = None
    day = lambda d, year=True: f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")  # noqa: E731
    if one and two and one != two:
        if (one.year, one.month) == (two.year, two.month):
            return f"{one.day}–{day(two)}"
        return f"{day(one, one.year != two.year)} – {day(two)}"
    if one and not last:
        return day(one)
    return " – ".join(filter(None, [first, last]))


def roll_summary(fields: dict) -> list[str]:
    """Up to two short lines, for the side panel and the contact sheet's header: camera and film;
    dates and developer. None for a roll without any of these fields."""
    film = fields.get("film", "")
    speed = fields.get("ei") or fields.get("box_speed")
    if film and fields.get("ei"):
        film += f" at EI {fields['ei']}"
    elif not film and speed:
        film = f"ISO {speed}"
    first = " · ".join(filter(None, [fields.get("camera", ""), film]))
    second = " · ".join(filter(None, [dates(fields.get("shot_from", ""), fields.get("shot_to", "")), fields.get("developer", "")]))
    return [line for line in (first, second) if line]


def frame_summary(roll: dict, frame: Optional[dict]) -> str:
    """The collapsed frame row: the note, and how many keywords, with those left out."""
    frame = frame or {}
    count = len(keywords(roll, frame))
    left = len({k.lower() for k in frame.get("keywords_left_out", [])} & {k.lower() for k in roll.get("keywords", [])})
    words = f"{count} keyword{'s' if count != 1 else ''}" if count else "no keywords"
    if left:
        words += f", {left} of the roll's left out"
    exposure = " ".join(filter(None, [frame.get("shutter", ""), frame.get("aperture", "")]))
    return " · ".join(filter(None, [frame.get("note", ""), exposure, words]))


def corrections(made: Optional[dict]) -> str:
    """A print's corrections, those that are not neutral: "Contrast +2, Tone soft", or "none"."""
    adjust = invert.adjustment(made)
    named = [f"{label} {adjust[key]:+d}" for key, label in CORRECTION_NAMES.items() if adjust[key]]
    if adjust["tone"] != invert.NEUTRAL["tone"]:
        named.append(f"Tone {adjust['tone']}")
    return ", ".join(named) or "none"


# ----- the XMP packet -----


def scan_context(roll) -> dict:
    """What every file of the roll says about the scanning rig: the same for all frames, so a
    whole roll is retagged with one look at the preset and the flat field."""
    from .flatfield import FlatField

    preset = roll.scanned_with()
    flat = FlatField.load(roll.folder) if roll.flatfield else None
    context = {"Roll": roll.name, "Software": f"TetherInvert {__version__}"}
    if preset is not None:
        exposure = " · ".join(filter(None, [f"ISO {preset.iso}" if preset.iso else "", f"{preset.shutter} s", preset.aperture]))
        by_hand = [name.lower() for name, _value, manual in preset.settings() if manual and name != "Light"]
        context["Camera"] = preset.camera
        context["Preset"] = f"{preset.name} ({'colour' if kinds.colour(preset.kind) else 'black and white'})"
        context["levels"] = (preset.kind, preset.levels, preset.light_note)
        context["Exposure"] = exposure + (f" (set by hand: {', '.join(by_hand)})" if by_hand else "")
    context.setdefault("levels", None)
    context["flat"] = (flat.kind, flat.created) if flat is not None else None
    return context


def scanning(context: dict, light: str = "") -> list[tuple[str, str]]:
    """TetherInvert Scan as the roll's dialog shows it (read only): what every file will say."""
    flat, lit = context.get("flat"), context.get("levels")
    if lit is not None and not kinds.KINDS[lit[0]].scanlight:  # an own light: its note names it, not the empty light slot
        light = ", ".join(filter(None, [lit[2], kinds.KINDS[lit[0]].lit(lit[1])]))
    elif light and lit is not None:
        light = f"{light}, {kinds.KINDS[lit[0]].lit(lit[1])}"
    lines = [
        ("Scanned with", context.get("Camera", "")), ("Light", light), ("Preset", context.get("Preset", "")), ("Exposure", context.get("Exposure", "")),
        ("Flat field", f"captured {flat[1]}" if flat else "none"),
        ("Per frame", "roll and frame number, scan time, light, for prints the base and corrections"), ("Software", context.get("Software", "")),
    ]  # fmt: skip
    return [(label, value) for label, value in lines if value]


def xmp(roll, frame, kind: str, suffix: str = "", made: Optional[dict] = None, context: Optional[dict] = None) -> str:
    """The XMP packet of one TIFF of a frame: `kind` "flat", "linear" or "print", `suffix` "_noflat"
    for a comparison copy, `made` what a print was developed with (`Frame.printed`)."""
    fields, own = roll.metadata or {}, frame.metadata or {}
    context = context if context is not None else scan_context(roll)
    standard: list[tuple[str, str]] = []  # (property, XML of its value)
    text = lambda value: escape(str(value))  # noqa: E731
    if fields.get("camera"):
        standard.append(("tiff:Model", text(fields["camera"])))
    if fields.get("lens"):
        standard += [("aux:Lens", text(fields["lens"])), ("exifEX:LensModel", text(fields["lens"]))]
    shot = frame_date(fields, own)
    if shot:
        standard += [("exif:DateTimeOriginal", shot), ("photoshop:DateCreated", shot)]
    speed = fields.get("ei") or fields.get("box_speed")
    if speed:
        standard.append(("exif:ISOSpeedRatings", f"<rdf:Seq><rdf:li>{speed}</rdf:li></rdf:Seq>"))
    if own.get("aperture"):
        standard.append(("exif:FNumber", _rational(float(own["aperture"][2:]))))
    if own.get("shutter"):
        shutter = own["shutter"]
        standard.append(("exif:ExposureTime", shutter if "/" in shutter else _rational(float(shutter))))
    if fields.get("author"):
        standard.append(("dc:creator", f"<rdf:Seq><rdf:li>{text(fields['author'])}</rdf:li></rdf:Seq>"))
    if fields.get("copyright"):
        standard.append(("dc:rights", _alt(fields["copyright"])))
    caption = "\n".join(filter(None, [own.get("note", ""), fields.get("notes", "")]))
    if caption:
        standard.append(("dc:description", _alt(caption)))
    words = keywords(fields, own)
    if words:
        standard.append(("dc:subject", "<rdf:Bag>" + "".join(f"<rdf:li>{text(w)}</rdf:li>" for w in words) + "</rdf:Bag>"))

    film = [(f"tetherinvertFilm:{name}", text(fields[key])) for name, key in FILM if fields.get(key)]
    scan = dict(context)
    flat, lit = scan.pop("flat"), scan.pop("levels")
    scan["Frame"] = paths.label(frame.number)
    scan["File"] = FILE_KINDS[kind] + (" without flat field, to compare" if suffix == "_noflat" else "")
    if frame.light:
        levels = lit[1] if lit is not None and lit[0] == frame.kind else None  # the roll's preset, if the frame was scanned with its kind
        scan["Light"] = ", ".join(filter(None, [frame.light, kinds.KINDS[frame.kind].lit(levels)]))  # an own light's note says it all
    applied = flat is not None and flat[0] == frame.kind and suffix != "_noflat"
    scan["FlatField"] = f"captured {flat[1]}" if applied else "none"
    if frame.scanned:
        scan["Scanned"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(frame.scanned))
    if kind == "print" and made is not None:
        scan["Base"] = BASE_NAMES.get(made.get("develop", ""), made.get("develop", ""))
        scan["Corrections"] = corrections(made)
    scan_props = [(f"tetherinvertScan:{name}", text(scan[name])) for name in SCAN if scan.get(name)]

    body = "".join(f"\n   <{name}>{value}</{name}>" for name, value in standard + film + scan_props)
    spaces = "".join(f'\n    xmlns:{prefix}="{uri}"' for prefix, uri in STANDARD_NS.items())
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        f'  <rdf:Description rdf:about=""{spaces}>{body}\n'
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n" + " " * PADDING + '\n<?xpacket end="w"?>'
    )


def _rational(value: float) -> str:
    fraction = Fraction(value).limit_denominator(1000)
    return f"{fraction.numerator}/{fraction.denominator}"


def _alt(value: str) -> str:
    return f'<rdf:Alt><rdf:li xml:lang="x-default">{escape(value)}</rdf:li></rdf:Alt>'
