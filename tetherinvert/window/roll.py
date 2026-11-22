"""The Roll step: the roll's folder and name, which are the session, its preset, its
metadata, the files written for it, and starting a session on it; the rolls already in the
folder; and the roll picker in the header over every step. A session's first step, its flat
field, is on Scan (scan.py), beside the live view that shows the holder empty; its state is kept
here with the session's.

`Roll.state(folder)` decides: a free name starts a new roll with the chosen preset, a roll is
continued with its own preset, a folder that is not a roll is never scanned into. A new roll is
written only when its session really starts, so a roll started on nothing writes nothing: its
metadata waits in the page's form until then.

The roll and its session are this part's state (`_init_roll`). The other parts ask it
(`_in_session`, `_session_folder`, `_session_preset`, `_scanning_preset`, …) rather than reading
`_session` themselves, so what a session is stays written down in one place.

The questions before a session (its metadata) are fields on the page, and its flat field is a
step of buttons on Scan, which the tests use as they do the calibration wizard's.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QPushButton, QVBoxLayout,
    QWidget,
)  # fmt: skip

from .. import metadata, paths
from ..flatfield import FILE as FLAT_FILE
from ..flatfield import FlatField
from ..kinds import KINDS
from ..session import Preset, Roll, frame_tiffs
from ..workers import NOT_CONNECTED
from . import style
from .metaedit import RECENT, RollForm
from .rail import LABELS
from .viewer import LIVE

if TYPE_CHECKING:
    from .mainwindow import Now

ROLL_PAGE_WIDTH = 1080  # px: the Roll page's two columns at most, the roll and its metadata on the left, its session and the folder's rolls on the right


def rolls_in(folder: Optional[Path]) -> list[Roll]:
    """The rolls in a folder, the one written to last first."""
    if folder is None or not folder.is_dir():
        return []
    found = [child for child in folder.iterdir() if Roll.state(child) == "existing"]
    found.sort(key=lambda child: (child / "roll.json").stat().st_mtime, reverse=True)
    return [Roll.open(child) for child in found]


def free_name(folder: Optional[Path], stem: str = "Roll") -> str:
    """The first RollNN that is not in the folder yet."""
    for number in range(1, 1000):
        name = f"{stem}{number:02d}"
        if folder is None or not (folder / name).exists():
            return name
    return stem


def preset_name(roll: Roll) -> str:
    return (roll.preset or {}).get("name", "")


class RollHeader(QWidget):
    """Over every step: the roll picker ("Roll01 · Portra 400"), to switch to another roll from
    anywhere, and the step's title. On Presets, which is not about a roll, only the title:
    the picker keeps its roll and is back on the roll's steps."""

    def __init__(self) -> None:
        super().__init__()
        style.set_role(self, "header")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.picker = QPushButton("No roll")
        self.picker.setMenu(QMenu(self.picker))
        self.picker.setMinimumHeight(32)
        self.title = style.label("", "heading", wrap=False)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(16)
        row.addWidget(self.picker)
        row.addWidget(self.title)
        row.addStretch(1)
        # As tall without the picker as with it (the stylesheet's padding counted): a header that changed
        # height would change the window's minimum with the step.
        for widget in (self, self.picker, self.title):
            widget.ensurePolished()
        self.setMinimumHeight(self.minimumSizeHint().height())


