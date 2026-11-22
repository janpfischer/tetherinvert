"""The window: the state the parts share, the threads, the steps, and the one place that decides what
can be done now (`_update_buttons`).

A session is how a roll gets scanned: pick the preset, start the session, then on Scan take the
film out and capture the flat field (or keep the roll's, or scan without), put the film back in,
scan frame after frame.

The rail (rail.py) is the way around: the steps of a roll, Roll, Scan, Develop and Finish, and
Presets, each a page; Lightroom and the devices open as popovers. Scan and Develop share the picture:
the live view or a frame with the strip under it, and a column beside both; so does Presets while the
calibration wizard is open, which needs the live view. A frame shown is Develop's: a frame clicked in
the strip opens Develop, the live view (Esc, Home, Live) opens Scan.

Three background threads keep the window responsive: the camera and the light, rendering
(workers.py), and the pictures of the roll's frames (viewer.py, which also says how the window
moves between them and the live view). The window only asks and listens, by signals.

MainWindow is put together from parts, one module each, which hold a page's widgets and the
window's behaviour for it; each declares the state it keeps in its `_init_…`:

    roll.py            the Roll page: the roll, its preset, metadata, the folder's rolls, the session
                       and how its flat field is settled; the roll picker over every step
    scan.py            the live view; the Scan column: the flat field's step, Scan, the frame counter, the session
    frames.py          the strip, the live view or a frame shown, the keys
    develop.py         the Develop page: a frame's base and corrections, its metadata
    finish.py          the Finish page: the prints, the contact sheet, the roll's metadata, rendering again, deleting RAW files
    presets.py         the Presets page: film presets by light, the one chosen; the calibration wizard
    devices.py         the camera and the light (a popover)
    lightroompanel.py  Lightroom Classic watching the roll (a popover)
    log.py             the status bar and the log's panel over the page
    helpmenu.py        the Help menu

style.py has the look: colour tokens for light and dark, and the stylesheet made from them.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QMetaObject, QSettings, QSize, Qt, QThread, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QMessageBox, QStackedWidget, QVBoxLayout, QWidget

from ..session import Preset, Roll
from ..workers import Hardware, Renderer
from . import style
from .develop import DevelopColumn, DevelopPart, FramePage, NoFramePage
from .devices import DevicesPanel, DevicesPart
from .finish import FinishPage, FinishPart
from .frames import LIVE_AWAY_MS, LIVE_MS, FramesPart
from .helpmenu import HelpPart
from .lightroompanel import LightroomPanel, LightroomPart
from .log import LogBar, LogPart
from .presets import PresetsPage, PresetsPart, WizardColumn
from .rail import Popover, Rail
from .roll import RollHeader, RollPage, RollPart
from .scan import LivePage, ScanColumn, ScanPart
from .scrolling import Column, Scroll
from .viewer import LIVE, FrameView, LiveView, Previewer, Strip


@dataclass(frozen=True)
class Now:
    """What the pages are decided from (`_update_buttons`), found once for all of them."""

    idle: bool  # no job on the hardware thread
    in_session: bool
    flat_pending: bool  # a session runs, its first step, the flat field, not settled yet
    calibrating: bool  # the calibration wizard is open
    folder: Optional[Path]  # the roll in the Roll fields
    state: str  # Roll.state: none, new, existing or taken
    roll: Optional[Roll]  # that roll, when it exists
    own: Optional[Preset]  # its own preset, the copy it was started with
    has_frames: bool
    has_raws: bool


def debuggable() -> None:
    """Lets a debugger stop in the QThread this runs on. debugpy (VS Code) follows only threads started
    through `threading`, and its "qt" setting knows no PySide6, so breakpoints in the camera, render
    and preview threads would never stop. Connected to each thread's `started` directly: by default PySide
    queues a plain function to the thread that connected it, which would run it on the GUI thread.
    Without a debugger it does nothing."""
    if "debugpy" in sys.modules:
        sys.modules["debugpy"].debug_this_thread()


class MainWindow(RollPart, ScanPart, FramesPart, DevelopPart, FinishPart, PresetsPart, DevicesPart, LightroomPart, LogPart, HelpPart, QMainWindow):
    """The parts come before QMainWindow, so their eventFilter is the one Qt calls."""

    ask_framing = Signal(object)
    ask_calibrate = Signal(str, str, object, object, object)
    ask_calibrate_colour = Signal(object, str, str)  # own-rgb: roi, kind, the colour the operator has set the light to
    ask_shoot = Signal()  # the prompted shot of an own-rgb flat field or frame
    ask_cancel_prompt = Signal()
    ask_flatfield = Signal(str, object)
    ask_scan = Signal(str, object, bool, object)
    ask_render = Signal(str, int, bool)
    ask_render_roll = Signal(str, bool, object)
    ask_live_rate = Signal(int)
    ask_thumbnail = Signal(object)
    ask_picture = Signal(object)
    ask_full = Signal(object)
    ask_sheet = Signal(object)
    ask_prints = Signal(str)
    ask_turn = Signal(str, int, str)
    ask_retag = Signal(str, object)
    ask_contact = Signal(str)

    def __init__(self, sim: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("TetherInvert" + (" (simulated rig)" if sim else ""))
        self._settings = QSettings("tetherinvert", "tetherinvert-sim" if sim else "tetherinvert")
        self._busy = False  # the hardware thread is at a job
        self._pending = 0  # jobs of the render thread not finished yet
        self._step = "roll"  # the step shown (rail.STEPS, or presets)
        parts = (
            self._init_roll, self._init_scan, self._init_frames, self._init_develop, self._init_finish, self._init_presets, self._init_devices,
            self._init_lightroom, self._init_help,
        )  # fmt: skip
        for part in parts:
            part()
        self._build()
        self._restore()

        self._hardware, self._hardware_thread = Hardware(sim), QThread(self)
        self._hardware.moveToThread(self._hardware_thread)
        self._renderer, self._render_thread = Renderer(), QThread(self)
        self._renderer.moveToThread(self._render_thread)
        self._previewer, self._preview_thread = Previewer(), QThread(self)
        self._previewer.moveToThread(self._preview_thread)
        for thread in (self._hardware_thread, self._render_thread, self._preview_thread):
            thread.started.connect(debuggable, Qt.ConnectionType.DirectConnection)  # first, so a breakpoint in Hardware.start stops too
        self._hardware_thread.started.connect(self._hardware.start)

        self._hardware.frame.connect(self._on_frame)
        self._hardware.devices.connect(self._on_devices)
        self._hardware.temperature.connect(self._on_temperature)
        self._hardware.exposure.connect(self._on_exposure)
        self._hardware.message.connect(self._say)
        self._hardware.busy.connect(self._on_busy)
        self._hardware.calibration_step.connect(self._on_calibration_step)
        self._hardware.calibrated.connect(self._on_calibrated)
        self._hardware.flat_ready.connect(self._on_flat_ready)
        self._hardware.prompt.connect(self._on_prompt)
        self._hardware.colour_shot.connect(self._on_colour_shot)
        self._hardware.scanned.connect(self._on_scanned)
        self._hardware.failed.connect(self._on_hardware_failed)
        self._renderer.done.connect(self._on_written)  # every job of it that ends well has written files
        self._renderer.failed.connect(self._on_failed)
        self._renderer.finished.connect(self._on_render_finished)  # a method, so it runs on the window's thread
        self._renderer.settled.connect(self._on_settled)
        self._renderer.printed.connect(self._on_printed)
        self._renderer.print_step.connect(self._on_print_step)
        self._previewer.thumbnail.connect(self._on_thumbnail)
        self._previewer.picture.connect(self._on_picture)
        self._previewer.full.connect(self._on_full)
        self._previewer.sheet.connect(self._on_sheet)
        self.ask_framing.connect(self._hardware.set_framing)
        self.ask_calibrate.connect(self._hardware.calibrate)
        self.ask_calibrate_colour.connect(self._hardware.calibrate_colour)
        self.ask_shoot.connect(self._hardware.shoot)
        self.ask_cancel_prompt.connect(self._hardware.cancel_prompt)
        self.ask_flatfield.connect(self._hardware.flatfield)
        self.ask_scan.connect(self._hardware.scan)
        self.ask_render.connect(self._renderer.frame)
        self.ask_render_roll.connect(self._renderer.roll)
        self.ask_live_rate.connect(self._hardware.set_interval)
        self.ask_thumbnail.connect(self._previewer.make_thumbnail)
        self.ask_picture.connect(self._previewer.make_picture)
        self.ask_full.connect(self._previewer.make_full)
        self.ask_sheet.connect(self._previewer.make_sheet)
        self.ask_prints.connect(self._renderer.prints)
        self.ask_turn.connect(self._renderer.turn)
        self.ask_retag.connect(self._renderer.retag)
        self.ask_contact.connect(self._renderer.contact)
        self._renderer.sheet.connect(self._on_sheet_written)

        self._hardware_thread.start()
        self._render_thread.start()
        self._preview_thread.start()
        self._reload_presets(self._settings.value("preset", ""))
        self._refresh_strip()
        QApplication.instance().installEventFilter(self)  # Tab is held anywhere in the window, not just over one widget
        QApplication.instance().aboutToQuit.connect(self._save_edits)  # Cmd+Q, in case the window is not closed first
        QApplication.instance().aboutToQuit.connect(self._save_frame_metadata)
        for key in ("Esc", "Home"):
            QShortcut(QKeySequence(key), self).activated.connect(self._show_live)
        # Space scans. Not the button's own shortcut: every setText replaces that with the label's
        # mnemonic, and the button says "Scan frame N", so Space would do nothing.
        QShortcut(QKeySequence("Space"), self).activated.connect(self._space)
        self._set_step(self._step)

    def _build(self) -> None:
        """The rail on the left; beside it the header with the roll picker, the step's page and the status
        bar, with the log's panel laid over the page's bottom when it is open. Scan, Develop and the
        calibration wizard share the picture (the workspace: live view or frame, the strip under it, and a
        column beside both); Roll, Finish and Presets are pages of their own."""
        style.apply(QApplication.instance())
        self.view, self.frame_view, self.strip = LiveView(), FrameView(), Strip()
        self.rail, self.header = Rail(), RollHeader()
        self.roll_page, self.finish_page, self.presets_page = RollPage(), FinishPage(), PresetsPage()
        self.live_page, self.frame_page, self.no_frame = LivePage(self.view), FramePage(self.frame_view), NoFramePage()
        self.scan_page, self.develop_column, self.wizard_column = ScanColumn(), DevelopColumn(), WizardColumn()
        self.devices_panel, self.lightroom_panel, self.log_bar = DevicesPanel(), LightroomPanel(), LogBar()
        self._popovers = {
            "devices": Popover("Devices", self.devices_panel, self, self.devices_panel.status),
            "lightroom": Popover("Lightroom Classic", self.lightroom_panel, self, self.lightroom_panel.status),
        }

        # The live view and a frame are pages of one stack, so the window's minimum size is the same for
        # both: shown and hidden by turns, the frame's tools would raise it at the first frame, and a
        # window filling the screen on macOS would grow past its bottom. The columns beside the
        # picture (Scan's, Develop's, the wizard's) are another stack, the same width whichever is shown.
        self.side = QStackedWidget()
        self._sides = {}
        for step, page in (("scan", self.scan_page), ("develop", self.develop_column), ("presets", self.wizard_column)):
            style.set_role(page, "column")
            page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            column = Column()
            QVBoxLayout(column).addWidget(page)
            column.layout().setContentsMargins(0, 0, 0, 0)
            scroll = self._sides[step] = Scroll(column)  # on a short screen the column scrolls
            self.side.addWidget(scroll)
        self.side.setFixedWidth(style.SIDE_WIDTH + self._sides["scan"].bar_width())
        self.views = QStackedWidget()
        for page in (self.live_page, self.frame_page, self.no_frame):
            self.views.addWidget(page)
        # The strip under the picture, as wide as it: under the column too, it would take the column's
        # height on Develop, where every row of it is wanted at once.
        self.picture = QWidget()
        under = QVBoxLayout(self.picture)
        under.setContentsMargins(12, 12, 12, 12)
        under.setSpacing(10)
        under.addWidget(self.views, 1)
        under.addWidget(self.strip)
        # Its least height with the strip, also while the wizard has the strip hidden (the roll's frames
        # are nothing to a calibration, and a click on one would open Develop): the window's minimum stays put.
        for widget in [self.picture, *self.picture.findChildren(QWidget)]:
            widget.ensurePolished()
        self.picture.setMinimumHeight(self.picture.minimumSizeHint().height())
        self.workspace = QWidget()
        row = QHBoxLayout(self.workspace)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.picture, 1)
        row.addWidget(self.side)
        self.body = QStackedWidget()
        for page in (self.roll_page, self.workspace, self.finish_page, self.presets_page):
            self.body.addWidget(page)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        right.addWidget(self.header)
        right.addWidget(self.body, 1)
        right.addWidget(self.log_bar)
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.rail)
        outer.addLayout(right, 1)
        self.setCentralWidget(central)
        self.log_bar.panel.lay_over(self.body)  # over the page, above the status bar: in no layout, so the minimum stays
        self._connect_widgets()
        self._build_menu()
        self.resize(QSize(1400, 900).boundedTo(self.screen().availableGeometry().size()))  # the first start; then where it was left (_restore)

    def _connect_widgets(self) -> None:
        """What each widget does when used. A checkable button's `clicked` goes to a lambda that takes
        `checked`: connected straight to a signal's emit, PySide picks clicked() without it."""
        self.rail.step.connect(self._go)
        self.rail.foot.connect(self._open_popover)
        roll = self.roll_page
        for edit in (roll.folder_edit, roll.roll_edit):
            edit.textChanged.connect(self._update_buttons)
            edit.textChanged.connect(self._strip_timer.start)
        roll.choose_button.clicked.connect(self._choose_folder)
        roll.preset_menu.aboutToShow.connect(self._fill_preset_menu)
        roll.metadata_form.changed.connect(self._update_metadata_save)
        roll.metadata_form.save.connect(self._save_roll_metadata)
        roll.metadata_form.copy.connect(self._copy_roll_metadata)
        roll.start_button.clicked.connect(self._start_session)
        self.header.picker.menu().aboutToShow.connect(self._fill_roll_menu)
        scan = self.scan_page
        scan.next_minus.clicked.connect(lambda: self._step_next(-1))
        scan.next_plus.clicked.connect(lambda: self._step_next(+1))
        scan.scan_button.clicked.connect(self._click_scan)
        scan.retake_button.clicked.connect(self._click_under_scan)
        scan.end_button.clicked.connect(self._end_session)
        scan.to_roll_button.clicked.connect(lambda _checked=False: self._go("roll"))
        scan.continue_button.clicked.connect(self._start_session)
        scan.flat_button.clicked.connect(self._click_flat)
        scan.flat_cancel_button.clicked.connect(self._click_flat_cancel)
        scan.keep_flat_button.clicked.connect(self._keep_flat)
        scan.without_button.clicked.connect(self._scan_without)
        self.view.changed.connect(self._update_buttons)
        self.view.inset_clicked.connect(self._show)
        for bar in (self.live_page.live_orientation, self.frame_page.frame_orientation):
            bar.change.connect(self._orient)
        self.strip.currentRowChanged.connect(lambda _row: self._show(self.strip.shown()))
        frame = self.frame_page
        self.frame_view.wants_full.connect(self._want_full)
        frame.rescan_button.clicked.connect(self._click_rescan)
        frame.delete_frame_button.clicked.connect(self._delete_frame)
        frame.frame_row.changed.connect(self._meta_timer.start)
        frame.frame_row.toggled.connect(lambda on: self._settings.setValue("frame_metadata_open", "true" if on else "false"))
        develop = self.develop_column
        for key, button in develop.base_buttons.items():
            button.clicked.connect(lambda _checked, name=key: self._set_base(name))
        develop.step.connect(self._edit)
        develop.tone.connect(self._set_tone)
        develop.match.connect(self._set_matched)
        develop.hold.connect(self._set_hold)
        develop.reset.connect(lambda: self._set_edits(self._shown, None))
        develop.to_all.connect(self._edits_to_all)
        finish = self.finish_page
        finish.print_button.clicked.connect(self._write_prints)
        finish.edit_button.clicked.connect(self._edit_roll_metadata)
        finish.contact_button.clicked.connect(self._write_contact_sheet)
        for key, box in finish.sheet_options.items():
            box.clicked.connect(lambda checked, name=key: self._set_sheet_option(name, checked))
        finish.roll_button.clicked.connect(self._render_roll)
        finish.clean_button.clicked.connect(self._clean_raws)
        presets = self.presets_page
        presets.preset_list.picked.connect(lambda _name: self._on_preset_chosen())
        presets.new_preset_button.clicked.connect(self._new_preset)
        presets.recalibrate_button.clicked.connect(self._recalibrate)
        presets.delete_button.clicked.connect(self._delete_preset)
        presets.start_button.clicked.connect(self._start_roll)
        wizard = self.wizard_column.wizard_panel
        wizard.edited.connect(self._wizard_edited)
        wizard.forward.connect(self._wizard_forward)
        wizard.backward.connect(self._wizard_back)
        wizard.cancel.connect(self._wizard_cancel)
        wizard.stop.connect(self._wizard_stop)
        lightroom = self.lightroom_panel
        lightroom.lightroom_button.clicked.connect(self._open_lightroom)
        lightroom.stop_button.clicked.connect(self._stop_lightroom)
        for box in (lightroom.lightroom_scans, lightroom.lightroom_prints):
            box.toggled.connect(self._update_buttons)

    def _restore(self) -> None:
        s = self._settings
        if s.value("geometry"):
            self.restoreGeometry(s.value("geometry"))  # size, place and screen, maximized or not
        roll = self.roll_page
        roll.folder_edit.setText(s.value("folder", str(Path.home() / "Scans")))
        roll.roll_edit.setText(s.value("roll", "Roll01"))
        roll.negative_box.setChecked(s.value("negative", "false") == "true")
        roll.crop_box.setChecked(s.value("crop", "false") == "true")
        roll.compare_box.setChecked(s.value("compare", "false") == "true")
        self.lightroom_panel.lightroom_scans.setChecked(s.value("lightroom_scans", "true") == "true")
        self.lightroom_panel.lightroom_prints.setChecked(s.value("lightroom_prints", "true") == "true")
        for key in ("base", "picture"):
            saved = s.value(key, "")
            if saved:
                self.view.rects[key] = tuple(float(v) for v in saved.split(","))
        self._set_orientation({"turn": int(s.value("turn", 0)), "mirror": s.value("mirror", "false") == "true"})
        self.frame_page.frame_row.set_open(s.value("frame_metadata_open", "false") == "true")

    def _remember(self) -> None:
        s = self._settings
        roll = self.roll_page
        s.setValue("geometry", self.saveGeometry())
        s.setValue("folder", roll.folder_edit.text())
        s.setValue("roll", roll.roll_edit.text())
        s.setValue("negative", "true" if roll.negative_box.isChecked() else "false")
        s.setValue("crop", "true" if roll.crop_box.isChecked() else "false")
        s.setValue("compare", "true" if roll.compare_box.isChecked() else "false")
        s.setValue("lightroom_scans", "true" if self.lightroom_panel.lightroom_scans.isChecked() else "false")
        s.setValue("lightroom_prints", "true" if self.lightroom_panel.lightroom_prints.isChecked() else "false")
        s.setValue("turn", self._orientation["turn"])
        s.setValue("mirror", "true" if self._orientation["mirror"] else "false")
        s.setValue("preset", self.presets_page.preset_list.chosen() or "")
        for key, rect in self.view.rects.items():
            s.setValue(key, ",".join(f"{v:.4f}" for v in rect) if rect else "")

    # ----- the steps -----

    def _go(self, step: str) -> None:
        """Open a step: from the rail, or for what is done next (a session started opens Scan). Develop
        opens the frame looked at last, else the roll's last; leaving a frame shows the live view."""
        if step == "develop":
            numbers = self.strip.numbers()[1:]
            frame = self._developing if self._developing in numbers else numbers[-1] if numbers else None
            if frame is not None:
                self._show(frame)  # opens Develop
                return
        elif self._shown != LIVE:
            self._show(LIVE)
        self._set_step(step)

    def _set_step(self, step: str) -> None:
        """Show a step's page. Only `_show` and `_go` call it: a frame shown is Develop's, and
        Develop shows a frame whenever the roll has one. Presets is the list, or the workspace while the
        wizard is open: the film base is drawn on the live view."""
        self._step = step
        wizard = step == "presets" and self._calibrating()
        pages = {"roll": self.roll_page, "finish": self.finish_page, "presets": self.presets_page}
        self.strip.setVisible(not wizard)  # the live view takes its height
        self.body.setCurrentWidget(self.workspace if wizard or step not in pages else pages[step])
        if step == "develop":
            self.views.setCurrentWidget(self.frame_page if self._shown != LIVE else self.no_frame)
        else:
            self.views.setCurrentWidget(self.live_page)
        if step in self._sides:
            self.side.setCurrentWidget(self._sides[step])
        self.rail.set_current(step)
        self._set_header_title(step)
        self._update_devices()  # needed here or not
        self._ask_sheet()  # the contact sheet's pages, made while Finish is shown
        # The live view is polled fast only where it is shown: the camera has other work while a frame is looked at.
        self.ask_live_rate.emit(LIVE_MS if step == "scan" or wizard else LIVE_AWAY_MS)

    def _open_popover(self, name: str) -> None:
        """Lightroom and Devices, beside their place on the rail."""
        self._popovers[name].show_beside(self.rail.buttons[name])

    def _update_rail(self) -> None:
        """The counts on the rail: frames scanned on Scan, frames to print on Develop."""
        count = len(self.strip.numbers()) - 1
        self.rail.buttons["scan"].set_badge(str(count) if count else "")
        self.rail.buttons["develop"].set_badge(str(self._due) if self._due else "", accent=True)

    def _update_buttons(self) -> None:
        """The one place that decides what can be done now: the same facts, page by page."""
        folder = self._roll_folder()
        state = Roll.state(folder)
        roll = Roll.open(folder) if state == "existing" else None
        now = Now(
            idle=not self._busy,
            in_session=self._in_session(),
            flat_pending=self._flat_pending(),
            calibrating=self._calibrating(),
            folder=folder,
            state=state,
            roll=roll,
            own=roll.scanned_with() if roll is not None else None,
            has_frames=roll is not None and bool(roll.frames),
            has_raws=self._has_raws(),
        )
        self._update_roll_page(now)
        self._update_scan_page(now)
        self._update_finish_page(now)
        self._update_lightroom(now)
        self._update_presets_page(now)
        self._update_develop_column()  # which the corrections also update as they are made
        self._update_devices()
        self._update_rail()

    def _confirm(self, title: str, text: str) -> bool:
        """A yes-or-no question. Every question to the operator is a method of its own, so the tests can answer it."""
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _on_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_buttons()

    def _on_render_finished(self) -> None:
        self._count_pending(-1)

    def _count_pending(self, change: int) -> None:
        self._pending += change
        self._update_buttons()

    def closeEvent(self, event) -> None:
        self._remember()
        self._save_edits()
        self._save_frame_metadata()
        QApplication.instance().removeEventFilter(self)
        QApplication.instance().aboutToQuit.disconnect(self._save_edits)
        QApplication.instance().aboutToQuit.disconnect(self._save_frame_metadata)
        # Waits for a running scan or calibration to finish, then switches the light off. A
        # calibration is stopped at its next shot instead of run to the end.
        self._hardware.stop_calibration.set()
        QMetaObject.invokeMethod(self._hardware, "stop", Qt.ConnectionType.BlockingQueuedConnection)
        for thread in (self._hardware_thread, self._render_thread, self._preview_thread):
            thread.quit()
            thread.wait(15000)
        super().closeEvent(event)


