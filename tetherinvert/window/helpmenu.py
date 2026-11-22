"""The Help menu: the manual, the keyboard shortcuts drawn on a keyboard (keyboard.py), the doctor
(diagnose.py, run by the hardware thread on the devices it holds), About.

On macOS the menu bar is the screen's; About moves into the application menu there.
"""

from __future__ import annotations

from html import escape
from typing import Optional

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QKeySequence
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout

from .. import __version__, about, keyboard
from ..paths import home
from . import style


class DoctorDialog(QDialog):
    """The doctor's report as the hardware thread finds it, line by line; Copy report puts it on the
    clipboard after About's details, so one paste into an issue has both. Show test shot opens the
    folder that keeps it, to attach a RAW file that does not decode."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("Doctor")
        self.status = style.label("")
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.report.setFont(QFont(style.mono_family()))
        self.report.setMinimumSize(720, 440)
        self.copy_button = QPushButton("Copy report")
        self.copy_button.setToolTip("The report and the details from About TetherInvert onto the clipboard, for an issue")
        self.copy_button.clicked.connect(self.copy)
        self.shot_button = QPushButton("Show test shot")
        self.shot_button.setToolTip("The folder that keeps the test shot, to attach it to an issue")
        self.shot_button.clicked.connect(lambda _checked=False: QDesktopServices.openUrl(QUrl.fromLocalFile(str(home() / "doctor"))))
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.shot_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.report, 1)
        layout.addLayout(buttons)

    def start(self) -> None:
        self.report.clear()
        self.status.setText("Checking the installation and taking a test shot at the camera's current exposure. Nothing on the camera is changed.")
        self.copy_button.setEnabled(False)
        self.shot_button.setEnabled(False)

    def add(self, line: str) -> None:
        self.report.appendPlainText(line)

    def finish(self, decodes: bool) -> None:
        self.status.setText("This camera's files decode: scanning should work." if decodes else "Something is wrong: see the RESULT line. Copy report puts it on the clipboard for an issue.")
        self.copy_button.setEnabled(True)
        self.shot_button.setEnabled((home() / "doctor").is_dir() and any((home() / "doctor").glob("test-shot*")))

    def text(self) -> str:
        return about.text() + "\n\n" + self.report.toPlainText()

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text())


class HelpPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_help(self) -> None:
        self._shortcuts: Optional[QDialog] = None  # the keyboard drawing's window, once opened
        self._doctor: Optional[DoctorDialog] = None  # the doctor's report, once run
        self._doctor_running = False  # asked of the hardware thread, its report not complete yet

    def _build_menu(self) -> None:
        """The menu bar (on macOS the one at the top of the screen): Help with the manual, the shortcuts, the doctor, About."""
        menu = self.menuBar().addMenu("Help")
        manual = menu.addAction("TetherInvert Manual")
        manual.setShortcut(QKeySequence.StandardKey.HelpContents)  # Cmd+? on macOS
        manual.triggered.connect(lambda _checked=False: self._open_url(about.MANUAL_URL))
        menu.addAction("Keyboard Shortcuts").triggered.connect(lambda _checked=False: self._show_shortcuts())
        self.doctor_action = menu.addAction("Run Doctor…")
        self.doctor_action.setToolTip("What is installed and connected, and whether a test shot from this camera decodes")
        self.doctor_action.triggered.connect(lambda _checked=False: self._run_doctor())
        menu.addSeparator()
        info = menu.addAction("About TetherInvert")
        info.setMenuRole(QAction.MenuRole.AboutRole)  # on macOS Qt moves it into the application menu, where About is
        info.triggered.connect(lambda _checked=False: self._show_about())
        self.help_menu = menu

    def _open_url(self, url: str) -> None:
        """A web page in the default browser: the manual."""
        QDesktopServices.openUrl(QUrl(url))

    def _show_shortcuts(self) -> None:
        """The keyboard drawing in a window of its own beside the main one, so it can stay open while working."""
        if self._shortcuts is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("Keyboard Shortcuts")
            drawing = QSvgWidget(dialog)
            drawing.load(QByteArray(keyboard.svg().encode()))  # drawn now, from the window's own keys (keyboard.py)
            size = drawing.renderer().defaultSize()
            screen = self.screen().availableGeometry() if self.screen() is not None else None
            scale = min(1.0, 0.9 * screen.width() / size.width()) if screen is not None and size.width() else 1.0
            drawing.setFixedSize(size * scale)  # its own proportions: drawn to scale, never stretched
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(drawing)
            self._shortcuts = dialog
        self._shortcuts.show()
        self._shortcuts.raise_()
        self._shortcuts.activateWindow()

    def _update_help(self, now) -> None:
        """The doctor takes a shot with the hardware thread's camera: only while that thread is free."""
        self.doctor_action.setEnabled(now.idle and not self._doctor_running)

    def _run_doctor(self) -> None:
        """The report in a window of its own; asked again, the same window with a new report."""
        if self._doctor is None:
            self._doctor = DoctorDialog(self)
        self._doctor.start()
        self._doctor.show()
        self._doctor.raise_()
        self._doctor.activateWindow()
        self._doctor_running = True
        self._update_buttons()
        self.ask_doctor.emit()

    def _on_doctor_line(self, line: str) -> None:
        if self._doctor is not None:
            self._doctor.add(line)

    def _on_doctor_done(self, decodes: bool) -> None:
        self._doctor_running = False
        if self._doctor is not None:
            self._doctor.finish(decodes)
        self._update_buttons()

    def _show_about(self) -> None:
        rows = "".join(f"<tr><td>{escape(label)}&nbsp;&nbsp;</td><td>{escape(value)}</td></tr>" for label, value in about.details())
        html = (
            f"<b>TetherInvert {escape(__version__)}</b><br>Tethered film scanning and conversion, built around single-channel RGB scanning."
            f"<br><br><table>{rows}</table><br><a href=\"{about.REPOSITORY}\">{about.REPOSITORY}</a>"
        )
        self._ask_about(html, about.text())

    def _ask_about(self, html: str, plain: str) -> None:
        """The About box. Copy details puts the plain text on the clipboard, for an issue."""
        box = QMessageBox(self)
        box.setWindowTitle("About TetherInvert")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.setText(html)
        copy = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy:
            QApplication.clipboard().setText(plain)

