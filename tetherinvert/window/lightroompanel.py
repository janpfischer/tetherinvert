"""Lightroom Classic watching the roll's folders as hot folders (lightroom.py and its plug-in), from
a popover at the rail's foot; its dot is green while Lightroom watches, grey otherwise.

The scans/ folder is handed over during a session only, prints/ of any
roll. `_lightroom_folders` is the one place that rule lives. What Lightroom watches is what its
plug-in last answered to this window's request (`_watched`), not read at start: status.txt still
says watching after Lightroom quit. *Stop watching* is a request without folders, as the plug-in's
own menu command makes; it does not start Lightroom, and a Lightroom that does not answer it is not
running, so it watches nothing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from .. import lightroom, paths
from ..session import Roll
from . import style

if TYPE_CHECKING:
    from .mainwindow import Now

OPEN_WAIT_S = 120.0  # for an answer to Open: a cold start of Lightroom takes a while
STOP_WAIT_S = 10.0  # for an answer to Stop watching: a Lightroom that is running picks a request up within 2 s
NO_ANSWER = (
    "Lightroom Classic did not pick up the request. In Lightroom Classic, File → Plug-in Manager should list "
    "“TetherInvert hot folder”, enabled. More in the log."
)


class LightroomPanel(QWidget):
    """What Lightroom watches (`status`, the chip beside the popover's title, and `lightroom_note`),
    the folders the button hands over, the button, and Stop watching."""

    def __init__(self) -> None:
        super().__init__()
        self.status = style.label("", "chip", wrap=False)
        self.lightroom_note = style.label("", "muted")
        self.lightroom_scans = QCheckBox("Scans")
        self.lightroom_scans.setToolTip("The roll's scans/ folder: the flat files as they are scanned. Watched during a session only.")
        self.lightroom_prints = QCheckBox("Prints")
        self.lightroom_prints.setToolTip("The roll's prints/ folder, at any time.")
        self.lightroom_button = QPushButton("Open in Lightroom Classic")
        self.lightroom_button.setMinimumHeight(36)
        self.lightroom_button.setToolTip(
            "Opens Lightroom Classic with the ticked folders of this roll as hot folders: every TIFF written there is added to the catalog, where it is."
        )
        self.stop_button = QPushButton("Stop watching")
        style.set_role(self.stop_button, "danger")
        self.stop_button.setFlat(True)
        self.stop_button.setToolTip("Lightroom Classic stops adding files; those already added stay in the catalog")
        boxes = QHBoxLayout()
        boxes.addWidget(self.lightroom_scans)
        boxes.addWidget(self.lightroom_prints)
        boxes.addStretch(1)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.lightroom_note)
        column.addLayout(boxes)
        column.addWidget(style.label("Scans are watched during a session only; prints at any time.", "muted"))
        column.addWidget(self.lightroom_button)
        column.addWidget(self.stop_button)


def what(folders: list[Path]) -> str:
    """"scans and prints": the roll's folders by their own names."""
    return " and ".join(folder.name for folder in folders)


def roll_name(folder: Path) -> str:
    """A roll by the name it was given, which its folder need not have."""
    return Roll.open(folder).name if Roll.state(folder) == "existing" else folder.name


class LightroomPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_lightroom(self) -> None:
        self._lightroom_wait: Optional[tuple[str, float, bool]] = None  # a request Lightroom has not answered yet: token, until when, a stop
        self._lightroom_silent = False  # the last request to watch got no answer
        self._watched: list[Path] = []  # what the plug-in last said it watches, after a request from this window
        self._watched_roll = ""  # the name of the roll those folders are of
        self._lightroom_timer = QTimer(self)
        self._lightroom_timer.setInterval(2000)
        self._lightroom_timer.timeout.connect(self._check_lightroom)

    def _lightroom_folders(self, folder: Optional[Path], state: str) -> list[Path]:
        """The folders the button hands to Lightroom: those ticked that may be watched now. The
        scans while a session writes them; the prints of any roll."""
        if folder is None:
            return []
        in_session = self._in_session()
        watch = [folder / paths.FOLDERS["flat"]] if self.lightroom_panel.lightroom_scans.isChecked() and in_session else []
        if self.lightroom_panel.lightroom_prints.isChecked() and (in_session or state == "existing"):
            watch.append(folder / paths.FOLDERS["print"])
        return watch

    def _set_watched(self, folders: list[Path]) -> None:
        self._watched = folders
        self._watched_roll = roll_name(folders[0].parent) if folders else ""

    def _lightroom_state(self, watch: list[Path], now: "Now") -> tuple[str, str, str]:
        """The chip's text and tone, and the line under it: what Lightroom watches, as its plug-in
        last said, and what the button would ask for when that is something else."""
        if self._lightroom_wait is not None:
            return "Asking…", "warning", "Waiting for Lightroom Classic to pick up the request."
        if self._lightroom_silent:
            return "No answer", "danger", NO_ANSWER
        name = now.roll.name if now.roll is not None else now.folder.name if now.folder is not None else ""
        if self._watched:
            mine = now.folder is not None and all(folder.parent.resolve() == now.folder.resolve() for folder in self._watched)
            if mine:
                note = f"Lightroom adds {name}'s {what(self._watched)} as they appear."
                if watch and [p.resolve() for p in watch] != [p.resolve() for p in self._watched]:
                    note += f" Open asks for its {what(watch)}."
                return "Watching", "ok", note
            note = f"Lightroom adds {self._watched_roll}'s {what(self._watched)} as they appear" + (f", not {name}'s." if name else ".")
            if watch:
                note += f" Open hands it {name}'s {what(watch)} instead."
            return f"Watching {self._watched_roll}", "ok", note
        if watch:
            return "Not watching", "off", f"Open hands Lightroom {name}'s {what(watch)}: it adds every TIFF written there to the catalog, where it is."
        if now.state != "existing" and not now.in_session:
            return "Not watching", "off", "Lightroom watches a roll that exists: its prints at any time, its scans during a session."
        return "Not watching", "off", "Tick Prints, or Scans during a session."

    def _open_lightroom(self) -> None:
        watch = self._lightroom_folders(self._roll_folder(), Roll.state(self._roll_folder()))
        if not watch or self._lightroom_wait is not None:
            return
        try:
            if lightroom.plugin_state() == "missing" and not self._confirm(
                "Lightroom Classic",
                "TetherInvert hands its scans to Lightroom Classic through a small plug-in. "
                f"Install it into Lightroom's plug-in folder?\n\n{lightroom.modules_folder() / lightroom.PLUGIN.name}",
            ):
                return
            token, notes = lightroom.open_hot_folder(watch)
        except lightroom.LightroomError as exc:
            self._on_failed(str(exc))
            return
        for note in notes:
            self._say(note)
        self._wait_for_lightroom(token, OPEN_WAIT_S, stopping=False)

    def _stop_lightroom(self) -> None:
        """Stop watching: a request without folders. Lightroom is not started for it."""
        if not self._watched or self._lightroom_wait is not None:
            return
        try:
            token = lightroom.request()
        except OSError as exc:
            self._on_failed(f"Lightroom Classic could not be asked to stop watching: {exc}")
            return
        self._wait_for_lightroom(token, STOP_WAIT_S, stopping=True)

    def _wait_for_lightroom(self, token: str, seconds: float, stopping: bool) -> None:
        self._lightroom_wait = (token, time.monotonic() + seconds, stopping)
        self._lightroom_silent = False
        self._lightroom_timer.start()
        self._update_buttons()

    def _check_lightroom(self) -> None:
        if self._lightroom_wait is None:
            self._lightroom_timer.stop()
            return
        token, deadline, stopping = self._lightroom_wait
        if lightroom.answered(token):
            seen = lightroom.status() or {}
            self._set_watched([Path(folder) for folder in seen.get("folders", [])] if seen.get("watching") else [])
            if stopping and not self._watched:
                self._say("Lightroom Classic stopped watching.")
            elif stopping:
                self._on_failed(f"Lightroom Classic answered, but is still watching {lightroom.names(self._watched)}.")
            elif self._watched:
                self._say(f"Lightroom Classic is watching {lightroom.names(self._watched)}.")
            else:
                self._on_failed("Lightroom Classic answered, but is not watching the folders. See " + str(lightroom.exchange() / "plugin.log"))
        elif time.monotonic() < deadline:
            return
        elif stopping:
            self._set_watched([])  # a Lightroom that is not running watches nothing
            self._say("Lightroom Classic did not answer: it is not running, so it watches nothing.")
        else:
            self._set_watched([])  # whatever it watched, it is not known any more
            self._lightroom_silent = True
            self._on_failed(lightroom.no_answer())
        self._lightroom_wait = None
        self._lightroom_timer.stop()
        self._update_buttons()

    def _update_lightroom(self, now: "Now") -> None:
        panel = self.lightroom_panel
        waiting = self._lightroom_wait is not None
        panel.lightroom_scans.setEnabled(now.in_session and not waiting)
        panel.lightroom_prints.setEnabled(not waiting)
        watch = self._lightroom_folders(now.folder, now.state)
        panel.lightroom_button.setEnabled(bool(watch) and not waiting)
        panel.stop_button.setEnabled(bool(self._watched) and not waiting)
        chip, tone, note = self._lightroom_state(watch, now)
        panel.status.setText(chip)
        style.set_tone(panel.status, tone)
        panel.lightroom_note.setText(note)
        self.rail.buttons["lightroom"].set_dot("ok" if self._watched else "off")