def name_the_app_menu(name: str) -> None:
    """On macOS, call the application menu `name`. Run from a Python environment it is named after the
    process, "Python"; the built app has the name in its Info.plist. macOS
    reads it from the main bundle's info dictionary, which is set here through the Objective-C runtime,
    before Qt starts the application. Anything going wrong leaves the menu as it was."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        objc.objc_getClass.restype = objc.sel_registerName.restype = objc.objc_msgSend.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def send(receiver, selector: str, *args):
            objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [type(arg) if isinstance(arg, ctypes.c_char_p) else ctypes.c_void_p for arg in args]
            return objc.objc_msgSend(receiver, objc.sel_registerName(selector.encode()), *args)

        text = lambda words: send(objc.objc_getClass(b"NSString"), "stringWithUTF8String:", ctypes.c_char_p(words.encode()))  # noqa: E731
        info = send(send(objc.objc_getClass(b"NSBundle"), "mainBundle"), "infoDictionary")
        if info:
            send(info, "setObject:forKey:", text(name), text("CFBundleName"))
    except Exception:  # the menu keeps the process's name: nothing else depends on it
        pass


def main(sim: bool = False) -> int:
    if QApplication.instance() is None:
        name_the_app_menu("TetherInvert")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setDesktopFileName("tetherinvert")  # Linux: pairs the window with tetherinvert.desktop for the dock's icon
    window = MainWindow(sim or os.environ.get("TETHERINVERT_SIM") == "1")
    window.show()
    return app.exec()
