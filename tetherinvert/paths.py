"""Where TetherInvert keeps its own files, and what a roll's files are called."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# A roll's TIFFs, each kind in its own subfolder, so Lightroom can watch one kind alone: the
# flat positive, the master every print is made from (the scans, watched while scanning), the
# optional linear negative, and the prints.
FOLDERS = {"flat": "scans", "linear": "negative", "print": "prints"}


def home() -> Path:
    """~/.tetherinvert, or the folder named by TETHERINVERT_HOME."""
    return Path(os.environ.get("TETHERINVERT_HOME") or Path.home() / ".tetherinvert")


# Frames are numbered as on the film: 135 film prints 00, 0, 1, 2, … along its edge, and the
# first scans of a roll are often 00 or 0. 00 is stored as -1. These three functions are the only
# places the spelling lives.
FIRST = -1  # 00, the lowest number on the film


@dataclass(frozen=True)
class Place:
    """What a roll's file names need: where the roll is and its name (session.Roll has both too).
    The name is the roll's as roll.json keeps it, not the folder's: a roll moved into another
    folder under another name (Roll2 into Roll/2) still finds its files as Roll2_001, not 2_001."""

    folder: Path
    name: str


def stem(name: str, number: int) -> str:
    """The name every file of a frame starts with: the roll's name and the frame number.
    00 -> Roll15_00, 0 -> Roll15_000, 1 -> Roll15_001: sorted by name the TIFFs come out in film
    order, since "_00." sorts before "_000"."""
    return f"{name}_{'00' if number == -1 else f'{number:03d}'}"


def label(number: int) -> str:
    """A frame's number as the film's edge prints it: 00, 0, 1, 2, …"""
    return "00" if number == -1 else str(number)


def parse(text: str) -> int:
    """A frame number as the operator writes it ("00", "0", "7") -> the number stored."""
    text = str(text).strip()
    if text == "00":
        return -1
    try:
        number = int(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a frame number: 00, 0, 1, 2, …") from None
    if number < FIRST:
        raise ValueError(f"{text!r} is not a frame number: the film starts at 00")
    return number


def contact_sheet(roll: Place) -> Path:
    """The roll's contact sheet: in the roll folder itself, so Lightroom, which watches scans/
    and prints/, never adds it."""
    return Path(roll.folder) / f"{roll.name}_contact.pdf"


def contact_page(first: Path, page: int) -> Path:
    """A page of the contact sheet as PNG: the first is `first` (Roll_contact.png), the next
    Roll_contact_2.png, … (page counts from 0)."""
    first = Path(first)
    return first if page == 0 else first.with_name(f"{first.stem}_{page + 1}{first.suffix}")


def tiff(roll: Place, number: int, kind: str) -> Path:
    """The TIFF of a frame: "flat", "linear" or "print". It exists only if it was written."""
    return Path(roll.folder) / FOLDERS[kind] / f"{stem(roll.name, number)}.tif"
