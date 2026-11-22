"""The corrections of one roll's frames while the window has the roll open.

The window is the truth for corrections being made: a held key changes a frame's values many
times a second, and they reach roll.json shortly after the last step, in one write (`unsaved`).
What a value means for the pixels is invert.py's business; this module only keeps track of
which frame has which values, and of the SP-3000's Hold key:

- With Hold on, a frame shown or scanned without corrections of its own takes the held ones.
- A frame shown that has its own becomes the held ones. A correction made while holding does
  too.
- A scanned frame that already has corrections (a retake) keeps them.

No Qt here, so these rules are tested without a window.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import invert


class Corrections:
    def __init__(self) -> None:
        self._edits: dict[int, Optional[dict]] = {}  # per frame, as being made; None = none
        self._unsaved: set[int] = set()
        self.hold: Optional[dict] = None  # carried to frames that have none; None = Hold off

    def clear(self) -> None:
        """Another roll: nothing of this one carries over, Hold included."""
        self._edits.clear()
        self._unsaved.clear()
        self.hold = None

    def load(self, number: int, saved: Optional[dict]) -> Optional[dict]:
        """A frame's corrections: as made here if they were, otherwise as saved in roll.json."""
        return self._edits.setdefault(number, saved)

    def of(self, number: int) -> Optional[dict]:
        return self._edits.get(number)

    def made(self) -> dict[int, Optional[dict]]:
        """Every frame's corrections as the window has them (loaded or made here), for development.of_roll."""
        return dict(self._edits)

    def set(self, number: int, adjust: Optional[dict]) -> None:
        """New corrections for a frame (None, or all neutral: none). While holding, they are the held ones."""
        a = invert.adjustment(adjust)
        self._edits[number] = a if invert.corrected(a) else None
        self._unsaved.add(number)
        if self.hold is not None:
            self.hold = a

    def step(self, number: int, key: str, delta: int) -> None:
        """One press of a correction key; the tone key steps through the tones and wraps round."""
        a = invert.adjustment(self._edits.get(number))
        if key == "tone":
            tones = list(invert.TONES)
            a["tone"] = tones[(tones.index(a["tone"]) + delta) % len(tones)]
        else:
            a[key] += delta  # set() keeps it inside the key's range
        self.set(number, a)

    def to_all(self, source: int, numbers: Iterable[int]) -> None:
        for number in numbers:
            self._edits[number] = self._edits.get(source)
            self._unsaved.add(number)

    def set_hold(self, number: Optional[int]) -> None:
        """Hold on, with this frame's corrections (even none), or off (None)."""
        self.hold = invert.adjustment(self._edits.get(number)) if number is not None else None

    def shown(self, number: int) -> bool:
        """A frame is shown. True when it took the held corrections."""
        if self.hold is None:
            return False
        if invert.corrected(self._edits.get(number)):
            self.hold = invert.adjustment(self._edits.get(number))
            return False
        self.set(number, self.hold)
        return True

    def scanned(self, number: int) -> bool:
        """A frame was scanned. True when it took the held corrections."""
        if self.hold is None or invert.corrected(self._edits.get(number)):
            return False
        self.set(number, self.hold)
        return True

    def forget(self, number: int) -> None:
        """A frame deleted: its corrections go with it, saved or not."""
        self._edits.pop(number, None)
        self._unsaved.discard(number)

    def has_unsaved(self) -> bool:
        return bool(self._unsaved)

    def take_unsaved(self) -> list[tuple[int, Optional[dict]]]:
        """The frames changed since the last call, with their corrections, to be written to roll.json."""
        numbers, self._unsaved = sorted(self._unsaved), set()
        return [(number, self._edits.get(number)) for number in numbers]
