"""The roll's frames in the window: the strip, the live view or one frame shown large, their
pictures, and the keys.

viewer.py has the widgets and the rules of moving between the live view and the frames. The
pictures are asked of the preview thread (viewer.Previewer) by the frame's development, and an
answer for a development that is no longer the frame's is dropped. Every picture request counts
up (`_seq`); the window shows any picture newer than the one on screen.

The keys are one application event filter: Tab is held anywhere in the window, and a correction
key must reach the frame whichever widget has the keyboard focus.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPlainTextEdit, QWidget

from .. import development, invert, orient, paths
from ..session import SHEET_DEFAULTS, Roll, delete_frame, frame_files, has_raws
from .develop import BASE_LABELS
from .viewer import KEYS, LIVE, THUMB_EDGE, Request, saturation_key, summary

LIVE_MS, LIVE_AWAY_MS = 60, 500  # live view polling: while it is shown, and while a photo is
INSET_S = 30  # seconds the last scan stays in the live view's corner
FULL_MS = 400  # zoomed in, the picture at 100 % is made again this long after the last correction: one takes about 2 s, a held key repeats faster


class FramesPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_frames(self) -> None:
        self._strip_roll = ""  # the roll folder the strip shows
        self._strip_name = ""  # that roll's name, which its files are named after (Roll.name)
        self._shown = LIVE
        self._thumbs: dict[int, tuple[QImage, development.Development]] = {}  # each frame's in the strip, with the development it shows
        self._thumbs_asked: dict[int, development.Development] = {}  # the thumbnail asked for last, so a refresh does not ask again
        self._rendering: set[int] = set()
        self._last: Optional[int] = None  # the frame scanned last in this window
        self._inset_wanted: Optional[int] = None  # a scanned frame whose picture goes into the live view's corner
        self._peek_from: Optional[tuple[int, str]] = None  # while Tab is held: what to go back to, and the step it was on
        self._developing: Optional[int] = None  # the frame Develop opens: the one looked at last
        self._tab_down = False  # a Tab press the window acted on, until its release
        self._live_icon_at = 0.0
        self._orientation = dict(orient.UPRIGHT)  # for the next scans: set on the live view, recorded per frame
        self._developments = development.Developments({}, frozenset())  # the strip's roll: each frame's base, points, corrections, orientation
        self._kinds: dict[int, str] = {}  # each frame's kind (kinds.KINDS): C, M, Y and Sat do nothing on black and white
        self._matched = False  # the roll's: prints developed with shared points
        self._due = 0  # prints missing or out of date
        self._roll_metadata: dict = {}  # the roll's metadata and each frame's, as in roll.json
        self._frame_metadata: dict[int, Optional[dict]] = {}
        self._before = False  # while C is held: the frame without its corrections
        self._seq = 0  # counts the pictures asked for
        self._shown_seq = 0  # the one on screen: only newer ones replace it
        self._full_seq = 0
        self._strip_timer = QTimer(self)
        self._strip_timer.setSingleShot(True)
        self._strip_timer.setInterval(300)  # typing a roll name opens one roll, not one per letter
        self._strip_timer.timeout.connect(self._refresh_strip)
        self._inset_timer = QTimer(self)
        self._inset_timer.setSingleShot(True)
        self._inset_timer.timeout.connect(lambda: self.view.set_inset(None))
        self._full_timer = QTimer(self)
        self._full_timer.setSingleShot(True)
        self._full_timer.setInterval(FULL_MS)
        self._full_timer.timeout.connect(self._want_full)

    def _reload_pictures(self) -> None:
        """Every picture again: the roll was matched or unmatched. The strip keeps its pictures
        until the new ones arrive."""
        self._refresh_strip()
        if self._shown != LIVE:
            self._show(self._shown)

    def _developed(self, number: int, shown: bool = False) -> Optional[development.Development]:
        """The frame as the window has it: with its corrections as they are being made. `shown`:
        as the frame view shows it, without them while C is held."""
        dev = self._developments.get(number)
        if dev is None:
            return None
        return dev.corrected(None if shown and self._before and number == self._shown else self._corrections.of(number))

    def _ask_picture(self, number: int) -> None:
        dev = self._developed(number, shown=True)
        if dev is None:
            return
        self._seq += 1
        self._previewer.newest = self._seq  # what is still queued for this view is skipped
        self.ask_picture.emit(Request(dev, self._seq))

    def _ask_thumbnail(self, number: int) -> None:
        """A new thumbnail when the one in the strip shows another development; it stays until the new one arrives."""
        dev = self._developed(number)
        if dev is None or number in self._rendering or dev == self._thumbs_asked.get(number):
            return
        if number in self._thumbs and self._thumbs[number][1] == dev:
            return
        self._thumbs_asked[number] = dev
        self.ask_thumbnail.emit(Request(dev))

    def _refresh_strip(self) -> None:
        """The strip lists the roll in the Roll fields, and asks for pictures it does not have yet."""
        folder = self._roll_folder()
        key = str(folder) if folder is not None else ""
        if key != self._strip_roll:
            self._save_edits()  # into the roll they were made on
            self._save_frame_metadata()
            self.frame_page.frame_row.number = None
            self._strip_roll, self._last, self._peek_from, self._tab_down = key, None, None, False
            self._thumbs.clear()
            self._thumbs_asked.clear()
            self._rendering.clear()
            self._corrections.clear()
            self.view.set_inset(None)
        roll = Roll.open(folder) if folder is not None else None
        self._strip_name = roll.name if roll is not None else ""
        frames = roll.frames if roll is not None else []
        self._matched = roll is not None and roll.matched
        for frame in frames:
            self._corrections.load(frame.number, frame.adjust)  # corrections not saved yet count
        self._developments = development.of_roll(roll, self._corrections.made()) if roll is not None else development.Developments({}, frozenset())
        self._kinds = {f.number: f.kind for f in frames}
        self._roll_metadata = roll.metadata if roll is not None else {}
        options = roll.sheet_options() if roll is not None else SHEET_DEFAULTS
        for key, action in self.finish_page.sheet_options.items():
            action.setChecked(options[key])
        self._frame_metadata = {f.number: f.metadata for f in frames}
        captions, self._due = {}, len(self._developments.due)
        for frame in frames:
            dev = self._developments[frame.number]
            due = frame.number in self._developments.due
            # One tag, the most pressing: a frame being written cannot be printed yet; one to print asks for
            # something, one without its RAW files only cannot be rendered again.
            tag = "rendering" if frame.number in self._rendering else "to print" if due else "" if has_raws(roll, frame) else "RAW deleted"
            details = " ".join(filter(None, [BASE_LABELS[dev.base][1] if dev.base != invert.BASES[0] else "", summary(dev.adjust)]))
            captions[frame.number] = paths.label(frame.number) + (f" · {tag}" if tag else "") + (f"\n{details}" if details else "")
        self.strip.set_frames(captions)
        self._update_rail()
        for number in captions:
            self.strip.set_icon(number, self._thumbs[number][0] if number in self._thumbs else None)
            self._ask_thumbnail(number)
        if self._shown not in captions and self._shown != LIVE:
            self._show(LIVE)
        self._update_develop_column()
        self._show_prints()  # Finish's count of prints up to date
        self._ask_sheet()  # roll.json changed, or the roll did
        self._refresh_rolls()  # the Roll page's list and metadata follow the roll too

    def _show(self, number: int) -> None:
        """Show the live view (LIVE) or one frame."""
        if number != LIVE and number not in self.strip.numbers():
            return
        same = number == self._shown
        self._shown = number
        self.strip.blockSignals(True)
        self.strip.select(number)
        self.strip.blockSignals(False)
        live = number == LIVE
        if not live:
            self._developing = number
            self._set_step("develop")  # a frame is looked at in Develop
        elif self._step == "develop":
            self._set_step("scan")  # the live view is Scan's
        if not live and self._target is not None:
            # A rescan waits on the live view only: with a frame shown, Scan (and Space) would
            # otherwise replace a frame other than the one looked at.
            self._say(f"Rescan of frame {paths.label(self._target)} cancelled: a frame was selected.")
            self._target = None
        self._update_scan_buttons()  # for the live view too: an early return here would leave "Rescan frame 1" up, and a click retake the last frame
        if self.frame_page.frame_row.number is not None and self.frame_page.frame_row.number != number:
            self._save_frame_metadata()  # into the frame it was typed for, before the row shows another
        if live:
            return
        if self.frame_page.frame_row.number != number:
            self.frame_page.frame_row.show_frame(number, self._roll_metadata, self._frame_metadata.get(number))
        if not same and self._corrections.shown(number):  # Hold: the picture asked for below shows them
            self._save_timer.start()
        self.frame_page.frame_info.setText(f"Frame {paths.label(number)}")
        dev = self._developments.get(number)
        self.frame_page.frame_orientation.show_orientation(dev.orientation if dev else None)
        self.develop_column.set_base(dev.base if dev else invert.BASES[0])
        if not same or self.frame_view.image is None:
            self.frame_view.show_image(self._thumbs[number][0] if number in self._thumbs else None)  # sharp in a moment, when the picture has been made
        if number not in self._rendering:
            self._ask_picture(number)
        self._update_develop_column()

    def _show_live(self) -> None:
        """Esc, Home: the live view, on Scan; with the calibration wizard open, which has it, Presets stays."""
        self._show(LIVE)
        if self._step != "scan" and not (self._step == "presets" and self._calibrating()):
            self._set_step("scan")

    def _set_orientation(self, orientation: dict) -> None:
        """How the next frames are stored, turned and mirrored; the live view shows it already."""
        self._orientation = orient.normal(orientation)
        self.view.set_orientation(self._orientation)
        self.live_page.live_orientation.show_orientation(self._orientation)

    def _orient(self, change: str) -> None:
        """A turn or mirror (orient.CHANGES), from a button or a key: on the live view, how the next
        frames are stored; on a frame, that frame's files, turned on the render thread. The change
        goes by name, not as a result, so clicks in quick succession add up."""
        if self._shown == LIVE:
            self._set_orientation(orient.CHANGES[change](self._orientation))
            return
        number = self._shown
        # Queued even while the frame is being written: the render thread does one job after the
        # other, and a turn reads the way the frame is stored when its turn comes.
        self._rendering.add(number)
        self._count_pending(+1)
        self._refresh_strip()
        self.ask_turn.emit(self._strip_roll, number, change)

    def _delete_frame(self) -> None:
        """The frame shown, with all its files, after asking."""
        number = self._shown
        if number == LIVE or not self._strip_roll:
            return
        if number in self._rendering:
            self._say(f"Frame {paths.label(number)} is being written; delete it when that is done.")
            return
        roll = Roll.open(Path(self._strip_roll))
        files = frame_files(roll, roll.frame(number))
        kinds = sorted({"RAW files" if f.parent.name == "raw" else "preview" if f.suffix == ".npz" else "TIFFs" for f in files})
        question = (
            f"Delete frame {paths.label(number)} of roll “{roll.name}”?\n\n{len(files)} files go: {', '.join(kinds)}. "
            "This cannot be undone. Lightroom keeps a catalog entry for a TIFF it had added, shown as missing."
        )
        if not self._confirm("Delete frame", question):
            return
        self._save_edits()
        gone = delete_frame(roll, number)
        self._corrections.forget(number)
        self._thumbs.pop(number, None)
        self._thumbs_asked.pop(number, None)
        if self._target == number:
            self._target = None
        if self._last == number:
            self._last = None
        if self.view.inset is not None and self.view.inset[0] == number:
            self.view.set_inset(None)
        numbers = self.strip.numbers()
        after = [n for n in numbers if n > number] or [n for n in numbers if n != number]
        self._say(f"Frame {paths.label(number)} deleted, {len(gone)} files.")
        self._refresh_next()
        self._refresh_strip()
        self._show(after[0] if after else LIVE)
        self._update_buttons()

    def _peek(self, held: bool) -> None:
        """Holding Tab shows the last frame scanned; letting go returns to what was shown."""
        if held and self._last is not None and self._peek_from is None:
            self._peek_from = (self._shown, self._step)
            self._show(self._last)
        elif not held and self._peek_from is not None:
            (back, step), self._peek_from = self._peek_from, None
            self._show(back)
            if back == LIVE:
                self._set_step(step)  # Roll, say: Tab was held there

    def _want_full(self) -> None:
        if self._shown != LIVE and self.frame_view.zoomed:
            self._full_seq += 1
            self._previewer.newest_full = self._full_seq  # one still queued for older corrections is skipped
            dev = self._developed(self._shown, shown=True)
            if dev is not None:
                self.ask_full.emit(Request(dev, self._full_seq))

    def _on_thumbnail(self, request: Request, image: QImage) -> None:
        dev = request.development
        number = dev.number
        if dev != self._developed(number):
            return  # asked for before the roll, the base, the points or the corrections changed
        self._thumbs[number] = (image, dev)
        self.strip.set_icon(number, image)
        if self.view.inset is not None and self.view.inset[0] == number:  # rendered again, or shown as another output
            self.view.set_inset((number, image))
        if number == self._inset_wanted:
            self._inset_wanted = None
            self.view.set_inset((number, image))
            self._inset_timer.start(INSET_S * 1000)

    def _on_picture(self, request: Request, image: QImage, outline: Optional[tuple]) -> None:
        # Not only the newest: while a key is held a newer one is always asked for before the last
        # arrives, and waiting for the newest would show nothing until the key is let go.
        dev = request.development
        current = self._developments.get(dev.number)
        if request.seq <= self._shown_seq or dev.number != self._shown or current is None or current.place != dev.place:
            return  # an older picture, another frame, or the strip shows another roll now
        self._shown_seq = request.seq
        self.frame_view.show_image(image, outline=outline, same_frame=True)  # none on a frame cropped to it
        if self.frame_view.zoomed:
            self._full_timer.start()  # the picture at 100 % again, once the keys rest
        parts = [f"Frame {paths.label(dev.number)}", BASE_LABELS[dev.base][0]]
        parts += ["without its corrections (C held)"] if self._before else []
        parts += ["matched"] if self._matched else []
        self.frame_page.frame_info.setText(" · ".join(parts + ["double-click for 100 %"]))

    def _on_full(self, request: Request, image: QImage) -> None:
        if request.development == self._developed(self._shown, shown=True):
            self.frame_view.show_full(image)

    def _on_settled(self, folder: str, numbers: list) -> None:
        """The renderer is done with these frames: their pictures are new."""
        if folder != self._strip_roll:
            return
        for number in numbers:
            self._rendering.discard(number)
            self._thumbs_asked.pop(number, None)  # rendered again or turned: asked for anew, the old one stays until it arrives
        self._refresh_strip()
        if self._shown in numbers:
            self._show(self._shown)

    def _on_printed(self, folder: str) -> None:
        self._printing = None
        if folder == self._strip_roll:
            self._refresh_strip()  # captions and the count of prints due

    def _on_frame(self, data: bytes) -> None:
        self.view.show_frame(data)
        self.live_page.focus_label.setText(f"Focus {self.view.focus:.0%} of best" if self.view.focus else "Focus —")
        if self.view.image is not None and time.monotonic() >= self._live_icon_at:  # the strip's Live entry, once a second
            self._live_icon_at = time.monotonic() + 1.0
            self.strip.set_icon(LIVE, self.view.image.scaled(THUMB_EDGE, THUMB_EDGE, Qt.AspectRatioMode.KeepAspectRatio))

    def _typing(self) -> bool:
        focused = QApplication.focusWidget()
        if isinstance(focused, QComboBox):  # an editable one keeps the focus itself, not its text field
            return focused.isEditable()
        return isinstance(focused, (QLineEdit, QPlainTextEdit)) and not focused.isReadOnly()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) or not isinstance(watched, QWidget) or watched.window() is not self:
            return super().eventFilter(watched, event)
        key = event.key()
        if key == Qt.Key.Key_Tab:
            # On a frame, Tab goes back to the live view (and stays: a way home that scans nothing,
            # unlike Space). On the live view, held, it shows the last frame until it is let go.
            # A held key repeats its press. Every repeat is taken too: let through, each one would move
            # the keyboard focus on by one widget, and the release could then land in a text field and
            # never end the peek.
            if self._tab_down:
                if kind == QEvent.Type.KeyRelease and not event.isAutoRepeat():
                    self._tab_down = False
                    self._peek(False)
                return True
            if kind == QEvent.Type.KeyPress and not event.isAutoRepeat() and not self._typing():
                if self._shown != LIVE:
                    self._tab_down = True
                    self._show(LIVE)
                    return True
                if self._last is not None:
                    self._tab_down = True
                    self._peek(True)
                    return True
        if self._typing():
            return super().eventFilter(watched, event)
        # Which way up, on the live view (the next frames) and on a frame (its files): N turns a
        # quarter right (Shift: left), M mirrors; side by side.
        # Letters, not [ and ], which a German Mac keyboard types with Option.
        if kind == QEvent.Type.KeyPress and key in (Qt.Key.Key_N, Qt.Key.Key_M) and not event.isAutoRepeat():
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._orient("mirror" if key == Qt.Key.Key_M else "left" if shift else "right")
            return True
        if self._shown == LIVE:
            return super().eventFilter(watched, event)
        # Held: the frame without its corrections (C for compare). A letter, not \, which a German Mac
        # keyboard has no key for: it types it with Option+Shift+7.
        if key == Qt.Key.Key_C:
            if not event.isAutoRepeat() and self._before != (kind == QEvent.Type.KeyPress):
                self._before = kind == QEvent.Type.KeyPress
                self._ask_picture(self._shown)
            return True
        if kind != QEvent.Type.KeyPress:
            return super().eventFilter(watched, event)
        # The arrows go the way things lie: the strip runs left to right under the picture, so ← and →
        # step through it; ↑ and ↓ through the bases.
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):  # through the bases, on a frame only
            self._step_base(1 if key == Qt.Key.Key_Down else -1)
            return True
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):  # through the strip, wherever the keyboard focus is
            numbers = self.strip.numbers()
            step = -1 if key == Qt.Key.Key_Left else 1
            self._show(numbers[min(max(numbers.index(self._shown) + step, 0), len(numbers) - 1)])
            return True
        shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if key in KEYS or saturation_key(event):  # held keys repeat: that is how a value is run up quickly
            name, sign = KEYS[key] if key in KEYS else ("sat", 1)
            self._edit(name, -sign if shift else sign)
            return True
        if key == Qt.Key.Key_L and not event.isAutoRepeat():  # the frame's metadata row, open or closed
            self.frame_page.frame_row.set_open(not self.frame_page.frame_row.is_open())
            return True
        if key == Qt.Key.Key_H and not event.isAutoRepeat():
            self._set_hold(self._corrections.hold is None)
            return True
        if key == Qt.Key.Key_X and not event.isAutoRepeat():
            self._set_edits(self._shown, None)
            return True
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not event.isAutoRepeat():  # Backspace: the Mac's delete key
            self._delete_frame()
            return True
        return super().eventFilter(watched, event)

