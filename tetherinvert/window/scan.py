"""The Scan step: the live view with its tools, and beside it the session: the number the next frame
gets, the Scan button and the one under it, the exposure, ending the session. Also own-rgb's
prompted shots.

A session's first step is its flat field, here beside the live view, which shows the holder empty:
nothing is shot until the operator presses Capture flat field (or keeps the roll's, or scans
without). Until then the counter and Scan are not shown; afterwards the step is not.

The buttons get label, enabled state and action from one decision (`_scan_actions`, `_flat_actions`),
and a click runs the action stored with the label, so the two cannot drift apart: a button that
says "Rescan frame 1" never retakes the last frame.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QToolButton, QVBoxLayout, QWidget

from .. import paths
from ..kinds import EXPOSURES, KINDS
from ..session import Preset, Roll, Scanner, free_number, usable_flat
from . import style
from .viewer import LIVE, LiveView, OrientationBar

if TYPE_CHECKING:
    from .mainwindow import Now


class LivePage(QWidget):
    """The live view and its tools: the two rectangles, which way up the next frames are stored, and
    the focus. Scan shows it, and Presets while a preset is calibrated."""

    def __init__(self, view: LiveView) -> None:
        super().__init__()
        self.view = view
        self.base_tool, self.picture_tool = QPushButton("Draw film base"), QPushButton("Draw picture area")
        tools = QButtonGroup(self)
        for button, tool in ((self.base_tool, "base"), (self.picture_tool, "picture")):
            button.setCheckable(True)
            tools.addButton(button)
            button.clicked.connect(lambda _checked, name=tool: setattr(self.view, "tool", name))
        self.base_tool.setChecked(True)
        self.focus_label = QPushButton("Focus —")
        self.focus_label.setFlat(True)
        self.focus_label.setToolTip("Sharpness as a share of the best seen. Click to start over.")
        self.focus_label.clicked.connect(self.view.reset_focus)
        self.live_orientation = OrientationBar("Applies to the frames scanned from now on (keys N, Shift+N, M); their files are written this way up.")
        self.live_tools = QWidget()
        row = QHBoxLayout(self.live_tools)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.base_tool)
        row.addWidget(self.picture_tool)
        row.addSpacing(12)
        row.addWidget(self.live_orientation)
        row.addStretch(1)
        row.addWidget(self.focus_label)
        # The row as wide as it ever gets, from the start: the film base tool shown (W3), the longest
        # focus reading. Grown by them, it would change the window's minimum size during a calibration.
        self.focus_label.setText("Focus 100% of best")
        self.focus_label.setMinimumWidth(self.focus_label.sizeHint().width())
        self.focus_label.setText("Focus —")
        self.live_tools.setMinimumWidth(self.live_tools.minimumSizeHint().width())
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.live_tools)
        column.addWidget(self.view, 1)


class ScanColumn(QWidget):
    """Beside the live view during a session: its flat field first, then the next frame's number, Scan
    and the button under it, what the camera is set to, the session and its end."""

    def __init__(self) -> None:
        super().__init__()
        # The session's first step: its flat field. One primary button (Capture, or own-rgb's Shoot),
        # Cancel while a colour is prompted, and the two ways round it.
        self.flat_heading = style.label("Flat field", "heading")
        self.flat_text = style.label(
            "Take the film out of the holder and leave the holder in place: the live view shows the bare light. "
            "Then capture it, one shot per colour, a few seconds."
        )
        self.flat_button = QPushButton("Capture flat field")
        style.set_role(self.flat_button, "primary")
        self.flat_button.setMinimumHeight(44)
        self.flat_button.setToolTip("Space")
        self.flat_cancel_button = QPushButton("Cancel flat field")
        self.flat_problem = style.label()
        style.set_tone(self.flat_problem, "danger")
        self.keep_flat_button = QPushButton("Keep the flat field")
        self.keep_flat_note = style.label("If holder, light and focus are as they were.", "muted")
        self.without_button = QPushButton("Scan without")
        self.without_note = style.label("Exposes a quarter stop less, to leave room for light that is uneven across the frame.", "muted")
        self.film_back = style.label("Put the film back in the holder, then scan.")
        style.set_tone(self.film_back, "accent")
        # The number the next scan gets, as on the film (00, 0, 1, …): corrected here when a frame is
        # left out; − and + step only onto numbers the roll does not have.
        self.counter = QWidget()
        counter_row = QHBoxLayout(self.counter)
        counter_row.setContentsMargins(0, 0, 0, 0)
        self.next_minus, self.next_plus = QToolButton(), QToolButton()
        self.next_label = style.label("", "number", wrap=False)
        self.next_minus.setText("−")
        self.next_plus.setText("+")
        self.next_label.setMinimumWidth(56)
        self.next_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        counter_row.addWidget(self.next_minus)
        counter_row.addWidget(self.next_label)
        counter_row.addWidget(self.next_plus)
        counter_row.addStretch(1)
        self.counter.setToolTip("The number on the film's edge next to the frame the next scan takes.")
        self.counter_heading = style.label("Next frame on the film", "heading")
        self.scan_button = QPushButton("Scan")
        style.set_role(self.scan_button, "primary")
        self.scan_button.setMinimumHeight(44)
        self.scan_button.setToolTip("Space")
        self.retake_button = QPushButton("Retake last frame")
        self.exposure_label = style.label("…")
        self.exposure_label.setToolTip("What the camera is set to now")
        self.session_label = style.label()
        self.session_label.setTextFormat(Qt.TextFormat.RichText)
        self.end_button = QPushButton("End session")
        self.end_button.setToolTip("The roll can be continued later: everything about it is in its folder.")
        self.to_roll_button = QPushButton("Roll…")
        self.to_roll_button.setToolTip("A new roll is named, given its preset and metadata, and started on Roll")
        # Outside a session, a roll that exists is continued from here too, where its session was ended.
        self.continue_button = QPushButton("Continue session")
        column = QVBoxLayout(self)
        column.setContentsMargins(20, 20, 20, 20)
        column.setSpacing(8)
        for widget in (self.flat_heading, self.flat_text, self.flat_button, self.flat_cancel_button, self.flat_problem):
            column.addWidget(widget)
        for widget in (self.keep_flat_button, self.keep_flat_note, self.without_button, self.without_note):
            column.addWidget(widget)
        for widget in (self.counter_heading, self.counter):
            column.addWidget(widget)
        column.addSpacing(4)
        column.addWidget(self.film_back)
        column.addWidget(self.scan_button)
        column.addWidget(self.retake_button)
        column.addSpacing(14)
        column.addWidget(style.label("Exposure", "heading"))
        column.addWidget(self.exposure_label)
        column.addSpacing(14)
        column.addWidget(style.label("Session", "heading"))
        column.addWidget(self.session_label)
        column.addWidget(self.continue_button)
        column.addWidget(self.end_button)
        column.addWidget(self.to_roll_button)
        column.addStretch(1)


class ScanPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_scan(self) -> None:
        self._prompt: Optional[tuple[str, str, str]] = None  # own-rgb: ("flat" or "scan", the colour, why the last was refused) while a shot waits
        self._next_frame: Optional[int] = None  # in a session: the number the next scan gets, as on the film
        self._taken: set[int] = set()  # in a session: the numbers the roll has
        self._target: Optional[int] = None  # the frame the next scan replaces (Rescan frame N); the window's only, never saved
        self._actions: tuple = ()  # what the Scan button and the one under it do: set with their labels (_update_scan_buttons)
        self._rescan: Optional[tuple] = None  # what Develop's Rescan frame F does: set with its label (_update_scan_buttons)
        self._flat_now: tuple = ()  # what the flat field's button and Cancel do: set with their labels (_update_scan_buttons)
        self._film_back = False  # the flat field is in: the film goes back in before this session's first frame

    def _show_session(self, flat_note: str) -> None:
        """The session's flat field is settled (the Roll part's `_settle_flat`): what it scans with,
        beside the live view."""
        preset = self._session_preset()
        roll = Roll.open(Path(self._session_folder()))  # Roll.start stored the flat-field choice
        levels, _base = Scanner.session_exposure(roll, preset)
        lowered = ""
        if levels != preset.levels:
            now, was = KINDS[preset.kind].leds(levels), KINDS[preset.kind].leds(preset.levels)
            why = (
                "the flat field shows the light is brighter elsewhere in the frame than where the film base was calibrated"
                if usable_flat(roll, preset.kind)
                else "without a flat field TetherInvert does not know where the light is brightest, so it leaves a quarter stop more room"
            )
            lowered = f"<br>LEDs while scanning: {now} (preset: {was}). Lowered because {why}; this keeps the brightest part from clipping."
        shutters = Scanner.session_shutters(roll, preset)
        if shutters != {letter: preset.shutter_for(letter) for letter in shutters}:  # an own light: no LEDs, the shutters instead
            now = " · ".join(f"{letter} {shutter}" for letter, shutter in shutters.items()).replace("O ", "")
            why = "the flat field shows the light is brighter elsewhere in the frame" if usable_flat(roll, preset.kind) else "there is no flat field"
            lowered = f"<br>Shutter while scanning: {now} s, shorter than the preset's, because {why}; this keeps the brightest part from clipping."
        self.scan_page.session_label.setText(f"<b>Session: {preset.name} → {roll.name}</b>, {flat_note}<br>{self._settings_html(preset)}{lowered}")
        self._film_back = True
        self._say(f"Session on, with {flat_note}. Put the film back in the holder, then scan.")
        self._refresh_next()
        self._update_buttons()

    @staticmethod
    def _settings_html(preset: Preset) -> str:
        """The preset's settings, with those the operator has to set by hand in bold."""
        lines = []
        for name, value, by_hand in preset.settings():
            lines.append(f"<b>{name}: {value} — set this yourself</b>" if by_hand else f"{name}: {value}")
        return "<br>".join(lines)

    def _scan_actions(self) -> tuple:
        """((label, enabled, action) for Scan, the same for the button under it), from the
        session, what is shown, the rescan target and the counter."""
        nothing = lambda: None  # noqa: E731
        if self._prompt is not None and self._prompt[0] == "scan":  # own-rgb: a shot waits for the operator to set the light
            letter, ready = self._prompt[1], not self._busy
            return (f"Shoot: {EXPOSURES[letter].name} only", ready, self.ask_shoot.emit), ("Cancel frame", ready, self._cancel_prompt)
        if not self._in_session() or self._flat_pending():
            return ("Scan", False, nothing), ("Retake last frame", False, nothing)
        idle = not self._busy
        number = self._next_frame
        scan = (f"Scan frame {paths.label(number)}" if number is not None else "Scan", idle, self._scan_new)
        rescan = self._rescan_action()
        if rescan is not None:
            return scan, rescan
        if self._target is not None:
            return (f"Replace frame {paths.label(self._target)}", idle, self._replace), ("Cancel rescan", idle, self._cancel_rescan)
        if self._taken:
            return scan, (f"Retake frame {paths.label(max(self._taken))}", idle, self._retake_last)
        return scan, ("Retake last frame", False, nothing)

    def _rescan_action(self) -> Optional[tuple]:
        """(label, enabled, action) of Rescan frame F for a frame of the session's roll shown, else None.
        A frame shown is Develop's, so it is on Develop's frame bar, where any frame of the session's
        roll can be scanned again."""
        if self._prompt is not None or not self._in_session():
            return None
        if self._shown == LIVE or self._shown not in self._taken or self._strip_roll != self._session_folder():
            return None
        shown = self._shown
        return f"Rescan frame {paths.label(shown)}", not self._flat_pending() and not self._busy, lambda: self._start_rescan(shown)

    def _flat_actions(self) -> tuple:
        """((label, enabled, action) for the flat field's button, the same for Cancel flat field or
        None where it is not shown), from the session's flat field and own-rgb's prompt."""
        if self._prompt is not None and self._prompt[0] == "flat":
            letter, ready = self._prompt[1], not self._busy
            return (f"Shoot: {EXPOSURES[letter].name} only", ready, self.ask_shoot.emit), ("Cancel flat field", ready, self._cancel_prompt)
        capturing = self._flat_capturing()
        return ("Capturing…" if capturing else "Capture flat field", self._flat_pending() and not capturing and not self._busy, self._capture_flat), None

    def _update_scan_buttons(self) -> None:
        self._flat_now = self._flat_actions()
        page = self.scan_page
        page.flat_button.setText(self._flat_now[0][0])
        page.flat_button.setEnabled(self._flat_now[0][1])
        if self._flat_now[1] is not None:
            page.flat_cancel_button.setEnabled(self._flat_now[1][1])
        self._actions = self._scan_actions()
        for button, (label, enabled, _action) in zip((self.scan_page.scan_button, self.scan_page.retake_button), self._actions, strict=True):
            button.setText(label)
            button.setEnabled(enabled)
        self._rescan = self._rescan_action()
        rescan = self.frame_page.rescan_button
        rescan.setVisible(self._rescan is not None)
        if self._rescan is not None:
            rescan.setText(self._rescan[0])
            rescan.setEnabled(self._rescan[1])

    def _click_flat(self) -> None:
        if self._flat_now and self._flat_now[0][1]:
            self._flat_now[0][2]()

    def _click_flat_cancel(self) -> None:
        if self._flat_now and self._flat_now[1] is not None and self._flat_now[1][1]:
            self._flat_now[1][2]()

    def _click_scan(self) -> None:
        if self._actions and self._actions[0][1]:
            self._actions[0][2]()

    def _click_under_scan(self) -> None:
        if self._actions and self._actions[1][1]:
            self._actions[1][2]()

    def _click_rescan(self) -> None:
        if self._rescan is not None and self._rescan[1]:
            self._rescan[2]()

    def _space(self) -> None:
        if self._wizard_shoots():  # own-rgb's colour steps: Space shoots there too
            self._wizard_forward()
        elif self._flat_pending():  # the session's flat field: Space captures, or shoots a prompted colour
            self._click_flat()
        else:
            self._click_scan()

    def _on_prompt(self, what: str, letter: str, problem: str) -> None:
        """The hardware thread waits for an own-rgb shot, or ("") no longer."""
        self._prompt = (what, letter, problem) if what else None
        if what:
            preset = self._session_preset()
            setting = f" ({preset.light_note})" if preset is not None and preset.light_note else ""
            self._say(f"{problem + ' ' if problem else ''}Set your light to {EXPOSURES[letter].name} only, at full{setting}, then Shoot (or Space).")
        self._update_buttons()

    def _cancel_prompt(self) -> None:
        if self._prompt is not None and self._prompt[0] == "flat":
            self._flat_failed("")  # back to the flat field's step
        self.ask_cancel_prompt.emit()

    def _scan_new(self) -> None:
        """The next new frame, numbered by the counter; a new roll's first scan asks first."""
        roll = Roll.open(Path(self._session_folder()))
        if not roll.frames and roll.next is None:  # which number the film gives the first frame
            first = self._ask_first_number()
            if first is None:
                return
            roll.set_next(first)
        if roll.next_number() in {f.number for f in roll.frames}:  # taken meanwhile (the command line): never replace unasked
            roll.set_next(None)
        self._send(roll.next_number(), retake=False)

    def _replace(self) -> None:
        """The frame asked for with Rescan frame N, scanned again; asked once."""
        number = self._target
        if not self._confirm(
            "Replace frame", f"Scan frame {paths.label(number)} again? Its RAW files and TIFFs are replaced; its base and corrections stay."
        ):
            return
        self._target = None
        self._send(number, retake=False)

    def _retake_last(self) -> None:
        self._send(None, retake=True)

    def _start_rescan(self, number: int) -> None:
        """Rescan frame N: nothing is shot yet; the live view comes up, and Scan reads Replace frame N."""
        self._go("scan")
        self._target = number
        self._say(f"Put frame {paths.label(number)} in the holder and press Replace frame {paths.label(number)}.")
        self._update_scan_buttons()

    def _cancel_rescan(self) -> None:
        self._say(f"Rescan of frame {paths.label(self._target)} cancelled.")
        self._target = None
        self._update_scan_buttons()

    def _send(self, number: Optional[int], retake: bool) -> None:
        """Hand the scan to the hardware thread, with the number that was on the button."""
        self._refresh_next()
        self._go("scan")  # what is shot has to be seen, from whichever step Space was pressed
        self.view.set_inset(None)
        self._remember()
        rect = self.view.rects["picture"]
        settings = {
            "rect": list(rect) if rect else None,
            "crop": self.roll_page.crop_box.isChecked() and rect is not None,
            "flatfield": self._session_flatfield(),
            "compare": self.roll_page.compare_box.isChecked(),
            "orientation": dict(self._orientation),
            "number": number,  # what the operator saw is what the frame gets
        }
        self.ask_scan.emit(self._session_folder(), self._session_preset(), retake, settings)  # the roll's preset, not the store's
        self._update_buttons()

    def _refresh_next(self) -> None:
        """The counter, from the session's roll: what the next new frame gets and which numbers it has."""
        if not self._in_session():
            self._next_frame, self._taken = None, set()
            return
        roll = Roll.open(Path(self._session_folder()))
        self._taken = {f.number for f in roll.frames}
        # Before the first scan the question decides; until then the counter is not shown.
        self._next_frame = None if not roll.frames and roll.next is None else roll.next_number()

    def _step_next(self, step: int) -> None:
        """− and +: to the nearest free number, so the counter never points at a scanned frame."""
        if not self._in_session() or self._next_frame is None:
            return
        number = free_number(self._taken, self._next_frame, step)
        if number is None:
            return
        Roll.open(Path(self._session_folder())).set_next(number)
        self._refresh_next()
        self._update_buttons()

    def _ask_first_number(self) -> Optional[int]:
        """Before a roll's first scan: the number on the film next to the frame in the holder.
        Skip counts from 1. None: cancelled."""
        dialog = QDialog(self)
        dialog.setWindowTitle("First frame of the roll")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("<b>Which number is on the film next to this frame?</b>"))
        note = QLabel(
            "Look at the edge print in the live view. Every scan after this one counts on from it; "
            "the counter in the Scan column can be corrected at any time."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        group = QButtonGroup(dialog)
        for text in ("00", "0", "1"):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setMinimumSize(56, 40)
            group.addButton(button)
            row.addWidget(button)
        group.buttons()[0].setChecked(True)
        other = QLineEdit()
        other.setPlaceholderText("or another, e.g. 12")
        for button in group.buttons():
            button.clicked.connect(other.clear)

        def typed(text: str) -> None:  # a number typed in is the answer: no button stays pressed
            if text and group.checkedButton() is not None:
                group.setExclusive(False)
                group.checkedButton().setChecked(False)
                group.setExclusive(True)

        other.textEdited.connect(typed)
        row.addWidget(other)
        layout.addLayout(row)
        problem = QLabel()
        style.set_tone(problem, "danger")
        layout.addWidget(problem)
        buttons = QHBoxLayout()
        skip, cancel, scan = QPushButton("Skip: count from 1"), QPushButton("Cancel"), QPushButton("Scan")
        scan.setDefault(True)
        buttons.addWidget(skip)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(scan)
        layout.addLayout(buttons)
        answer: list[int] = []

        def take() -> None:
            text = other.text().strip() or (group.checkedButton().text() if group.checkedButton() else "")
            try:
                answer.append(paths.parse(text))
            except ValueError as exc:
                problem.setText(str(exc))
                return
            dialog.accept()

        scan.clicked.connect(take)
        skip.clicked.connect(lambda: (answer.append(1), dialog.accept()))
        cancel.clicked.connect(dialog.reject)
        dialog.exec()
        return answer[0] if answer else None

    def _on_scanned(self, folder: str, number: int) -> None:
        self._say(f"Frame {paths.label(number)} captured.")
        self._film_back = False
        self._refresh_next()
        self._count_pending(+1)
        if folder == self._strip_roll:
            self._last = self._inset_wanted = number
            if self._corrections.scanned(number):  # Hold
                self._edits_changed(number)
            self._rendering.add(number)
            self._thumbs.pop(number, None)  # a retake replaces the picture
            self._thumbs_asked.pop(number, None)
            self._refresh_strip()
        self.ask_render.emit(folder, number, self.roll_page.negative_box.isChecked())
        self._update_buttons()

    def _on_hardware_failed(self, text: str) -> None:
        self._flat_failed(text)  # a flat field that did not work out: back to its step, saying why
        self._on_failed(text)


    def _end_session(self) -> None:
        if not self._in_session():
            return
        self._save_edits()
        self._close_session()
        self._target = None
        self._refresh_next()
        self._say("Session ended. The roll can be continued: everything about it is in its folder.")
        self._update_buttons()

    def _update_scan_page(self, now: "Now") -> None:
        page = self.scan_page
        number = self._next_frame
        scanning = now.in_session and not now.flat_pending
        if scanning and number is not None:
            page.next_label.setText(paths.label(number))
            page.next_minus.setEnabled(now.idle and free_number(self._taken, number, -1) is not None)
            page.next_plus.setEnabled(now.idle)
        self._update_scan_buttons()
        kept = self._session_kept_flat() if now.flat_pending else None
        if kept is not None:
            page.keep_flat_button.setText(f"Keep the flat field from {kept.created}")
        problem = self._flat_problem()
        page.flat_problem.setText(problem)
        settling = now.flat_pending and now.idle and not self._flat_capturing() and self._prompt is None
        for button in (page.keep_flat_button, page.without_button):
            button.setEnabled(settling)
        # Outside a session: a roll that exists is continued here; a new one is started on Roll,
        # where it gets its name and metadata.
        out = not now.in_session
        resuming = out and now.state == "existing"
        step = now.flat_pending
        shown = {
            page.flat_heading: step, page.flat_text: step, page.flat_button: step, page.flat_cancel_button: step and self._flat_now[1] is not None,
            page.flat_problem: step and bool(problem), page.keep_flat_button: step and kept is not None, page.keep_flat_note: step and kept is not None,
            page.without_button: step, page.without_note: step,
            page.counter_heading: scanning and number is not None, page.counter: scanning and number is not None,
            page.film_back: scanning and self._film_back, page.scan_button: not step, page.retake_button: not step,
            page.end_button: now.in_session, page.to_roll_button: out and not resuming, page.continue_button: resuming,
        }  # fmt: skip
        for widget, visible in shown.items():  # whatever goes is hidden before the other is shown (else the window can grow past the screen)
            if not visible:
                widget.setVisible(False)
        for widget, visible in shown.items():
            if visible:
                widget.setVisible(True)
        page.continue_button.setEnabled(self.roll_page.start_button.isEnabled())
        page.to_roll_button.setText("New roll on Roll…" if now.state == "new" else "Roll…")
        page.end_button.setEnabled(now.idle and self._prompt is None)
        if step:
            preset = self._session_preset()
            name = Path(self._session_folder()).name
            page.session_label.setText(f"<b>Session: {preset.name} → {name}</b>, its flat field first<br>{self._settings_html(preset)}")
        elif resuming:
            page.session_label.setText(self.roll_page.note.text())
        elif out and now.state == "new":
            page.session_label.setText(f"No session. New roll “{now.folder.name}”: its preset and metadata are on Roll, and it is started there.")
        elif out:
            page.session_label.setText("No session. A roll's session is started or continued on Roll.")