class RollPage(QWidget):
    """The roll and its folder, its film preset, its metadata, its flat field and files, the button
    that starts or continues its session; beside them the rolls already in the folder."""

    def __init__(self) -> None:
        super().__init__()
        scroll, column = style.full_page("Name the roll, say what was shot, then start its session: on Scan the flat field first, then the frames.", ROLL_PAGE_WIDTH)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Where rolls are saved")
        self.choose_button = QPushButton("Choose…")
        self.roll_edit = QLineEdit()
        self.roll_edit.setPlaceholderText("Roll name")
        self.roll_edit.setToolTip("Becomes the folder and the start of every file name")
        # The preset as a picker: the presets, then the ways to the Presets step.
        self.preset_picker = QPushButton("None chosen")
        self.preset_menu = QMenu(self.preset_picker)
        self.preset_picker.setMenu(self.preset_menu)
        self.preset_picker.setToolTip("The film preset a new roll is scanned with; a roll continued keeps its own")
        self.preset_label = style.label("", "muted")
        self.preset_label.setTextFormat(Qt.TextFormat.RichText)
        folder_row, preset_row = QHBoxLayout(), QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(self.choose_button)
        preset_row.addWidget(self.preset_picker)
        preset_row.addWidget(self.preset_label, 1)
        roll, roll_column = style.panel("Roll and film")
        form = QFormLayout()
        form.addRow("Roll name", self.roll_edit)
        form.addRow("Folder", folder_row)
        form.addRow("Film preset", preset_row)
        roll_column.addLayout(form)

        about, about_column = style.panel("Metadata")
        self.metadata_panel = about  # Edit… on Finish shows it
        about_column.addWidget(style.label("Every field is optional. Written into every TIFF of the roll (scans/, prints/, negative/).", "muted"))
        self.metadata_form = RollForm()
        about_column.addWidget(self.metadata_form)

        # The session's flat field is its first step on Scan, where the live view shows the holder.
        session, session_column = style.panel("Start the session")
        self.start_note = style.label()  # what to check on the camera, an own light's setting, a roll's next frame
        self.start_note.setTextFormat(Qt.TextFormat.RichText)
        session_column.addWidget(self.start_note)
        session_column.addWidget(style.label("Files", "heading"))
        self.negative_box = QCheckBox("Also save the linear negative (negative/)")
        self.negative_box.setToolTip("The merged exposures as they are: not inverted, no white balance, gamma 1.0, in the roll's negative/ folder.")
        self.crop_box = QCheckBox("Crop files to the picture area")
        self.compare_box = QCheckBox("Also save without flat field, to compare")
        self.compare_box.setToolTip("Writes each file a second time as ..._noflat.tif: same frame, without the correction.")
        for box in (self.negative_box, self.crop_box, self.compare_box):
            session_column.addWidget(box)
        self.note = style.label()  # what the button would do: start a new roll, continue one, or why neither
        self.start_button = QPushButton("Start new session")
        style.set_role(self.start_button, "primary")
        self.start_button.setMinimumHeight(40)
        session_column.addSpacing(6)
        session_column.addWidget(self.note)
        session_column.addWidget(self.start_button)

        # The rolls already in the folder, each with Continue.
        self.rolls_heading = style.label("", "heading")
        self.rolls_list = QWidget()
        self.rolls_rows = QVBoxLayout(self.rolls_list)
        self.rolls_rows.setContentsMargins(0, 0, 0, 0)
        self.rolls_rows.setSpacing(8)
        self.continue_buttons: dict[str, QPushButton] = {}
        self.rolls_empty = style.label("No rolls here yet.", "muted")
        rolls = QWidget()
        rolls_column = QVBoxLayout(rolls)
        rolls_column.setContentsMargins(0, 0, 0, 0)
        rolls_column.setSpacing(10)
        rolls_column.addWidget(self.rolls_heading)
        rolls_column.addWidget(self.rolls_list)
        rolls_column.addWidget(self.rolls_empty)
        hint = "Continue a roll to scan more frames into it. Its preset and metadata come with it."
        rolls_column.addWidget(style.label(hint, "muted"))

        # The roll and its metadata on the left; on the right what is done with it, so Start is in view
        # without scrolling past the metadata, and the other rolls under it.
        left, right = QVBoxLayout(), QVBoxLayout()
        for side, widgets in ((left, (roll, about)), (right, (session, rolls))):
            side.setSpacing(14)
            for widget in widgets:
                side.addWidget(widget)
            side.addStretch(1)
        sides = QHBoxLayout()
        sides.setSpacing(20)
        sides.addLayout(left, 3)
        sides.addLayout(right, 2)
        column.addLayout(sides)
        column.addStretch(1)
        self.scroll = scroll
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def set_rolls(self, rolls: list[tuple[str, str]]) -> None:
        """The folder's rolls as rows: name, and what it is (preset, frames)."""
        while self.rolls_rows.count():
            item = self.rolls_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()  # at once: deleted only at the event loop's next turn, it would be drawn over the new rows until then
                item.widget().deleteLater()
        self.continue_buttons = {}
        for name, about in rolls:
            row = QFrame()
            style.set_role(row, "panel")
            line = QHBoxLayout(row)
            line.setContentsMargins(12, 8, 8, 8)
            words = QVBoxLayout()
            words.setSpacing(0)
            title = style.label(name, "heading", wrap=False)
            words.addWidget(title)
            words.addWidget(style.label(about, "muted"))
            button = self.continue_buttons[name] = QPushButton("Continue")
            button.setToolTip(f"Choose roll “{name}” here, to continue its session")
            line.addLayout(words, 1)
            line.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
            self.rolls_rows.addWidget(row)
            row.show()  # at once: added to a page on screen, Qt shows it a turn of the event loop later, and the page is laid out short until then
        self.rolls_empty.setVisible(not rolls)


class RollPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_roll(self) -> None:
        # While a session runs: folder, preset, flatfield; flat, how its flat field was settled (None:
        # not yet, the session's first step on Scan); capturing; problem, why the last capture failed.
        self._session: Optional[dict] = None
        self._metadata_for: Optional[str] = None  # the roll whose metadata the form shows: its folder, "" for a roll not started yet
        self._flat_seen: tuple = (None, None, None)  # the last flat field read: its file, the file's time, the FlatField
        self._listed: list[tuple[str, str]] = []  # the folder's rolls as the page lists them

    # ----- what the other parts ask -----

    def _in_session(self) -> bool:
        return self._session is not None

    def _flat_pending(self) -> bool:
        """A session runs and its flat field is not settled yet: nothing can be scanned."""
        return self._session is not None and self._session["flat"] is None

    def _flat_capturing(self) -> bool:
        return self._session is not None and self._session["capturing"]

    def _flat_problem(self) -> str:
        """Why the session's last flat-field capture did not work, '' when it did or none was tried."""
        return self._session["problem"] if self._session is not None else ""

    def _session_kept_flat(self) -> Optional[FlatField]:
        """The session roll's flat field, when its preset can use it: the one Keep offers."""
        return self._kept_flat(Path(self._session["folder"]), self._session["preset"]) if self._session is not None else None

    def _session_folder(self) -> Optional[str]:
        return self._session["folder"] if self._session is not None else None

    def _session_flatfield(self) -> bool:
        return self._session is not None and self._session["flatfield"]

    def _session_preset(self) -> Optional[Preset]:
        """The preset of the session running: the roll's own copy, not the store's."""
        return (self._session or {}).get("preset")

    def _scanning_preset(self) -> Optional[Preset]:
        """The preset the light is used with now: the session's, else the one chosen."""
        return self._session["preset"] if self._session is not None else self._preset()

    # ----- the roll -----

    def _roll_folder(self) -> Optional[Path]:
        folder, roll = self.roll_page.folder_edit.text().strip(), self.roll_page.roll_edit.text().strip()
        if not folder or not roll or any(c in roll for c in "/\\:") or roll in (".", ".."):
            return None
        return Path(folder).expanduser() / roll

    def _rolls_folder(self) -> Optional[Path]:
        folder = self.roll_page.folder_edit.text().strip()
        return Path(folder).expanduser() if folder else None

    def _has_raws(self) -> bool:
        folder = self._roll_folder()
        return folder is not None and (folder / "raw").is_dir() and any((folder / "raw").iterdir())

    def _choose_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Folder for rolls", self.roll_page.folder_edit.text())
        if chosen:
            self.roll_page.folder_edit.setText(chosen)

    def _pick_roll(self, name: str) -> None:
        """Another roll of the folder, from the header's picker or a Continue on the Roll page: the
        strip shows it at once, and the step stays (Develop shows one of its frames)."""
        if self._session is not None or self._calibrating():
            return
        step = self._step
        if self._shown != LIVE:
            self._show(LIVE)
        self.roll_page.roll_edit.setText(name)
        self._strip_timer.stop()
        self._refresh_strip()
        self._go(step)

    def _new_roll(self) -> None:
        """New roll… in the picker: a free name on the Roll page, to be started there."""
        if self._session is not None or self._calibrating():
            return
        self._pick_roll(free_name(self._rolls_folder()))
        self._go("roll")
        self.roll_page.roll_edit.setFocus()
        self.roll_page.roll_edit.selectAll()

    def _fill_roll_menu(self) -> None:
        """The header's picker, as it opens: the folder's rolls, then New roll…."""
        menu = self.header.picker.menu()
        menu.clear()
        current = self._roll_folder()
        for roll in rolls_in(self._rolls_folder()):
            name = preset_name(roll)
            action = menu.addAction(f"{roll.name} · {name}" if name else roll.name)
            action.setCheckable(True)
            action.setChecked(current is not None and roll.folder == current)
            action.triggered.connect(lambda _checked=False, chosen=roll.name: self._pick_roll(chosen))
        menu.addSeparator()
        menu.addAction("New roll…").triggered.connect(lambda _checked=False: self._new_roll())

    def _fill_preset_menu(self) -> None:
        """The Roll page's preset picker, as it opens: the presets, then New preset… and Manage presets."""
        menu = self.roll_page.preset_menu
        menu.clear()
        group = QActionGroup(menu)
        chosen = self.presets_page.preset_list.chosen()
        for name in self._store.names():
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name == chosen)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, picked=name: self._choose_preset(picked))
        menu.addSeparator()
        new = menu.addAction("New preset…")
        new.setEnabled(self.presets_page.new_preset_button.isEnabled())
        new.triggered.connect(lambda _checked=False: self._new_preset())
        menu.addAction("Manage presets").triggered.connect(lambda _checked=False: self._go("presets"))

    def _refresh_rolls(self) -> None:
        """The Roll page's list of the folder's rolls, and the roll's metadata when the roll in the
        fields is another. Called with the strip's refresh (a roll typed or picked, a frame scanned):
        it reads each roll.json, too much for every _update_buttons."""
        folder = self._rolls_folder()
        home = str(Path.home())
        shown = str(folder) if folder is not None else ""
        self.roll_page.rolls_heading.setText(f"In {('~' + shown[len(home):]) if shown.startswith(home) else shown}" if shown else "No folder chosen")
        listed = []
        for roll in rolls_in(folder):
            count = len(roll.frames)
            running = " · session running" if self._session is not None and self._session["folder"] == str(roll.folder) else ""
            listed.append((roll.name, " · ".join(filter(None, [preset_name(roll), f"{count} frame{'s' if count != 1 else ''}"])) + running))
        if listed != self._listed:
            self._listed = listed
            self.roll_page.set_rolls(listed)
            for name, button in self.roll_page.continue_buttons.items():
                button.clicked.connect(lambda _checked=False, chosen=name: self._pick_roll(chosen))
        self._show_roll_metadata()
        self._update_buttons()

    def _show_roll_metadata(self) -> None:
        """The form shows the roll's metadata, or a new roll's entries. Those are kept while the name
        is typed (a roll not started is still one roll); another roll's fields replace them."""
        folder = self._roll_folder()
        state = Roll.state(folder)
        key = str(folder) if state == "existing" else ""
        form = self.roll_page.metadata_form
        form.set_recent(self._recent())
        if state == "existing":
            roll = Roll.open(folder)
            form.set_files(sum(len(frame_tiffs(roll, frame)) for frame in roll.frames))
        else:
            preset = self._preset()
            roll = Roll(folder, preset=asdict(preset)) if folder is not None and preset is not None else None
            form.set_files(None)
        if key != self._metadata_for:
            if key or self._metadata_for:  # to or from a roll that exists: its own fields, or none
                form.fill(roll.metadata if state == "existing" else {})
            self._metadata_for = key
        form.set_scanning(self._scanning_lines(roll) if roll is not None else [])

    def _kept_flat(self, folder: Optional[Path], preset: Optional[Preset]) -> Optional[FlatField]:
        """The roll's flat field, when the preset can use it. Read again only when its file changed."""
        if folder is None or preset is None:
            return None
        file = folder / FLAT_FILE
        try:
            stamp = file.stat().st_mtime
        except OSError:
            return None
        if self._flat_seen[:2] != (file, stamp):
            self._flat_seen = (file, stamp, FlatField.load(folder))
        flat = self._flat_seen[2]
        return flat if flat is not None and flat.kind == preset.kind else None

    def _start_lines(self, preset: Preset, roll: Optional[Roll]) -> str:
        """Over the Start button: what to set by hand, an own light's setting, a roll's next frame."""
        by_hand = [name for name, _value, manual in preset.settings() if manual and name != "Light"]  # an own light has its own line
        lines = [f"Scanning with preset <b>{preset.name}</b>."]
        if by_hand:
            lines.append(f"Check on the camera or lens: <b>{', '.join(by_hand)}</b>. The program cannot set it.")
        if not KINDS[preset.kind].scanlight:
            each = " For every frame you set it to red, green and blue only in turn, as asked." if KINDS[preset.kind].prompted else ""
            lines.append(f"Switch your light on at: <b>{preset.light_note or 'the setting of the calibration'}</b>.{each}")
        if roll is not None:
            lines.append(f"The next frame is number {paths.label(roll.next_number())}.")
        return "<br>".join(lines)

    def _roll_note(self, folder: Optional[Path], state: str, own: Optional[Preset], roll: Optional[Roll]) -> str:
        """Over the session button, outside a session: what pressing it would do."""
        if state == "none":
            return "No roll. Choose a folder and a roll name."
        name = folder.name
        if state == "taken":
            return f"A folder named “{name}” is there already and is not a roll. Choose another roll name."
        if state == "new":
            preset = self._preset()
            return f"New roll “{name}” with preset {preset.name}." if preset else f"New roll “{name}”: choose a film preset, then start."
        count = len(roll.frames)
        scanned = f"{count} frame{'s' if count != 1 else ''}"
        if own is None:
            return f"Roll “{name}”, {scanned}. Its preset is not known any more: it continues with the one chosen."
        return f"Roll “{name}”, {scanned}, preset {own.name}. Continue it to scan more; it keeps its preset."

    # ----- the session -----

    def _start_session(self) -> None:
        """A new roll, or one continued; then Scan, where the session's first step is its flat field."""
        if self._session is not None:
            return
        folder = self._roll_folder()
        state = Roll.state(folder)
        entered: Optional[dict] = None  # a new roll's metadata, from the form
        if state == "existing":
            preset = Roll.open(folder).scanned_with() or self._preset()  # a roll keeps the preset it was started with
            if preset is None:
                return
        elif state == "new" and self._preset() is not None:
            preset = self._preset()
            # Kept back until the session really starts, so a start that is refused still writes nothing.
            entered = self.roll_page.metadata_form.values()
            if entered is None:
                self._say("A metadata field is not taken (marked in red on Roll): correct or empty it, then start.")
                return
        else:
            return  # no roll name, or a folder that is not a roll: the button says so
        self._remember()
        roll = Roll.start(folder, preset, flatfield=False)  # from now on the folder is this roll; its flat field is settled on Scan
        if roll.frames and roll.frames[-1].orientation is not None:  # continued: the way up its frames were scanned
            self._set_orientation(roll.frames[-1].orientation)
        if entered:
            roll.set_metadata(entered)
            self._remember_recent(entered)
        if self._store.find(preset.name):
            self._choose_preset(self._store.find(preset.name))
        self._show_roll_metadata()  # the roll exists now: its own fields
        self._session = {"folder": str(folder), "preset": preset, "flatfield": False, "flat": None, "capturing": False, "problem": ""}
        self._refresh_rolls()
        self._go("scan")  # the flat field and the frames are taken on the live view
        self._say("Session started. First the flat field: take the film out of the holder, then Capture flat field.")
        self._update_buttons()

    # ----- the session's first step: its flat field -----

    def _capture_flat(self) -> None:
        """Capture flat field: the bare light, on the hardware thread. The session goes on once it is in."""
        if not self._flat_pending() or self._flat_capturing() or self._busy:
            return
        self._session.update(capturing=True, problem="")
        self._update_buttons()
        self.ask_flatfield.emit(self._session["folder"], self._session["preset"])

    def _keep_flat(self) -> None:
        """Keep the roll's flat field: holder, light and focus are as they were."""
        if not self._flat_pending() or self._flat_capturing():
            return
        flat = self._session_kept_flat()
        if flat is None:
            return
        Roll.open(Path(self._session["folder"])).set_settings(flatfield=True)
        self._settle_flat(True, f"the flat field from {flat.created}")

    def _scan_without(self) -> None:
        """No flat field this session: every LED a quarter stop lower instead."""
        if not self._flat_pending() or self._flat_capturing():
            return
        Roll.open(Path(self._session["folder"])).set_settings(flatfield=False)
        self._settle_flat(False, "no flat field")

    def _on_flat_ready(self, summary: str) -> None:
        self._say(summary)
        if self._flat_capturing():
            self._settle_flat(True, "flat field just captured")

    def _flat_failed(self, problem: str) -> None:
        """The flat field did not come (refused, or its prompt cancelled): back to the step, saying why."""
        if self._flat_capturing():
            self._session.update(capturing=False, problem=problem)
            self._update_buttons()

    def _settle_flat(self, flatfield: bool, note: str) -> None:
        self._session.update(flatfield=flatfield, flat=note, capturing=False, problem="")
        self._show_session(note)

    def _close_session(self) -> None:
        self._session = None
        self._refresh_rolls()

    # ----- the metadata -----

    def _scanning_lines(self, roll: Roll) -> list[tuple[str, str]]:
        light = self._light_text if self._light_text != NOT_CONNECTED else ""
        return metadata.scanning(metadata.scan_context(roll), light)

    def _recent(self) -> dict[str, list[str]]:
        found = {}
        for key in ("camera", "lens"):
            value = self._settings.value(f"recent_{key}", [])
            found[key] = [value] if isinstance(value, str) and value else list(value or [])  # one entry comes back as a string from an INI file
        return found

    def _remember_recent(self, fields: dict) -> None:
        recent = self._recent()
        for key in ("camera", "lens"):
            if fields.get(key):
                self._settings.setValue(f"recent_{key}", metadata.unique([fields[key]] + recent[key])[:RECENT])
        self.roll_page.metadata_form.set_recent(self._recent())

    def _choose_roll_metadata(self) -> Optional[dict]:
        """Copy from roll…: another roll's fields (not its frames')."""
        chosen = QFileDialog.getExistingDirectory(self, "Roll to copy the metadata from", self.roll_page.folder_edit.text())
        if not chosen:
            return None
        if Roll.state(Path(chosen)) != "existing":
            QMessageBox.information(self, "Copy from roll", f"{chosen} is not a roll folder: it has no roll.json.")
            return None
        return Roll.open(Path(chosen)).metadata

    def _copy_roll_metadata(self) -> None:
        found = self._choose_roll_metadata()
        if found is not None:
            self.roll_page.metadata_form.copy_from(found)

    def _save_roll_metadata(self) -> None:
        """Save metadata: into roll.json, then into every file of the roll (render thread)."""
        folder = self._roll_folder()
        form = self.roll_page.metadata_form
        if Roll.state(folder) != "existing":
            return
        fields = form.values()
        if fields is None:
            return
        roll = Roll.open(folder)
        form.edited = False
        self._update_metadata_save()
        if fields == roll.metadata:
            return
        files = sum(len(frame_tiffs(roll, frame)) for frame in roll.frames)
        roll.set_metadata(fields)
        self._remember_recent(fields)
        self._say(f"Roll {roll.name}: metadata saved" + (f"; writing it into {files} file{'s' if files != 1 else ''}…" if files else "."))
        if files:
            self._count_pending(+1)
            self.ask_retag.emit(str(folder), None)
        if str(folder) == self._strip_roll:
            self._roll_metadata = fields
            if self._shown != LIVE:
                self._save_frame_metadata()
                self.frame_page.frame_row.show_frame(self._shown, fields, self._frame_metadata.get(self._shown))
        self._update_buttons()

    def _update_metadata_save(self) -> None:
        """Save is there for a roll that exists, once something was typed (it rewrites every TIFF)."""
        form = self.roll_page.metadata_form
        form.save_button.setEnabled(form.edited and Roll.state(self._roll_folder()) == "existing")

    # ----- the page -----

    def _update_roll_page(self, now: "Now") -> None:
        """The roll and its preset are fixed for the length of a session, and the roll while the wizard
        is open: a session never starts with a preset that is half made."""
        page = self.roll_page
        free = not now.in_session and not now.calibrating
        for widget in (page.folder_edit, page.choose_button, page.roll_edit):
            widget.setEnabled(free)
        for name, button in page.continue_buttons.items():
            chosen = now.folder is not None and name == now.folder.name
            button.setText("Chosen" if chosen else "Continue")
            button.setEnabled(free and not chosen)
        page.preset_picker.setEnabled(not now.in_session)
        preset = self._session["preset"] if now.in_session else (now.own or self._preset())
        if preset is None:
            page.preset_picker.setText("None chosen")
            page.preset_label.setText("Calibrate one with New preset… in this list.")
        else:
            whose = "the roll's own · " if now.own is not None and not now.in_session else ""
            page.preset_picker.setText(preset.name)
            page.preset_label.setText(f"{whose}{KINDS[preset.kind].film}")
        self._update_header(now, preset)

        roll = now.roll
        page.start_note.setText(self._start_lines(preset, roll) if preset is not None and now.state in ("new", "existing") else "")

        # The roll folder is the session: a free name starts a new one, a roll is continued, any other folder is in the way.
        startable = (now.state == "new" and self._preset() is not None) or (now.state == "existing" and (now.own or self._preset()) is not None)
        page.start_button.setEnabled(now.idle and self._prompt is None and not now.calibrating and not now.in_session and startable)
        page.start_button.setText("Session running" if now.in_session else "Continue session" if now.state == "existing" else "Start new session")
        if now.in_session:
            page.note.setText(f"Scanning roll “{Path(self._session['folder']).name}”. End the session on Scan before starting another.")
        else:
            page.note.setText(self._roll_note(now.folder, now.state, now.own, roll))
        self._update_metadata_save()

    def _update_header(self, now: "Now", preset: Optional[Preset]) -> None:
        """The picker says which roll the steps show; it is locked while a session holds the roll."""
        picker = self.header.picker
        if now.folder is None:
            picker.setText("No roll")
        else:
            name = preset_name(now.roll) if now.roll is not None and now.roll.preset else preset.name if preset is not None and now.state == "new" else ""
            picker.setText(f"{now.folder.name} · {name}" if name else now.folder.name)
        picker.setEnabled(not now.in_session and not now.calibrating)
        tip = "Switch to another roll of the folder, or a new one"
        picker.setToolTip("End the session to switch rolls" if now.in_session else "Close the calibration first" if now.calibrating else tip)

    def _set_header_title(self, step: str) -> None:
        """The step's title; the roll picker on the roll's steps only."""
        self.header.picker.setVisible(step != "presets")
        self.header.title.setText("Film presets" if step == "presets" else LABELS[step])
