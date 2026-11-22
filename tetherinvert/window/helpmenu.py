"""The Help menu: the manual, the keyboard shortcuts drawn on a keyboard (keyboard.py), About.

On macOS the menu bar is the screen's; About moves into the application menu there.
"""

from __future__ import annotations

from html import escape
from typing import Optional

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QVBoxLayout

from .. import __version__, about, keyboard


class HelpPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_help(self) -> None:
        self._shortcuts: Optional[QDialog] = None  # the keyboard drawing's window, once opened

    def _build_menu(self) -> None:
        """The menu bar (on macOS the one at the top of the screen): Help with the manual, the shortcuts, About."""
        menu = self.menuBar().addMenu("Help")
        manual = menu.addAction("TetherInvert Manual")
        manual.setShortcut(QKeySequence.StandardKey.HelpContents)  # Cmd+? on macOS
        manual.triggered.connect(lambda _checked=False: self._open_url(about.MANUAL_URL))
        menu.addAction("Keyboard Shortcuts").triggered.connect(lambda _checked=False: self._show_shortcuts())
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

