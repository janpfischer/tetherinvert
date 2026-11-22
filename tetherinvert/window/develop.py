"""The Develop step: a frame shown large as its print will be, its base, its corrections, Hold and
Match the roll, and the frame's metadata. The prints are written on Finish.

The corrections are made on the preview and are the truth while the window is open
(`corrections.Corrections`, no Qt); they go to roll.json `SAVE_MS` after the last key, before the
strip switches rolls, and on close.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from .. import invert, kinds, paths, preview
from ..corrections import Corrections
from ..session import Roll
from . import style
from .metaedit import FrameRow
from .viewer import COLOUR_ONLY, KEYS, LIVE, FrameView, Gauge, OrientationBar, saturation_letter

# The bases a frame's print is developed from (invert.BASES): name, short name for buttons, what it does.
BASE_LABELS = {
    "flat": ("Flat", "Flat", "As scanned: inverted between film base and densest tone, no curve."),
    "converted": ("Converted", "Conv", "Black at the picture's first tone in every channel (as done by hand per channel in Lightroom); white at 75 %."),
    "converted white": ("Converted + white", "Conv + W", "Converted, and the densest tone taken up to white (98 %) as well."),
}
HINTS = {  # both ends of each correction, as on its gauge
    "d": "Density: − lighter, + darker", "c": "C: − red, + cyan", "m": "M: − green, + magenta", "y": "Y: − blue, + yellow",
    "grad": "Contrast: − softer, + harder", "sat": "Saturation: − greyer, + colour; + is the key right of T, found by its place",
}  # fmt: skip
CORRECTIONS = (("d", "D"), ("c", "C"), ("m", "M"), ("y", "Y"), ("grad", "Contrast"), ("sat", "Sat"))  # the keys of invert.edit, as the SP-3000 names them
NAME_WIDTH = 64  # px: a correction's name and keys, left of its gauge
SAVE_MS = 400  # corrections go to roll.json this long after the last key, not with every repeat of a held one
META_SAVE_MS = 1500  # a frame's metadata this long after the last change: typing pauses longer than a held key repeats



class FramePage(QWidget):
    """A frame: which it is, which way up, Rescan (in its roll's session), Delete; its metadata row; the picture. Its base and
    corrections are in the column beside it (DevelopColumn)."""

    def __init__(self, frame_view: FrameView) -> None:
        super().__init__()
        self.frame_view = frame_view
        self.frame_info = QLabel()
        self.frame_info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # cut short rather than widen the window
        self.frame_orientation = OrientationBar("Turns this frame's files, its print included (keys N, Shift+N, M); RAW files and preview stay as the camera saw them.")
        self.delete_frame_button = QPushButton("Delete frame…")
        style.set_role(self.delete_frame_button, "danger")
        self.delete_frame_button.setToolTip("Delete this frame: its RAW files, TIFFs, print and preview (Delete key)")
        # Scanning a frame of the session's roll again: a frame is looked at here, not on Scan.
        self.rescan_button = QPushButton("Rescan frame")
        self.rescan_button.setToolTip("Shoots nothing yet: the live view comes back, and Scan reads Replace frame. Put this frame in the holder first.")
        self.rescan_button.hide()
        self.frame_tools = QWidget()
        tools = QHBoxLayout(self.frame_tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.addWidget(self.frame_info, 1)
        tools.addWidget(self.frame_orientation)
        tools.addWidget(self.rescan_button)
        tools.addWidget(self.delete_frame_button)
        self.frame_row = FrameRow()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        for widget in (self.frame_tools, self.frame_row, self.frame_view):
            column.addWidget(widget, 1 if widget is self.frame_view else 0)


class DevelopColumn(QWidget):
    """Beside the frame shown: its base, a switch of three; its corrections in the SP-3000's terms, a
    row each with its keys (KEYS do the same), its gauge (GAUGES: which way the picture goes) and
    − value +; its tone; Hold, Match the roll and Give these to all frames. The prints are on Finish:
    the rail's count says how many are due."""

    step = Signal(str, int)  # key, +1 or -1
    tone = Signal(str)
    match = Signal(bool)
    hold = Signal(bool)
    reset = Signal()
    to_all = Signal()

    def __init__(self) -> None:
        super().__init__()
        column = QVBoxLayout(self)
        column.setContentsMargins(style.COLUMN_MARGIN, 16, style.COLUMN_MARGIN, 16)
        column.setSpacing(8)

        self.base_buttons = {key: QPushButton(short) for key, (_, short, _) in BASE_LABELS.items()}
        switch = QFrame()
        style.set_role(switch, "switch")
        row = QHBoxLayout(switch)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        self._bases = bases = QButtonGroup(self)
        for key, button in self.base_buttons.items():
            button.setCheckable(True)
            style.set_role(button, "segment")
            button.setToolTip(f"{BASE_LABELS[key][0]}: {BASE_LABELS[key][2]} What this frame's print is developed from (↑ / ↓).")
            bases.addButton(button)
            row.addWidget(button, 1)
        self.base_note = style.label("", "muted")
        # As tall as its longest text from the start: grown by it, the column would change its height.
        heights = []
        for _, _, what in BASE_LABELS.values():
            self.base_note.setText(what)
            heights.append(self.base_note.heightForWidth(style.SIDE_WIDTH - 2 * style.COLUMN_MARGIN))
        self.base_note.setMinimumHeight(max(heights))
        self.set_base(invert.BASES[0])
        column.addLayout(_heading("Base", "↑ ↓"))
        column.addWidget(switch)
        column.addWidget(self.base_note)

        _divide(column)
        self.reset_button = QToolButton()
        self.reset_button.setText("Reset  X")
        self.reset_button.setToolTip("Take this frame's corrections back (X)")
        self.reset_button.clicked.connect(self.reset.emit)
        heading = _heading("Corrections")
        heading.addWidget(self.reset_button)
        column.addLayout(heading)
        self.values: dict[str, QLabel] = {}
        self.names: dict[str, QLabel] = {}
        self.key_labels: dict[str, QLabel] = {}
        self.buttons: dict[str, tuple[QToolButton, QToolButton]] = {}  # − and +
        self.gauges: dict[str, Gauge] = {}
        rows = QGridLayout()
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setHorizontalSpacing(10)
        rows.setVerticalSpacing(2)
        rows.setColumnMinimumWidth(0, NAME_WIDTH)
        rows.setColumnStretch(1, 1)
        for line, (key, label) in enumerate(CORRECTIONS):
            names = QVBoxLayout()
            names.setSpacing(0)
            name = self.names[key] = style.label(label, "heading", wrap=False)
            keys = self.key_labels[key] = style.label(correction_keys(key), "keys", wrap=False)
            hint = f"{HINTS[key]} ({keys.text()})"
            for widget in (name, keys):
                widget.setToolTip(hint)
                names.addWidget(widget)
            rows.addLayout(names, line, 0)
            gauge = self.gauges[key] = Gauge(key)
            gauge.setToolTip(hint)
            rows.addWidget(gauge, line, 1)
            steps = QHBoxLayout()
            steps.setSpacing(2)
            buttons = []
            for sign, text in ((-1, "−"), (1, "+")):
                button = QToolButton()
                button.setText(text)
                button.setAutoRepeat(True)  # held like a key
                button.setToolTip(hint)
                button.clicked.connect(lambda _checked=False, k=key, s=sign: self.step.emit(k, s))
                buttons.append(button)
            value = self.values[key] = style.label("0", "value", wrap=False)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setMinimumWidth(value.fontMetrics().horizontalAdvance("+888") + 4)  # the widest value from the start
            for widget in (buttons[0], value, buttons[1]):
                steps.addWidget(widget)
            self.buttons[key] = (buttons[0], buttons[1])
            rows.addLayout(steps, line, 2)
        self.tone_box = QComboBox()
        self.tone_box.addItems([tone.title() for tone in invert.TONES])
        self.tone_box.setToolTip("Tone Adjustment, as on the SP-3000 (T, Shift+T)")
        self.tone_box.activated.connect(lambda index: self.tone.emit(list(invert.TONES)[index]))
        tone = QVBoxLayout()
        tone.setSpacing(0)
        tone.addWidget(style.label("Tone", "heading", wrap=False))
        tone.addWidget(style.label("⇧T / T", "keys", wrap=False))
        rows.addLayout(tone, len(CORRECTIONS), 0)
        rows.addWidget(self.tone_box, len(CORRECTIONS), 1, 1, 2)
        column.addLayout(rows)

        _divide(column)
        self.hold_box = QCheckBox("Hold  H")
        self.hold_box.setToolTip("Carry these corrections to the next frames that have none of their own (H)")
        self.hold_box.clicked.connect(lambda checked: self.hold.emit(checked))  # a lambda that takes the state, see match_box
        self.match_box = QCheckBox("Match the roll")
        self.match_box.setToolTip("Develop every frame with the same points (the median of the roll's), so colour and density agree across the roll")
        # A lambda that takes the state: connected straight to emit, PySide picks clicked() without it
        # (a real click raises "show_edits(bool) needs 1 argument(s), 0 given").
        self.match_box.clicked.connect(lambda checked: self.match.emit(checked))
        for box, hint in ((self.hold_box, "Frames without their own get these"), (self.match_box, "The same points for every frame")):
            column.addWidget(box)
            note = style.label(hint, "muted")
            note.setContentsMargins(22, 0, 0, 4)
            column.addWidget(note)
        self.all_button = QPushButton("Give these to all frames")
        self.all_button.setToolTip("Give every frame of the roll this frame's corrections")
        self.all_button.clicked.connect(self.to_all.emit)
        column.addWidget(self.all_button)
        column.addStretch(1)

    def set_base(self, base: Optional[str]) -> None:
        """The frame's base checked and described; None, no frame: none checked."""
        self._bases.setExclusive(base is not None)  # an exclusive group keeps one button checked
        for key, button in self.base_buttons.items():
            button.setChecked(key == base)
        self._bases.setExclusive(True)
        self.base_note.setText(BASE_LABELS[base][2] if base else "")

    def show_values(self, adjust: Optional[dict], editable: bool, matched: bool, holding: bool, colour: bool = True) -> None:
        """`colour` False: a black-and-white frame, on which C, M, Y and Sat do nothing. They are
        greyed out, but their values are still shown: Hold or To all frames may have brought some."""
        a = invert.adjustment(adjust)
        for key, label in self.values.items():
            label.setText(f"{a[key]:+d}" if a[key] else "0")
            style.set_tone(label, "accent" if a[key] else "")  # a changed value stands out
            self.gauges[key].set_value(a[key])
        self.tone_box.setCurrentIndex(list(invert.TONES).index(a["tone"]))
        self.match_box.setChecked(matched)
        self.hold_box.setChecked(holding)
        if not editable:
            self.set_base(None)
        for widget in self.findChildren(QToolButton) + [self.tone_box, self.hold_box, self.match_box, self.all_button, *self.base_buttons.values()]:
            widget.setEnabled(editable)
        for key in COLOUR_ONLY:
            for widget in (*self.buttons[key], self.names[key], self.key_labels[key], self.values[key], self.gauges[key]):
                widget.setEnabled(colour and (editable or not isinstance(widget, QToolButton)))


def correction_keys(key: str) -> str:
    """A correction's keys, − first as its buttons: "A / Q", "⇧G / G"."""
    if key == "sat":
        letter = saturation_letter()
        return f"⇧{letter} / {letter}"
    pressed = {(name, sign): QKeySequence(qt_key).toString() for qt_key, (name, sign) in KEYS.items()}
    return f"{pressed[key, -1]} / {pressed[key, 1]}" if (key, -1) in pressed else f"⇧{pressed[key, 1]} / {pressed[key, 1]}"


def _heading(text: str, keys: str = "") -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(style.label(text, "heading", wrap=False))
    row.addStretch(1)
    if keys:
        row.addWidget(style.label(keys, "keys", wrap=False))
    return row


def _divide(column: QVBoxLayout) -> None:
    """A line between two sections of the column."""
    line = QFrame()
    style.set_role(line, "divider")
    line.setFixedHeight(1)
    column.addSpacing(4)
    column.addWidget(line)
    column.addSpacing(4)


class NoFramePage(QLabel):
    """Develop before a roll has a frame."""

    def __init__(self) -> None:
        super().__init__("No frame to develop yet. The frames scanned appear in the strip.")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        style.set_role(self, "muted")


class DevelopPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_develop(self) -> None:
        self._corrections = Corrections()  # the truth while the window is open; saved soon after a change
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_MS)
        self._save_timer.timeout.connect(self._save_edits)
        self._meta_timer = QTimer(self)  # the frame row's edits, saved soon after
        self._meta_timer.setSingleShot(True)
        self._meta_timer.setInterval(META_SAVE_MS)
        self._meta_timer.timeout.connect(self._save_frame_metadata)

    def _set_base(self, base: str) -> None:
        """What the frame shown is developed from; its print is out of date until printed again."""
        number = self._shown
        dev = self._developments.get(number)
        if dev is None or dev.base == base:
            return
        Roll.open(Path(self._strip_roll)).update_frame(number, develop=base)
        self.develop_column.set_base(base)
        self._refresh_strip()  # the roll's developments again, and the strip's thumbnail asked for
        self._ask_picture(number)

    def _step_base(self, step: int) -> None:
        """The next base to the right (+1) or left (-1), round past the ends."""
        order = list(invert.BASES)
        dev = self._developments.get(self._shown)
        here = order.index(dev.base if dev else order[0])
        self._set_base(order[(here + step) % len(order)])

    def _update_develop_column(self) -> None:
        number = self._shown
        colour = kinds.colour(self._kinds.get(number))
        self.develop_column.show_values(self._corrections.of(number), number != LIVE, self._matched, self._corrections.hold is not None, colour=colour)

    def _set_matched(self, on: bool) -> None:
        """Develop the roll with shared points (or each frame with its own): every print changes."""
        folder = self._roll_folder()
        if folder is None or Roll.state(folder) != "existing":
            return
        roll = Roll.open(folder)
        roll.set_settings(matched=on)
        self._say(f"Roll {roll.name}: " + ("every frame developed with the same points, so the roll matches." if on else "each frame developed with its own points."))
        self._reload_pictures()

    def _ready_to_edit(self) -> bool:
        """Whether the frame shown can be corrected now."""
        number = self._shown
        if number == LIVE or number in self._rendering:
            return False
        dev = self._developments.get(number)
        if dev is None or dev.points is None or not preview.path(dev.place, number).exists():
            self._say(f"Frame {paths.label(number)} has no preview: render it again to correct it.")
            return False
        return True

    def _edit(self, key: str, delta: int) -> None:
        """One step of a correction key on the frame shown. Colour keys do nothing on black and
        white (invert.edit), so a step there would only be a number that changes nothing."""
        if key in COLOUR_ONLY and not kinds.colour(self._kinds.get(self._shown)):
            return
        if self._ready_to_edit():
            self._corrections.step(self._shown, key, delta)
            self._edits_changed(self._shown)

    def _set_tone(self, tone: str) -> None:
        if self._ready_to_edit():
            self._set_edits(self._shown, {**invert.adjustment(self._corrections.of(self._shown)), "tone": tone})

    def _set_edits(self, number: int, adjust: Optional[dict]) -> None:
        if number != LIVE:
            self._corrections.set(number, adjust)
            self._edits_changed(number)

    def _edits_changed(self, number: int) -> None:
        self._save_timer.start()
        if number == self._shown:
            self._ask_picture(number)
        self._update_develop_column()

    def _set_hold(self, on: bool) -> None:
        self._corrections.set_hold(self._shown if on and self._shown != LIVE else None)
        self._say("Hold: frames without corrections of their own take these." if self._corrections.hold is not None else "Hold off.")
        self._update_develop_column()

    def _edits_to_all(self) -> None:
        number = self._shown
        if number == LIVE or not self._confirm("Corrections to all frames", f"Give every frame of this roll the corrections of frame {paths.label(number)}?"):
            return
        self._corrections.to_all(number, self.strip.numbers()[1:])
        self._save_edits()

    def _save_edits(self) -> None:
        """The corrections made since the last save, into roll.json; the strip's captions follow."""
        self._save_timer.stop()
        if not self._corrections.has_unsaved() or not self._strip_roll:
            return
        roll = Roll.open(Path(self._strip_roll))
        saved = self._corrections.take_unsaved()
        for number, adjust in saved:
            roll.update_frame(number, adjust=adjust)
        if self._roll_folder() is not None and str(self._roll_folder()) == self._strip_roll:
            self._refresh_strip()  # new thumbnails; the old ones stay until they arrive, so nothing flickers

    def _save_frame_metadata(self) -> None:
        """The frame row's fields, into roll.json if they changed, then into the frame's files."""
        self._meta_timer.stop()
        number = self.frame_page.frame_row.number
        if number is None or not self._strip_roll:
            return
        before = self._frame_metadata.get(number) or {}
        fields = self.frame_page.frame_row.values(before)
        problems = self.frame_page.frame_row.problems()
        if problems:
            self._on_failed(f"Frame {paths.label(number)}: {problems[0]}")
        if fields == before:
            return
        roll = Roll.open(Path(self._strip_roll))
        if number not in {frame.number for frame in roll.frames}:
            return  # deleted meanwhile
        roll.update_frame(number, metadata=fields or None)
        self._frame_metadata[number] = fields or None
        self._count_pending(+1)
        self.ask_retag.emit(self._strip_roll, [number])

