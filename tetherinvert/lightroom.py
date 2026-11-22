"""Lightroom Classic as a hot folder on the roll.

Lightroom's own Auto Import cannot do this: its watched folder has to be empty, it moves the
files away, and it is set up by hand inside Lightroom. So TetherInvert brings a small plug-in
(tetherinvert.lrplugin, next to this file). The two talk through three small files in
~/.tetherinvert/lightroom:

    request.txt   written here: a token, then one folder to watch per line (no folder: stop)
    status.txt    written by the plug-in: what it watches, and the token it answered
    plugin.log    written by the plug-in

The plug-in adds every finished TIFF of those folders to the catalog, where it is. Nothing is
moved or copied. It lists each folder itself only, so the RAW files in raw/ stay out. A roll's
scans/ and prints/ are watched together; in Lightroom they are two folders of the roll.
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

APP = "Adobe Lightroom Classic"
PLUGIN = Path(__file__).with_name("tetherinvert.lrplugin")


class LightroomError(RuntimeError):
    pass


def exchange() -> Path:
    """Where TetherInvert and the plug-in leave each other notes. Fixed: the plug-in knows no other place."""
    return Path.home() / ".tetherinvert" / "lightroom"


def modules_folder() -> Path:
    """The folder Lightroom Classic loads plug-ins from by itself when it starts."""
    if sys.platform != "darwin":
        raise LightroomError("Opening Lightroom Classic from TetherInvert works on macOS only.")
    return Path.home() / "Library" / "Application Support" / "Adobe" / "Lightroom" / "Modules"


def plugin_state(modules: Optional[Path] = None) -> str:
    """'missing', 'outdated' or 'current': the installed plug-in compared with the one shipped here."""
    installed = (modules or modules_folder()) / PLUGIN.name
    if not installed.is_dir():
        return "missing"
    names = sorted(path.name for path in PLUGIN.glob("*.lua"))
    _, differing, errors = filecmp.cmpfiles(PLUGIN, installed, names, shallow=False)
    return "outdated" if differing or errors else "current"


def install_plugin(modules: Optional[Path] = None) -> tuple[Path, str]:
    """Put the plug-in where Lightroom Classic finds it. Returns its path and what was done:
    'installed', 'updated' or 'current'."""
    modules = modules or modules_folder()
    installed, before = modules / PLUGIN.name, plugin_state(modules)
    if before == "current":
        return installed, "current"
    try:
        installed.mkdir(parents=True, exist_ok=True)
        for source in PLUGIN.glob("*.lua"):
            shutil.copyfile(source, installed / source.name)
    except OSError as exc:
        raise LightroomError(f"The plug-in could not be written to {installed}: {exc}") from exc
    return installed, "installed" if before == "missing" else "updated"


def request(folders: Sequence[Path] = ()) -> str:
    """Ask the plug-in to watch `folders` (none: to stop). Returns the token it will answer with."""
    token = f"tetherinvert-{time.time_ns()}"
    target = exchange() / "request.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_text("".join(f"{line}\n" for line in [token, *(Path(folder).resolve() for folder in folders)]), encoding="utf-8")
    partial.replace(target)  # the plug-in never reads half a request
    return token


def status() -> Optional[dict]:
    """What the plug-in last reported, or None when it never has. Its lines: watching or idle,
    files added, plug-in version, the token answered, then one watched folder per line.
    Raises ValueError for a status that is not in this form (an older plug-in still
    running: the update takes effect when Lightroom is restarted)."""
    try:
        lines = (exchange() / "status.txt").read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    lines += [""] * 4
    return {
        "watching": lines[0] == "watching", "added": int(lines[1] or 0), "version": lines[2], "token": lines[3],
        "folders": [line for line in lines[4:] if line],
    }  # fmt: skip


def answered(token: str) -> bool:
    """True once the plug-in has picked up the request with this token."""
    try:
        seen = status()
    except ValueError:
        return False
    return seen is not None and seen["token"] == token


def launch() -> None:
    """Start Lightroom Classic, or bring it to the front."""
    if sys.platform != "darwin":
        raise LightroomError("Opening Lightroom Classic from TetherInvert works on macOS only.")
    try:
        done = subprocess.run(["open", "-a", APP], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LightroomError(f"{APP} could not be started: {exc}") from exc
    if done.returncode != 0:
        raise LightroomError(f"{APP} could not be started: {done.stderr.strip() or 'is it installed?'}")


def names(folders: Sequence[Path]) -> str:
    """Folders as the operator knows them: "Roll15/scans and Roll15/prints"."""
    short = [f"{Path(folder).parent.name}/{Path(folder).name}" for folder in folders]
    return ", ".join(short[:-1]) + " and " + short[-1] if len(short) > 1 else "".join(short)


def open_hot_folder(folders: Sequence[Path]) -> tuple[str, list[str]]:
    """Everything the button does: plug-in in place, request written, Lightroom in front.
    Returns the request's token and what to tell the operator."""
    folders = [Path(folder) for folder in folders]
    if not folders:
        raise LightroomError("Name at least one folder for Lightroom Classic to watch.")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)  # Lightroom can watch it before the first file
    installed, did = install_plugin()
    notes = []
    if did == "installed":
        notes.append(
            f"The TetherInvert plug-in was installed for Lightroom Classic ({installed}). "
            "If Lightroom Classic was already open, restart it once so it loads the plug-in."
        )
    elif did == "updated":
        notes.append("The TetherInvert plug-in for Lightroom Classic was updated. If Lightroom Classic was already open, restart it once so it loads the new version.")
    token = request(folders)
    launch()
    notes.append(f"Lightroom Classic was asked to watch {names(folders)}: every finished TIFF is added to the catalog, where it is.")
    return token, notes


NO_ANSWER = (
    "Lightroom Classic has not picked up the request. In Lightroom Classic, open File → Plug-in Manager: "
    "'TetherInvert hot folder' should be listed and enabled. If it is missing, press Add there and choose {plugin}. "
    "The plug-in's own notes are in {log}."
)


def no_answer() -> str:
    try:
        plugin = modules_folder() / PLUGIN.name
    except LightroomError:
        plugin = PLUGIN
    return NO_ANSWER.format(plugin=plugin, log=exchange() / "plugin.log")
