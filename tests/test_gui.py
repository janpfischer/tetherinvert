"""Drives the real window on the simulated rig, without a screen."""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest
import tifffile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QImage, QKeyEvent  # noqa: E402
import sys  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tetherinvert import invert, sim  # noqa: E402
from tetherinvert.session import PresetStore, Roll  # noqa: E402
from tetherinvert.window.viewer import LIVE, Request  # noqa: E402
from tetherinvert.wizard import DONE, FAILED  # noqa: E402


def brightness(image) -> float:
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    rows = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return float(rows[:, : 3 * image.width()].mean())


def below_menu(window) -> int:
    """The window's height without its menu bar. On macOS the menu bar is at the top of the screen and
    the window is this tall; offscreen (and on Linux) the bar sits inside the window as well."""
    bar = window.menuBar()
    return window.height() - (bar.height() if bar.isVisible() and bar.parentWidget() is window else 0)


def wait(app, condition, seconds: float = 120.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the window")
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def first_frame_skipped(monkeypatch):
    """A new roll's first scan asks which number the film gives the frame; unless a test answers
    otherwise, it is skipped, so the frames count from 1."""
    from tetherinvert.window.mainwindow import MainWindow

    monkeypatch.setattr(MainWindow, "_ask_first_number", lambda self: 1)


@pytest.fixture(autouse=True)
def own_settings(tmp_path, monkeypatch):
    """The window's settings in an INI file under tmp_path. On macOS QSettings goes to the
    system preferences whatever HOME or XDG_CONFIG_HOME say, so without this the tests would
    write into the operator's own tetherinvert-sim settings, never into tmp_path."""
    from PySide6.QtCore import QSettings

    from tetherinvert.window import mainwindow

    monkeypatch.setattr(mainwindow, "QSettings", lambda org, app: QSettings(str(tmp_path / "config" / f"{app}.ini"), QSettings.Format.IniFormat))


def test_a_whole_session_in_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("TETHERINVERT_SIM_MANUAL_LENS", "1")  # a lens whose aperture the camera cannot read
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    from tetherinvert import lightroom
    from tetherinvert.window.mainwindow import MainWindow

    # Lightroom Classic is not here: its plug-in folder is a scratch folder, starting it is noted.
    launched = []
    monkeypatch.setattr(lightroom, "modules_folder", lambda: tmp_path / "Modules")
    monkeypatch.setattr(lightroom, "launch", lambda: launched.append(1))

    # The operator's answers to the window's questions; those before a session are on the Roll page.
    prompts = []
    monkeypatch.setattr(MainWindow, "_confirm", lambda self, title, text: prompts.append(text) or True)

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    roll = tmp_path / "scans" / "Test"
    idle = lambda: not window._busy and window._pending == 0 and not window._flat_capturing()  # noqa: E731
    try:
        wait(app, lambda: window.view.image is not None and "iso" in window._camera)
        assert window.devices_panel.camera_label.text() == "Simulated Camera" and "Scanlight" in window.devices_panel.light_label.text()
        assert "aperture on the lens" in window.scan_page.exposure_label.text()
        assert not window.roll_page.start_button.isEnabled() and not window.lightroom_panel.lightroom_button.isEnabled()  # only a session has a roll to hand to Lightroom
        presets = window.presets_page
        assert presets.preset_list.names() == [] and presets.empty_note.isVisibleTo(presets) and presets.recalibrate_button.isHidden()
        assert window.view.hidden == {"base"} and window.live_page.base_tool.isHidden() and window.view.tool == "picture"
        window.view.rects["picture"] = sim.PICTURE_RECT
        window.roll_page.folder_edit.setText(str(tmp_path / "scans"))
        window.roll_page.roll_edit.setText("Test")

        # A new preset through the wizard. W1: the film. The session and the roll wait meanwhile.
        window.rail.buttons["presets"].click()
        assert presets.isVisible() and not window.view.isVisible()  # the list has no live view
        window.activateWindow()  # shortcuts fire only in the active window, once it is
        app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert window._step == "scan" and window.view.isVisible()  # Esc on the list: Scan, as on Roll and Finish
        window.rail.buttons["presets"].click()
        presets.new_preset_button.click()
        panel = window.wizard_column.wizard_panel
        assert panel.isVisible() and window.view.isVisible() and not presets.isVisible() and window.rail.buttons["presets"].isChecked()  # beside the live view
        QTest.keyClick(window, Qt.Key.Key_Home)
        assert window._step == "presets" and panel.isVisible()  # the wizard keeps the live view: Presets stays
        assert not window.roll_page.start_button.isEnabled() and not window.roll_page.roll_edit.isEnabled() and not window.strip.isVisible()  # no roll's frames
        assert panel.position.text() == "Step 1 of 4 · Film" and not panel.next_button.isEnabled() and "Name the film" in panel.missing.text()
        panel.name_edit.setText("Portra")
        assert panel.kind_buttons["rgb"].isChecked() and panel.next_button.isEnabled()
        panel.next_button.click()
        # W2: the camera cannot read the f-stop of this lens, so it is asked for; ISO it reports.
        assert panel.position.text() == "Step 2 of 4 · Camera" and panel.heading.text() == "New preset · Portra · colour"
        assert panel.fields["aperture"].isVisible() and panel.fields["iso"].isHidden() and "not reported by the lens" in panel.camera_rows["aperture"].text()
        assert not panel.next_button.isEnabled() and panel.missing.text() == "Fill in the f-stop."  # no preset to fill it in from
        panel.fields["aperture"].setText("8")
        panel.next_button.click()
        # W3: the film base rectangle, drawn with the mouse as a user would; its tool exists in this step only.
        assert window.live_page.base_tool.isVisible() and window.view.hidden == set() and window.view.tool == "base"
        assert panel.next_button.text() == "Calibrate" and not panel.next_button.isEnabled()
        area = window.view._area()
        x, y, w, h = sim.BASE_ROI
        corner = lambda fx, fy: QPoint(int(area.x() + fx * area.width()), int(area.y() + fy * area.height()))  # noqa: E731
        QTest.mousePress(window.view, Qt.MouseButton.LeftButton, pos=corner(x, y))
        QTest.mouseMove(window.view, corner(x + w, y + h))
        QTest.mouseRelease(window.view, Qt.MouseButton.LeftButton, pos=corner(x + w, y + h))
        drawn = window.view.rects["base"]
        assert drawn is not None and abs(drawn[0] - x) < 0.01 and abs(drawn[3] - h) < 0.01
        assert panel.next_button.isEnabled() and "✓" in panel.rect_check.text()
        # W4: calibrating, a histogram after every shot; the preset is saved, the f-stop as given.
        panel.next_button.click()
        assert window.live_page.base_tool.isHidden() and window.view.hidden == {"base"} and panel.stop_button.isVisible() and not panel.cancel_button.isEnabled()
        wait(app, lambda: window._wizard.histogram()[0])
        wait(app, lambda: window._wizard.state == DONE and idle())
        preset = PresetStore().get("Portra")
        assert preset.aperture == "f/8" and preset.by_hand == ("aperture",)
        assert sorted(panel.histogram.histograms) == [0, 1, 2] and all(t.startswith("✓") for t in (label.text() for label in panel.stage_labels))
        assert "Portra is calibrated and saved" in panel.result.text() and "f/8 by hand" in panel.result.text() and "Nothing clipped" in panel.result.text()
        assert panel.next_button.text() == "Finish" and panel.cancel_button.isHidden() and panel.stop_button.isHidden()
        panel.next_button.click()
        assert window._wizard is None and not panel.isVisible() and presets.isVisible() and presets.preset_list.chosen() == "Portra"  # back to the list
        assert "Aperture: f/8 — set this yourself" in presets.settings_lines() and presets.settings_lines()[1].startswith("Shutter: ")
        assert presets.kind_label.text() == "Colour: R, G, B one by one (3 shots)" and presets.recalibrate_button.isEnabled() and presets.delete_button.isEnabled()
        assert PresetStore().picture("Portra").exists() and presets.base_picture.image is not None  # the film base as it was metered
        assert window.view.hidden == {"base"} and window.live_page.base_tool.isHidden() and window.view.tool == "picture"

        # No scanning outside a session.
        assert not window.scan_page.scan_button.isEnabled() and window.roll_page.start_button.isEnabled()
        assert window.roll_page.start_button.text() == "Start new session" and "New roll “Test” with preset Portra" in window.roll_page.note.text()
        page = window.roll_page
        assert "Check on the camera or lens: <b>Aperture</b>" in page.start_note.text()
        # A metadata field that is not taken: the start is refused, and a start refused takes no name.
        page.metadata_form.edits["shot_from"].setText("3.10.2026")
        page.start_button.click()
        assert window._session is None and not page.metadata_form.problem.isHidden() and not roll.exists()
        page.metadata_form.edits["shot_from"].setText("")

        # Starting the session opens Scan on its first step, the flat field: nothing is shot until Capture.
        page.start_button.click()
        scan = window.scan_page
        assert window._session is not None and window._flat_pending() and window.rail.buttons["scan"].isChecked() and roll.exists()
        assert scan.flat_button.isVisible() and scan.flat_button.text() == "Capture flat field" and scan.without_button.isVisible()
        assert scan.keep_flat_button.isHidden() and scan.scan_button.isHidden() and scan.counter.isHidden()  # a new roll has no flat field to keep
        assert "Take the film out of the holder" in scan.flat_text.text() and not (roll / "flatfield.npz").exists()
        assert not page.start_button.isEnabled() and not page.roll_edit.isEnabled()  # the session runs: its roll is held
        scan.flat_button.click()
        wait(app, lambda: not window._flat_pending() and idle())
        assert (roll / "flatfield.npz").exists() and "Flat field captured" in window.log_bar.text()
        assert scan.flat_button.isHidden() and scan.film_back.isVisible() and "Put the film back in the holder" in window.log_bar.text()
        assert window.rail.buttons["scan"].isChecked() and window.scan_page.end_button.isVisible() and "Aperture: f/8" in window.scan_page.session_label.text()
        assert window.strip.isVisible()  # back under the live view after the wizard
        assert window.scan_page.scan_button.isEnabled() and not presets.preset_list.isEnabled() and not window.roll_page.roll_edit.isEnabled()
        assert not presets.new_preset_button.isEnabled() and not presets.recalibrate_button.isEnabled() and not presets.start_button.isEnabled()
        assert not window.scan_page.retake_button.isEnabled()  # nothing scanned yet
        # The film base rectangle belongs to calibration: gone for the length of the session.
        assert window.view.hidden == {"base"} and not window.live_page.base_tool.isVisible() and window.view.tool == "picture"
        assert window.view.rects["base"] is not None  # only hidden, not forgotten

        window.roll_page.compare_box.setChecked(True)  # each file twice: with and without the flat field
        window.scan_page.scan_button.click()
        wait(app, lambda: (roll / "scans" / "Test_001.tif").exists() and idle())
        assert scan.film_back.isHidden()  # the film is in: no more reminding
        assert (roll / "scans" / "Test_001_noflat.tif").exists() and "Test_001_noflat.tif" in window.log_bar.text()
        window.roll_page.compare_box.setChecked(False)

        # Lightroom Classic as a hot folder on the roll, its scans and its prints together: plug-in installed (after
        # asking), request left, Lightroom started.
        exchange = tmp_path / "userhome" / ".tetherinvert" / "lightroom"
        lightroom_panel = window.lightroom_panel
        assert lightroom_panel.lightroom_button.isEnabled() and lightroom_panel.lightroom_scans.isChecked() and lightroom_panel.lightroom_prints.isChecked()
        assert window.lightroom_panel.lightroom_note.text() == (
            "Open hands Lightroom Test's scans and prints: it adds every TIFF written there to the catalog, where it is."
        )
        # The devices are needed on every step during a session: Space scans from any of them.
        window.rail.buttons["finish"].click()
        assert window.rail.buttons["devices"].dot == "ok" and window.devices_panel.status.text() == "Ready"
        window.rail.buttons["scan"].click()
        window.lightroom_panel.lightroom_button.click()
        assert "Install it into Lightroom's plug-in folder" in prompts[-1] and (tmp_path / "Modules" / "tetherinvert.lrplugin" / "Info.lua").is_file()
        token, *asked_for = (exchange / "request.txt").read_text().split("\n")
        assert launched == [1] and asked_for == [str((roll / "scans").resolve()), str((roll / "prints").resolve()), ""]
        assert "asked to watch Test/scans and Test/prints" in window.log_bar.text() and (roll / "prints").is_dir()
        # ...and what the plug-in would answer.
        (exchange / "status.txt").write_text(f"watching\n1\n0.4.0\n{token}\n{roll / 'scans'}\n{roll / 'prints'}\n")
        wait(app, lambda: "Lightroom Classic is watching Test/scans and Test/prints." in window.log_bar.text(), 10)
        assert window.rail.buttons["lightroom"].dot == "ok"
        assert not window._lightroom_timer.isActive() and window.lightroom_panel.lightroom_note.text() == "Lightroom adds Test's scans and prints as they appear."
        window.lightroom_panel.lightroom_prints.click()
        assert window.lightroom_panel.lightroom_note.text() == "Lightroom adds Test's scans and prints as they appear. Open asks for its scans."
        window.lightroom_panel.lightroom_prints.click()
        window.scan_page.scan_button.click()
        wait(app, lambda: (roll / "scans" / "Test_002.tif").exists() and idle())
        assert window.scan_page.retake_button.isEnabled()
        window.scan_page.retake_button.click()
        wait(app, lambda: window._busy)
        wait(app, idle)
        saved = Roll.open(roll)
        assert [f.number for f in saved.frames] == [1, 2] and saved.flatfield and saved.rect == pytest.approx(list(sim.PICTURE_RECT))
        assert not saved.compare and not (roll / "scans" / "Test_002_noflat.tif").exists()
        assert "lowered from the preset" in window.log_bar.text()

        # The strip: the live view first, then the roll's frames; the last scan waits in the live view's corner.
        wait(app, lambda: set(window._thumbs) == {1, 2} and window.view.inset is not None, 30)
        assert window.strip.numbers() == [LIVE, 1, 2] and window.view.inset[0] == 2 and window._shown == LIVE
        assert [window.strip.item(row).text() for row in range(3)] == ["Live", "1 · to print", "2 · to print"]  # every frame gets a print
        # A click on a frame shows it large, as its print will be: flat, as scanned, by default.
        strip = window.strip
        QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=strip.visualItemRect(strip.item(1)).center())
        assert window.views.currentWidget() is window.frame_page and window.frame_page.frame_tools.isVisible() and not window.live_page.live_tools.isVisible()
        wait(app, lambda: window.frame_page.frame_info.text().startswith("Frame 1 · Flat ·"))
        assert window.frame_view.image.width() == 1600 and window.frame_view.outline == pytest.approx(list(sim.PICTURE_RECT))
        assert window.develop_column.base_buttons["flat"].isChecked() and all(button.isEnabled() for button in window.develop_column.base_buttons.values())
        # A double click: the print at 100 %, developed from the flat TIFF.
        QTest.mouseDClick(window.frame_view, Qt.MouseButton.LeftButton)
        wait(app, lambda: window.frame_view.full is not None)
        assert window.frame_view.zoomed and window.frame_view.full.width() == 3000
        # Esc goes home. Holding Tab shows the last frame, letting go comes back.
        window.activateWindow()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert window._shown == LIVE and window.view.isVisible()
        # Held as macOS sends it: one press, repeated presses, one release. The repeats must not
        # move the keyboard focus on (that makes the strip's selection blink on a real Mac).
        strip.setFocus()

        def tab(kind, repeat=False):
            QApplication.sendEvent(QApplication.focusWidget() or strip, QKeyEvent(kind, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier, "\t", repeat))

        tab(QEvent.Type.KeyPress)
        assert window._shown == 2 and window.views.currentWidget() is window.frame_page
        for _ in range(5):
            tab(QEvent.Type.KeyPress, repeat=True)
        assert window._shown == 2 and QApplication.focusWidget() is strip
        tab(QEvent.Type.KeyRelease)
        assert window._shown == LIVE and QApplication.focusWidget() is strip
        # On a frame, Tab goes back to the live view and stays there, held or not; nothing is scanned.
        frames = len(Roll.open(roll).frames)
        for repeats in (0, 5):
            window._show(1)
            tab(QEvent.Type.KeyPress)
            assert window._shown == LIVE
            for _ in range(repeats):
                tab(QEvent.Type.KeyPress, repeat=True)
            tab(QEvent.Type.KeyRelease)
            assert window._shown == LIVE and QApplication.focusWidget() is strip and not window._busy
        assert len(Roll.open(roll).frames) == frames
        # The inset opens its frame on Develop. Rescan frame 2 from its frame bar, where it is seen: the
        # live view comes up with Replace frame 2, and scanning goes on from the live view.
        QTest.mouseClick(window.view, Qt.MouseButton.LeftButton, pos=window.view._inset_area().center().toPoint())
        rescan = window.frame_page.rescan_button
        assert window._shown == 2 and window.develop_column.isVisible() and rescan.isVisible() and rescan.text() == "Rescan frame 2"
        rescan.click()
        assert window._shown == LIVE and window.scan_page.scan_button.text() == "Replace frame 2"
        window.scan_page.scan_button.click()
        assert "Scan frame 2 again?" in prompts[-1] and window.view.inset is None
        wait(app, lambda: strip.item(2).text() == "2 · rendering")  # shot, now being written: no print due until it can be printed
        wait(app, lambda: idle() and 2 in window._thumbs and window.view.inset is not None)
        assert strip.item(2).text() == "2 · to print"

        # Rendered again from the RAW files, with the linear negative and comparison copies.
        window.roll_page.negative_box.setChecked(True)
        window.roll_page.compare_box.setChecked(True)
        window.finish_page.roll_button.click()
        wait(app, lambda: (roll / "negative" / "Test_002.tif").exists() and idle())
        assert all((roll / folder / f"Test_00{n}_noflat.tif").exists() for n in (1, 2) for folder in ("scans", "negative"))
        assert "Rendered 8 files again from the RAW files" in window.log_bar.text() and window.view.focus > 0

        # Corrections, SP-3000 style: made on the preview at once, printed in a batch.
        QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=strip.visualItemRect(strip.item(1)).center())
        wait(app, lambda: window.frame_page.frame_info.text().startswith("Frame 1 · Flat ·") and window.develop_column.isVisible())
        assert window.finish_page.print_button.text() == "Print 2 frames"
        plain = brightness(window.frame_view.image)
        for _ in range(3):
            QTest.keyClick(strip, Qt.Key.Key_Q)  # D +: denser; a held key arrives like this too
        QTest.keyClick(strip, Qt.Key.Key_D)  # M -
        QTest.keyClick(strip, Qt.Key.Key_G, Qt.KeyboardModifier.ShiftModifier)  # contrast the other way: softer
        assert window._corrections.of(1) == {**invert.NEUTRAL, "d": 3, "m": -1, "grad": -1}
        wait(app, lambda: brightness(window.frame_view.image) < plain - 2)  # always shown: the picture is the print
        # While a key is held a newer picture is always asked for before the last arrives: every
        # picture newer than the one on screen is shown, or nothing would move until the key is let go.
        seq, dev = window._seq, window._developed(1, shown=True)
        window._seq = seq + 5
        held, older = window.frame_view.image.copy(), window.frame_view.image.copy()
        window._on_picture(Request(dev, seq + 1), held, None)
        assert window._shown_seq == seq + 1 and window.frame_view.image is held
        window._on_picture(Request(dev, seq), older, None)
        assert window._shown_seq == seq + 1 and window.frame_view.image is held  # an older one never replaces it
        assert window.develop_column.values["d"].text() == "+3" and window.develop_column.values["grad"].text() == "-1"
        QTest.keyPress(strip, Qt.Key.Key_C)  # held: without its corrections, to compare
        wait(app, lambda: "without its corrections (C held)" in window.frame_page.frame_info.text())
        assert abs(brightness(window.frame_view.image) - plain) < 1
        QTest.keyRelease(strip, Qt.Key.Key_C)
        wait(app, lambda: "without its corrections" not in window.frame_page.frame_info.text())
        # Saved a moment later; the strip says so.
        wait(app, lambda: Roll.open(roll).frame(1).adjust == window._corrections.of(1))
        assert strip.item(1).text() == "1 · to print\nD+3 M-1 Ct-1"
        # Another base for this frame: ↑ and ↓, round the bases.
        QTest.keyClick(strip, Qt.Key.Key_Down)
        assert window._developments[1].base == "converted" and Roll.open(roll).frame(1).develop == "converted"
        assert window.develop_column.base_buttons["converted"].isChecked()
        wait(app, lambda: window.frame_page.frame_info.text().startswith("Frame 1 · Converted ·"))
        assert strip.item(1).text() == "1 · to print\nConv D+3 M-1 Ct-1"
        # Hold carries the corrections to the next frame; X takes them back there.
        QTest.keyClick(strip, Qt.Key.Key_H)
        QTest.keyClick(strip, Qt.Key.Key_Right)  # the next frame: the strip runs left to right
        assert window._shown == 2 and window._corrections.of(2) == window._corrections.of(1) and window._developments[2].base == "flat"
        QTest.keyClick(strip, Qt.Key.Key_X)
        QTest.keyClick(strip, Qt.Key.Key_H)
        assert window._corrections.of(2) is None and window._corrections.hold is None
        window.finish_page.print_button.click()
        wait(app, lambda: (roll / "prints" / "Test_002.tif").exists() and idle())
        wait(app, lambda: window.finish_page.print_button.text() == "Prints up to date")
        assert "Wrote 2 prints." in window.log_bar.text() and strip.item(2).text() == "2"
        with tifffile.TiffFile(roll / "prints" / "Test_001.tif") as tif:
            assert "Test_001.tif as converted (its own points), D+3 C+0 M-1 Y+0, contrast -1" in tif.pages[0].description
        # The roll matched: every frame developed with the same points, every print to be done again.
        window.develop_column.match_box.click()
        assert Roll.open(roll).matched and window.finish_page.print_button.text() == "Print 2 frames"
        wait(app, lambda: "matched" in window.frame_page.frame_info.text())
        QTest.keyClick(strip, Qt.Key.Key_Escape)

        shot = os.environ.get("TETHERINVERT_SCREENSHOT")
        if shot:
            app.processEvents()
            window.grab().save(shot)

        # Cleaning up: the RAW files go, the TIFFs stay, and rendering again is no longer offered.
        assert window.finish_page.clean_button.isEnabled()
        window.finish_page.clean_button.click()
        assert "Delete 6 RAW files" in prompts[-1] and not (roll / "raw").exists() and (roll / "scans" / "Test_001.tif").exists()
        assert not window.finish_page.clean_button.isEnabled() and not window.finish_page.roll_button.isEnabled()

        # Ending the session unlocks the preset; a new one can keep the roll's flat field.
        window.scan_page.end_button.click()
        assert window._session is None and presets.preset_list.isEnabled() and not window.scan_page.scan_button.isEnabled()
        window._show(2)  # outside a session a frame cannot be scanned again
        assert window.develop_column.isVisible() and not window.frame_page.rescan_button.isVisible()
        window._show_live()
        # Outside a session Lightroom may watch the roll's prints, never its scans.
        assert not lightroom_panel.lightroom_scans.isEnabled() and lightroom_panel.lightroom_scans.isChecked() and lightroom_panel.lightroom_button.isEnabled()
        window.lightroom_panel.lightroom_button.click()
        token, *asked_for = (exchange / "request.txt").read_text().split("\n")
        assert asked_for == [str((roll / "prints").resolve()), ""]
        assert not window.lightroom_panel.lightroom_prints.isEnabled()  # while Lightroom is being asked
        (exchange / "status.txt").write_text(f"watching\n1\n0.4.0\n{token}\n{roll / 'prints'}\n")
        wait(app, lambda: window.lightroom_panel.status.text() == "Watching", 10)
        window.lightroom_panel.lightroom_prints.click()
        assert not window.lightroom_panel.lightroom_button.isEnabled() and not window.lightroom_panel.lightroom_prints.isChecked()
        window.lightroom_panel.lightroom_prints.click()
        # New preset… opens the wizard again after a cancel; Cancel leaves as before.
        window.presets_page.new_preset_button.click()
        assert panel.isVisible() and not window.roll_page.start_button.isEnabled()
        panel.cancel_button.click()
        # The roll continues with its own preset.
        assert window._wizard is None and presets.isVisible() and presets.preset_list.chosen() == "Portra" and window.roll_page.start_button.isEnabled()
        # The folder is the session: the same roll name continues it, with its own preset.
        assert window.roll_page.start_button.text() == "Continue session" and Roll.open(roll).preset["name"] == "Portra"
        assert "Roll “Test”, 2 frames, preset Portra. Continue it" in window.roll_page.note.text()
        assert "The next frame is number 3." in page.start_note.text()
        window.roll_page.start_button.click()
        assert window._flat_pending() and scan.keep_flat_button.isVisible() and "Keep the flat field from" in scan.keep_flat_button.text()  # its own
        scan.keep_flat_button.click()
        assert not window._flat_pending() and "the flat field from" in scan.session_label.text() and Roll.open(roll).flatfield
        window.scan_page.scan_button.click()
        wait(app, lambda: (roll / "scans" / "Test_003.tif").exists() and idle())
    finally:
        window.close()
        app.processEvents()
    assert not window._hardware_thread.isRunning() and not window._render_thread.isRunning()
    assert Path(tmp_path / "config").exists()  # the window remembered its settings


def test_the_calibration_wizard_replaces_refuses_stops_and_recalibrates(tmp_path, monkeypatch):
    """The wizard's other ways: a taken name in another case, a rectangle off the film, Stop,
    Recalibrate…, and Cancel at every step, which writes nothing."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TETHERINVERT_SIM_MANUAL_LENS", "1")
    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.session import Preset

    store = PresetStore()
    store.save(Preset("gold200", "rgb", (200, 100, 90, 0), "1/4", iso="100", aperture="f/5.6", base=(58000.0, 58000.0, 58000.0),
                      created="2026-10-01 10:00", roi=sim.BASE_ROI, by_hand=("aperture",)))  # fmt: skip
    stored = lambda: store.path.read_bytes()  # noqa: E731
    before = stored()
    shot = os.environ.get("TETHERINVERT_SCREENSHOT")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.resize(1400, 900)
    window.show()
    panel = window.wizard_column.wizard_panel
    idle = lambda: not window._busy  # noqa: E731

    def draw(rect) -> None:  # as the mouse leaves it
        window.view.rects["base"] = rect
        window.view.changed.emit()

    try:
        wait(app, lambda: window.view.image is not None and "iso" in window._camera)
        # Cancel at each step before calibrating writes nothing.
        for steps in range(3):
            window.presets_page.new_preset_button.click()
            panel.name_edit.setText("Ektar")
            for _ in range(steps):
                panel.next_button.click()
            assert panel.position.text().startswith(f"Step {steps + 1} of 4")
            panel.cancel_button.click()
            assert window._wizard is None and stored() == before and window.presets_page.preset_list.chosen() == "gold200"

        # A taken name, in another case: said in the panel, with the film type it replaces.
        window.presets_page.new_preset_button.click()
        panel.name_edit.setText("GOLD200")
        assert "“gold200” exists" in panel.replace_note.text() and panel.replace_note.isVisible()
        panel.kind_buttons["white"].click()
        assert "It is for colour film; calibrating replaces it with a black and white preset" in panel.replace_note.text()
        panel.next_button.click()
        assert panel.fields["aperture"].text() == "f/5.6"  # from the preset calibrated last
        panel.back_button.click()
        assert panel.name_edit.text() == "GOLD200" and panel.kind_buttons["white"].isChecked()  # Back keeps what was entered
        panel.next_button.click()
        if shot:
            app.processEvents()
            window.grab().save(shot.replace(".png", "_w2.png"))
        assert below_menu(window) <= 900, "the wizard made the window taller than 900 px"
        panel.next_button.click()

        # A rectangle on the holder: W4 says why and what to do; Back goes to the rectangle.
        assert window.view.rects["base"] is None and not panel.next_button.isEnabled()
        draw((0.0, 0.0, 0.02, 0.02))
        panel.next_button.click()
        wait(app, lambda: window._wizard.state == FAILED and idle())
        assert "Too dark even at the longest exposure: open the aperture" in panel.problem.text() and panel.problem.isVisible() and stored() == before
        assert panel.back_button.isEnabled() and panel.cancel_button.isEnabled() and not panel.next_button.isVisible()
        panel.back_button.click()
        assert window.live_page.base_tool.isVisible() and panel.position.text() == "Step 3 of 4 · Film base"

        # Stop: the calibration ends at its next shot and saves nothing.
        draw(sim.BASE_ROI)
        panel.next_button.click()
        panel.stop_button.click()
        assert panel.stop_button.text() == "Stopping…" and not panel.stop_button.isEnabled()
        wait(app, lambda: window._wizard.state == FAILED and idle())
        assert "cancelled" in panel.problem.text() and stored() == before

        # Again, to the end: the new spelling replaces the old preset, which a roll would keep a copy of.
        panel.back_button.click()
        panel.next_button.click()
        wait(app, lambda: window._wizard.state == DONE and idle())
        if shot:
            app.processEvents()
            window.grab().save(shot.replace(".png", "_w4.png"))
        assert below_menu(window) <= 900
        assert "White " in panel.result.text() and sorted(panel.histogram.histograms) == [0, 1, 2]  # all three channels of the white shot
        panel.next_button.click()
        assert store.names() == ["GOLD200"] and store.get("gold200").kind == "white" and window.presets_page.preset_list.chosen() == "GOLD200"

        # Recalibrate…: from W2, name and film type fixed, the preset's own rectangle.
        draw(None)
        window.presets_page.recalibrate_button.click()
        assert panel.position.text() == "Step 1 of 3 · Camera" and panel.heading.text() == "Recalibrate · GOLD200 · black and white"
        assert panel.back_button.isHidden() and panel.fields["aperture"].text() == "f/5.6"
        assert window.view.rects["base"] == pytest.approx(sim.BASE_ROI)
        panel.cancel_button.click()
        assert store.get("GOLD200").created == PresetStore().get("GOLD200").created and window._wizard is None
    finally:
        window.close()
        app.processEvents()


def test_a_preset_lit_by_the_lights_colour_default_from_the_wizard_to_a_scan(tmp_path, monkeypatch):
    """W1 offers the Scanlight's three modes; with R, G, B together W4 finds the shutter only and
    shows the levels from the light; a session with it scans a frame in one exposure."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert import kinds
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    panel = window.wizard_column.wizard_panel
    roll = tmp_path / "scans" / "T"
    idle = lambda: not window._busy and window._pending == 0 and not window._flat_capturing()  # noqa: E731
    try:
        wait(app, lambda: window.view.image is not None and "iso" in window._camera)
        window.presets_page.new_preset_button.click()
        assert [button.text() for button in panel.kind_buttons.values()] == [kind.label for kind in kinds.KINDS.values()]
        assert [kind for kind, button in panel.kind_buttons.items() if not button.isHidden()] == ["rgb", "rgb-together", "white"]  # the Scanlight's three
        panel.name_edit.setText("Gold together")
        panel.kind_buttons["rgb-together"].click()
        panel.next_button.click()
        assert panel.heading.text() == "New preset · Gold together · one-shot colour"
        panel.next_button.click()
        window.view.rects["base"] = sim.BASE_ROI
        window.view.changed.emit()
        panel.next_button.click()
        wait(app, lambda: window._wizard.state == DONE and idle())
        shown = [label for label in panel.stage_labels if not label.isHidden()]
        assert len(shown) == 3 and all(label.text().startswith("✓") for label in shown) and "Trim" not in " ".join(label.text() for label in shown)
        assert "R 200 · G 170 · B 230 from the light" in panel.result.text() and sorted(panel.histogram.histograms) == [0, 1, 2]
        assert "factory default" not in panel.result.text()
        panel.next_button.click()
        assert "LEDs: R 200  G 170  B 230 together (the light's default)" in window.presets_page.settings_lines()
        assert window.presets_page.kind_label.text() == "Colour: the light's colour default (1 shot)"

        window.roll_page.folder_edit.setText(str(tmp_path / "scans"))
        window.roll_page.roll_edit.setText("T")
        window.roll_page.start_button.click()
        window.scan_page.flat_button.click()
        wait(app, lambda: not window._flat_pending() and idle())
        assert (roll / "flatfield.npz").exists() and "together" in window.scan_page.session_label.text()
        window.scan_page.scan_button.click()
        wait(app, lambda: (roll / "scans" / "T_001.tif").exists() and idle())
        assert sorted(path.name for path in (roll / "raw").iterdir()) == ["T_001_RGB.dng"] and Roll.open(roll).frame(1).kind == "rgb-together"
    finally:
        window.close()
        app.processEvents()


NOTE = "Kaiser slimlite plano, full"


def calibrate_in_the_wizard(app, window, name: str, kind: str, shoot=None) -> None:
    """W1 to W4 with an own light: W1 offers only Own light without a Scanlight, W2 wants the light noted."""
    panel = window.wizard_column.wizard_panel
    window.presets_page.new_preset_button.click()
    assert panel.light_buttons["Scanlight"].isHidden() and panel.light_buttons["Own light"].isChecked()
    assert panel.kind_buttons["rgb"].isHidden() and panel.kind_buttons["own-colour"].isVisible()
    panel.name_edit.setText(name)
    panel.kind_buttons[kind].click()
    panel.next_button.click()
    assert panel.light_box.isVisible() and "flicker" in panel.light_box.findChildren(type(panel.position))[-1].text()
    assert not panel.next_button.isEnabled() and panel.missing.text() == "Note which light it is and how it is set."
    panel.fields["light"].setText(NOTE)
    panel.next_button.click()
    window.view.rects["base"] = sim.BASE_ROI
    window.view.changed.emit()
    panel.next_button.click()
    if shoot:
        shoot(panel)
    wait(app, lambda: window._wizard.state == DONE and not window._busy)
    assert NOTE in panel.result.text()
    panel.next_button.click()
    assert f"Light: {NOTE} — set this yourself" in window.presets_page.settings_lines()


@pytest.mark.parametrize("kind", ["own-colour", "own-bw"])
def test_without_a_scanlight_a_session_scans_with_an_own_light(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TETHERINVERT_NO_LIGHT", "1")
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    roll = tmp_path / "scans" / "T"
    idle = lambda: not window._busy and window._pending == 0 and not window._flat_capturing()  # noqa: E731
    try:
        wait(app, lambda: window.view.image is not None and "iso" in window._camera and window._light_text == "none: own light")
        light = window.devices_panel.light_label
        assert light.text() == "not connected" and light.property("tone") == "danger"  # no preset yet: still the warning
        calibrate_in_the_wizard(app, window, "Own", kind)
        assert light.text() == "none: own light" and light.property("tone") == ""  # an own light needs none
        window.roll_page.folder_edit.setText(str(tmp_path / "scans"))
        window.roll_page.roll_edit.setText("T")
        window.roll_page.start_button.click()
        window.scan_page.flat_button.click()
        wait(app, lambda: not window._flat_pending() and idle())
        assert (roll / "flatfield.npz").exists() and window.scan_page.scan_button.isEnabled()
        window.scan_page.scan_button.click()
        wait(app, lambda: (roll / "scans" / "T_001.tif").exists() and idle())
        assert sorted(path.name for path in (roll / "raw").iterdir()) == ["T_001_O.dng"] and Roll.open(roll).frame(1).light == NOTE
    finally:
        window.close()
        app.processEvents()


def test_own_rgb_prompts_for_each_colour_at_calibration_flat_field_and_scan(tmp_path, monkeypatch):
    """The operator sets the light for every shot; Shoot or Space takes it; a wrong colour is asked for again."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TETHERINVERT_NO_LIGHT", "1")
    from tetherinvert import workers
    from tetherinvert.window.mainwindow import MainWindow

    operator, wrong = workers.set_by_hand, []

    def by_hand(camera, kind, letter):  # green asked, red shown, once at each prompt that sets wrong
        operator(camera, kind, "R" if wrong and letter == wrong[0] else letter)
        if wrong and letter == wrong[0]:
            wrong.pop()

    monkeypatch.setattr(workers, "set_by_hand", by_hand)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    roll = tmp_path / "scans" / "T"
    idle = lambda: not window._busy and window._pending == 0  # noqa: E731

    def shoot_colours(panel) -> None:
        assert panel.position.text() == "Step 4 of 7 · Red only" and panel.next_button.text() == "Shoot" and "red only" in panel.colour_prompt.text()
        panel.next_button.click()
        wait(app, lambda: window._wizard.step.key == "G" and idle())
        wrong.append("G")
        QTest.keyClick(window, Qt.Key.Key_Space)  # Space shoots too
        wait(app, lambda: window._wizard.shot_problem and idle())
        assert panel.colour_problem.text().startswith("That did not look like green only.") and window._wizard.step.key == "G"
        panel.next_button.click()
        wait(app, lambda: window._wizard.step.key == "B" and idle())
        panel.next_button.click()

    try:
        wait(app, lambda: window.view.image is not None and "iso" in window._camera)
        calibrate_in_the_wizard(app, window, "Gold own", "own-rgb", shoot_colours)
        shutters = PresetStore().get("Gold own").shutters
        assert sorted(shutters) == ["B", "G", "R"]
        assert f"Shutter: R {shutters['R']} · G {shutters['G']} · B {shutters['B']} s" in window.presets_page.settings_lines()  # a shutter per colour
        window.roll_page.folder_edit.setText(str(tmp_path / "scans"))
        window.roll_page.roll_edit.setText("T")
        window.roll_page.start_button.click()
        flat = window.scan_page.flat_button
        flat.click()  # the flat field: three prompted shots
        for letter in ("red", "green", "blue"):
            wait(app, lambda letter=letter: flat.text() == f"Shoot: {letter} only" and flat.isEnabled())
            assert window.scan_page.flat_cancel_button.isVisible() and window.scan_page.flat_cancel_button.text() == "Cancel flat field"
            flat.click()
        wait(app, lambda: not window._flat_pending() and idle())
        assert (roll / "flatfield.npz").exists()
        window.scan_page.scan_button.click()  # Scan frame 1, then a prompt per colour
        wait(app, lambda: window.scan_page.scan_button.text() == "Shoot: red only" and idle())
        assert window.scan_page.retake_button.text() == "Cancel frame"
        wrong.append("G")
        for letter in ("red", "green", "green", "blue"):  # green is asked for twice: the first was red
            wait(app, lambda letter=letter: window.scan_page.scan_button.text() == f"Shoot: {letter} only" and idle())
            QTest.keyClick(window, Qt.Key.Key_Space)
            if letter == "green" and wrong == []:
                wait(app, lambda: "That did not look like green only." in window.log_bar.text())
        wait(app, lambda: (roll / "scans" / "T_001.tif").exists() and idle())
        assert sorted(path.name for path in (roll / "raw").iterdir()) == ["T_001_B.dng", "T_001_G.dng", "T_001_R.dng"]
        assert window.scan_page.scan_button.text() == "Scan frame 2"
    finally:
        window.close()
        app.processEvents()


def test_the_presets_page_lists_by_light_and_its_choice_is_the_one_everywhere(tmp_path, monkeypatch):
    """The presets grouped Scanlight / Own light, each with a mark of how it scans; the one chosen in the
    list is the Roll page's and a new roll's. Its detail: settings with what to set by hand, the LEDs or
    an own light's shutter per colour, the film base where it was metered. Start a roll with … opens
    Roll with it, under a free name when the roll in the fields exists; Delete… asks first."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from PySide6.QtWidgets import QLabel

    from tetherinvert import kinds
    from tetherinvert.session import Preset
    from tetherinvert.window import presets as page_module
    from tetherinvert.window.mainwindow import MainWindow

    answers = []
    monkeypatch.setattr(MainWindow, "_confirm", lambda self, title, text: answers.pop(0))
    rolls = rolled(tmp_path, frames=1)  # roll R, with preset Gold (a sliver of rebate at sim.BASE_ROI)
    store = PresetStore()
    store.save(Preset("HP5", "white", (0, 0, 0, 120), "1/30", roi=sim.BASE_ROI, iso="400", aperture="f/8", base=(55000.0, 56000.0, 57000.0), camera="ILCE-6700",
                      created="2026-10-02 08:05", by_hand=("aperture",)))  # fmt: skip
    store.save(Preset("Agfa own", "own-rgb", (0, 0, 0, 0), "1/4", iso="100", aperture="f/8", base=(58000.0, 59000.0, 57500.0), created="2026-10-03 21:00",
                      roi=(0.1, 0.1, 0.05, 0.3), by_hand=("light",), light_note="Kaiser slimlite", shutters={"R": "1/8", "G": "1/4", "B": "1/2"}))  # fmt: skip
    kept = QImage(60, 40, QImage.Format.Format_RGB32)
    kept.fill(Qt.GlobalColor.gray)
    store.picture("agfa OWN").parent.mkdir(parents=True)
    kept.save(str(store.picture("agfa OWN")), "JPG")  # as the window keeps one at calibration; named without case
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    page, roll = window.presets_page, window.roll_page
    listed = page.preset_list
    try:
        roll.folder_edit.setText(str(rolls))
        roll.roll_edit.setText("New")  # a new roll: started with the preset chosen
        window.rail.buttons["presets"].click()
        app.processEvents()
        # By light: the Scanlight's first, an own light's under its own heading.
        assert listed.names() == ["Gold", "HP5", "Agfa own"]
        y = lambda widget: widget.mapTo(listed, QPoint(0, 0)).y()  # noqa: E731
        headings = {label.text(): label for label in listed.findChildren(QLabel) if label.text() in ("Scanlight", "Own light")}
        assert y(headings["Scanlight"]) < y(listed.rows["HP5"]) < y(headings["Own light"]) < y(listed.rows["Agfa own"])
        marks = {name: page_module.method(row.mark.kind) for name, row in listed.rows.items()}
        assert marks == {"Gold": ("shots", False), "HP5": ("white", False), "Agfa own": ("shots", True)}
        assert listed.rows["Agfa own"].mark.toolTip() == "Own light. Colour: R, G, B by hand (3 shots)"
        assert {kind: page_module.method(kind) for kind in kinds.KINDS} == {
            "rgb": ("shots", False), "rgb-together": ("together", False), "white": ("white", False),
            "own-rgb": ("shots", True), "own-colour": ("together", True), "own-bw": ("white", True),
        }  # fmt: skip
        assert [label.text() for label in listed.rows["Agfa own"].findChildren(QLabel)] == ["Agfa own", "Colour · 3 shots", "3 Oct"]

        # Chosen here, chosen everywhere: the Roll page's picker, its menu, a new roll's start.
        listed.rows["HP5"].click()
        assert window._preset().name == "HP5" and roll.preset_picker.text() == "HP5" and listed.rows["HP5"].isChecked()
        roll.preset_menu.aboutToShow.emit()
        checked = [action.text() for action in roll.preset_menu.actions() if action.isChecked()]
        assert checked == ["HP5"]
        next(action for action in roll.preset_menu.actions() if action.text() == "Gold").trigger()
        assert listed.chosen() == "Gold" and listed.rows["Gold"].isChecked() and not listed.rows["HP5"].isChecked()
        next(action for action in roll.preset_menu.actions() if action.text() == "Manage presets").trigger()
        assert window._step == "presets" and page.isVisible()

        # The detail: what to set by hand, the LEDs, the film base.
        assert page.name_label.text() == "Gold" and page.kind_label.text() == "Colour: R, G, B one by one (3 shots)"
        assert page.settings_lines()[2] == "Aperture: f/8" and page.settings_lines()[3].startswith("LEDs: R ")  # the simulated lens reports its f-stop
        picture = page.base_picture
        assert picture.isVisible() and picture.image is None and picture.roi == pytest.approx(sim.BASE_ROI)  # calibrated on the command line: the rectangle
        assert page.base_levels.shares() == pytest.approx([value / 65535 for value in store.get("Gold").base])
        listed.rows["HP5"].click()
        assert page.settings_lines() == ["ISO: 400", "Shutter: 1/30 s", "Aperture: f/8 — set this yourself", "LEDs: white 120"]
        assert page.base_picture.isVisible() and page.base_picture.image is None and page.calibrated_label.text() == "Calibrated 2 Oct 2026, 08:05 · ILCE-6700"
        listed.rows["Agfa own"].click()
        assert page.settings_lines()[1] == "Shutter: R 1/8 · G 1/4 · B 1/2 s" and page.settings_lines()[3] == "Light: Kaiser slimlite — set this yourself"
        assert page.base_picture.image is not None and page.calibrated_label.text() == "Calibrated 3 Oct 2026, 21:00"  # the picture kept at calibration
        assert page.start_button.text() == "Start a roll with Agfa own" and page.start_button.isEnabled()

        # Start a roll with …: the roll in the fields exists and would keep its own preset, so a free name.
        roll.roll_edit.setText("R")
        page.start_button.click()
        assert window._step == "roll" and roll.roll_edit.text() == "Roll01" and roll.preset_picker.text() == "Agfa own"
        assert roll.start_button.text() == "Start new session" and "New roll “Roll01” with preset Agfa own" in roll.note.text()
        roll.roll_edit.setText("Fresh")  # a name not taken stays
        window.rail.buttons["presets"].click()
        listed.rows["HP5"].click()
        page.start_button.click()
        assert roll.roll_edit.text() == "Fresh" and roll.preset_picker.text() == "HP5"

        # Delete… asks; the list follows, and the preset's picture goes with it.
        window.rail.buttons["presets"].click()
        listed.rows["Agfa own"].click()
        answers.append(False)
        page.delete_button.click()
        assert listed.names() == ["Gold", "HP5", "Agfa own"] and store.picture("Agfa own").exists()
        answers.append(True)
        page.delete_button.click()
        assert store.names() == ["Gold", "HP5"] and listed.names() == ["Gold", "HP5"] and listed.chosen() == "Gold"
        assert not store.picture("Agfa own").exists() and "No presets for an own light yet." in [label.text() for label in listed.findChildren(QLabel)]
        answers.extend([True, True])
        page.delete_button.click()  # Gold, then HP5, chosen after it
        page.delete_button.click()
        assert listed.names() == [] and page.empty_note.isVisible() and page.recalibrate_button.isHidden() and page.name_label.isHidden() and window._preset() is None
        assert roll.preset_picker.text() == "None chosen" and not page.start_button.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_the_film_base_picture_is_a_close_look_at_the_rebate_around_the_rectangle():
    """8 times the rectangle's narrow side across, 3 : 2, between 12 and 35 % of the frame, inside it: the
    rectangles drawn are slivers, and sized by their length the picture was the whole frame."""
    from tetherinvert.window.viewer import around

    left, top, across, down = around(sim.BASE_ROI, 1500, 1000)  # 2.5 % wide, 40 % high
    assert (left, top, across, down) == pytest.approx((0, 400, 300, 200))  # at the frame's left edge; the rectangle runs off top and bottom
    assert 1500 * sim.BASE_ROI[2] / across == pytest.approx(1 / 8)
    assert around((0.686, 0.0067, 0.028, 0.0096), 1500, 1000) == pytest.approx((1500 * 0.7 - 90, 0, 180, 120))  # a hair at the top: 12 % of the frame
    assert around((0.2, 0.2, 0.5, 0.5), 1500, 1000)[2] == pytest.approx(0.35 * 1500)  # a large one: its middle


def test_the_flat_field_step_offers_keep_only_with_a_flat_field_the_preset_can_use(tmp_path, monkeypatch):
    """A new roll's session: capture or scan without. A roll with a flat field its preset can use:
    keep it as well. Scan without settles at once, a quarter stop lower."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.flatfield import FlatField
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path, frames=0)
    assert cli(["--sim", "flatfield", str(rolls / "Flat"), "--preset", "Gold"]) == 0
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    page, scan = window.roll_page, window.scan_page
    offered = lambda: tuple(button.text() for button in (scan.flat_button, scan.keep_flat_button, scan.without_button) if button.isVisible())  # noqa: E731
    try:
        page.folder_edit.setText(str(rolls))
        window._choose_preset("Gold")
        page.roll_edit.setText("New")
        assert page.start_button.text() == "Start new session"
        page.start_button.click()
        assert offered() == ("Capture flat field", "Scan without") and scan.scan_button.isHidden()
        scan.without_button.click()
        assert not window._flat_pending() and not window._session["flatfield"] and not Roll.open(rolls / "New").flatfield
        assert scan.scan_button.isVisible() and "no flat field" in scan.session_label.text() and "quarter stop" in scan.session_label.text()
        scan.end_button.click()
        page.roll_edit.setText("Flat")
        created = FlatField.load(rolls / "Flat").created
        assert page.start_button.text() == "Continue session"
        page.start_button.click()
        assert offered() == ("Capture flat field", f"Keep the flat field from {created}", "Scan without")
    finally:
        window.close()
        app.processEvents()


def test_a_roll_is_continued_from_scan_and_a_new_one_started_on_roll(tmp_path, monkeypatch):
    """Where its session was ended, a roll is continued, and its flat field is the session's first
    step there too. A new roll is sent to Roll, for its name and metadata."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path, frames=1)
    assert cli(["--sim", "flatfield", str(rolls / "R"), "--preset", "Gold"]) == 0
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    scan, page = window.scan_page, window.roll_page
    try:
        page.folder_edit.setText(str(rolls))
        page.roll_edit.setText("R")
        window._refresh_strip()
        window.rail.buttons["scan"].click()
        assert scan.continue_button.isVisible() and scan.continue_button.isEnabled() and scan.to_roll_button.isHidden() and scan.end_button.isHidden()
        assert scan.flat_button.isHidden() and scan.keep_flat_button.isHidden()  # no session, no flat field's step
        scan.continue_button.click()
        assert window._session is not None and window._flat_pending() and window._step == "scan"
        assert scan.end_button.isVisible() and scan.continue_button.isHidden() and scan.keep_flat_button.isVisible()
        scan.keep_flat_button.click()
        assert window._session["flatfield"] and scan.keep_flat_button.isHidden() and scan.scan_button.isVisible()
        scan.end_button.click()
        assert scan.continue_button.isVisible() and scan.end_button.isHidden()
        # A name not taken yet: started on Roll.
        page.roll_edit.setText("Fresh")
        assert scan.continue_button.isHidden() and scan.to_roll_button.isVisible() and scan.to_roll_button.text() == "New roll on Roll…"
        scan.to_roll_button.click()
        assert window._step == "roll" and window._session is None
    finally:
        window.close()
        app.processEvents()


def test_a_flat_field_with_film_in_the_holder_is_refused_and_its_step_says_why(tmp_path, monkeypatch):
    """The check that the light is bare: the session stays on its flat field's step, which says why,
    and the frames wait until it is settled."""
    import contextlib

    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert import workers
    from tetherinvert.window.mainwindow import MainWindow

    monkeypatch.setattr(workers, "film_removed", lambda camera: contextlib.nullcontext())  # the film left in the holder
    rolls = rolled(tmp_path, frames=0)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    page = window.roll_page
    try:
        wait(app, lambda: "iso" in window._camera)
        page.folder_edit.setText(str(rolls))
        page.roll_edit.setText("R")
        window._choose_preset("Gold")
        page.start_button.click()
        scan = window.scan_page
        scan.flat_button.click()
        assert window._flat_capturing() and not scan.flat_button.isEnabled() and not scan.without_button.isEnabled()
        wait(app, lambda: not window._flat_capturing() and not window._busy)
        assert window._flat_pending() and not (rolls / "R" / "flatfield.npz").exists()
        assert "Take the film out of the holder and capture the flat field again." in window.log_bar.text()
        assert scan.flat_problem.isVisible() and "Take the film out" in scan.flat_problem.text() and scan.scan_button.isHidden()
        assert scan.flat_button.isEnabled() and scan.flat_button.text() == "Capture flat field"  # try again, or go without
        scan.without_button.click()
        assert not window._flat_pending() and scan.scan_button.isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_the_window_settings_come_back_as_they_were_left(tmp_path, monkeypatch):
    """The negative box, the Lightroom folders and the orientation, as last left; defaults the first time."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window import mainwindow

    app = QApplication.instance() or QApplication([])

    def reopened(**saved) -> tuple:
        settings = mainwindow.QSettings("tetherinvert", "tetherinvert-sim")
        settings.clear()
        for key, value in saved.items():
            settings.setValue(key, value)
        settings.sync()
        window = mainwindow.MainWindow(sim=True)
        try:
            lightroom = window.lightroom_panel
            return window.roll_page.negative_box.isChecked(), (lightroom.lightroom_scans.isChecked(), lightroom.lightroom_prints.isChecked()), window._orientation
        finally:
            window.close()
            app.processEvents()

    upright = {"turn": 0, "mirror": False}
    assert reopened() == (False, (True, True), upright)
    assert reopened(negative="true", lightroom_scans="false", turn=270, mirror="true") == (True, (False, True), {"turn": 270, "mirror": True})
    assert reopened(lightroom_prints="false") == (False, (True, False), upright)
    assert reopened(mode="both", lightroom="print", look="linear", show_edits="true", ends="both") == (False, (True, True), upright)


def test_the_window_opens_where_it_was_left(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    screen = window.screen().availableGeometry()
    assert window.width() <= screen.width() and window.height() <= screen.height()  # the first start: no larger than the screen
    window.show()
    window.setGeometry(30, 40, screen.width() - 60, screen.height() - 80)
    app.processEvents()
    left = window.geometry()
    window.close()
    app.processEvents()
    window = MainWindow(sim=True)
    try:
        window.show()
        app.processEvents()
        assert window.geometry() == left
    finally:
        window.close()
        app.processEvents()


def squeezed(top) -> list[str]:
    """The visible widgets of `top` given less room than they need: on a short screen the side panel's
    fields a few pixels high, rows of the metadata dialog on top of each other. A widget needs
    what its layout would give it at least (Qt's qSmartMinSize); one whose height follows its width,
    the height at the width it has."""
    from PySide6.QtWidgets import QSizePolicy, QWidget

    ignored = QSizePolicy.Policy.Ignored

    def least(policy, own: int, smallest: int, preferred: int) -> int:
        if own:
            return own  # set on the widget
        if policy == ignored:
            return 0
        return smallest if policy.value & QSizePolicy.PolicyFlag.ShrinkFlag.value else max(smallest, preferred)

    def placed(layout, widget) -> bool:
        items = [layout.itemAt(index) for index in range(layout.count())]
        return any(item.widget() is widget or (item.layout() is not None and placed(item.layout(), widget)) for item in items)

    short = []
    for widget in top.findChildren(QWidget):
        parent = widget.parentWidget()
        if not widget.isVisible() or parent is None or parent.layout() is None or not placed(parent.layout(), widget):
            continue  # sized by its parent itself, as a combo box sizes its text field
        policy, hint, smallest = widget.sizePolicy(), widget.sizeHint(), widget.minimumSizeHint()
        width = least(policy.horizontalPolicy(), widget.minimumWidth(), smallest.width(), hint.width())
        height = least(policy.verticalPolicy(), widget.minimumHeight(), smallest.height(), hint.height())
        if widget.hasHeightForWidth() and policy.verticalPolicy() != ignored:
            layout = widget.layout()
            height = layout.totalMinimumHeightForWidth(widget.width()) if layout is not None else widget.heightForWidth(widget.width())
        if widget.width() < width - 1 or widget.height() < height - 1:
            name = getattr(widget, "text", getattr(widget, "title", lambda: ""))()
            short.append(f"{type(widget).__name__} {name!r}: {widget.width()}×{widget.height()}, needs {width}×{height}")
    return short


def test_the_window_fits_a_small_screen_in_every_step(tmp_path, monkeypatch):
    """At the screen's height and shorter: nothing squeezed, a page or a column scrolls instead. The
    window's minimum is the same on every step: when it changed, macOS let go of a window filling the
    screen. The same with the log's panel open, which lies over the page's bottom, at most half of it."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path, frames=1)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    screen = window.screen().availableGeometry()
    window.show()
    try:
        wait(app, lambda: "iso" in window._camera)
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        steps = {
            "roll": lambda: window.rail.buttons["roll"].click(),
            "scan": lambda: window.rail.buttons["scan"].click(),
            "develop": lambda: (window.rail.buttons["develop"].click(), window.frame_page.frame_row.set_open(False)),
            "develop, metadata open": lambda: window.frame_page.frame_row.set_open(True),
            "finish": lambda: window.rail.buttons["finish"].click(),
            "presets": lambda: window.rail.buttons["presets"].click(),
            "the wizard": lambda: window.presets_page.new_preset_button.click(),
        }
        minimums = set()
        panel = window.log_bar.panel
        for step, show in steps.items():
            show()
            for log in ("closed", "open"):
                (window.log_bar.open_log if log == "open" else window.log_bar.close_log)()
                for height in (screen.height(), 600):
                    window.resize(screen.width(), height)
                    app.processEvents()
                    assert squeezed(window) == [], f"{step}, the log {log}, {height} px high"
                    if log == "open":  # over the page's bottom, at most half of it, the status bar free
                        page = window.body.geometry()
                        assert page.contains(panel.geometry()) and panel.geometry().bottom() == page.bottom(), (step, panel.geometry(), page)
                        assert panel.height() <= page.height() // 2 and panel.isVisible(), step
                minimums.add(window.minimumSize().toTuple())
        (width, height), *others = minimums
        assert others == [], f"the window's minimum changed with the step: {minimums}"
        assert width <= 1440 and height <= screen.height()  # a 13-inch MacBook Air's screen, at its default
    finally:
        window.close()
        app.processEvents()


def test_the_strip_runs_under_the_picture_with_a_tag_and_the_corrections(tmp_path, monkeypatch):
    """Left to right in one row under the picture, as wide as the picture and not under the column, at a
    height that does not change. A caption is the number with one tag, the most pressing, and the
    frame's base and corrections under it."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from PySide6.QtWidgets import QListView

    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path)
    roll = Roll.open(rolls / "R")
    for name in roll.frame(1).files:
        (roll.folder / name).unlink()  # frame 1's RAW files deleted
    roll.update_frame(2, adjust={**invert.NEUTRAL, "d": 2}, develop="converted")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        strip = window.strip
        captions = lambda: [strip.item(row).text() for row in range(strip.count())]  # noqa: E731
        assert captions() == ["Live", "1 · to print", "2 · to print\nConv D+2"]  # to print before RAW deleted: it asks for something
        window._write_prints()
        wait(app, lambda: window._due == 0 and not window._pending)
        assert captions() == ["Live", "1 · RAW deleted", "2\nConv D+2"]

        heights = set()
        for step in ("scan", "develop"):
            window.rail.buttons[step].click()
            app.processEvents()
            here = lambda widget: widget.mapTo(window, QPoint(0, 0))  # noqa: E731
            picture, side = window.views, window.side
            assert strip.flow() == QListView.Flow.LeftToRight and not strip.isWrapping()
            assert here(strip).y() >= here(picture).y() + picture.height(), step  # under the picture
            assert here(strip).x() == here(picture).x() and strip.width() == picture.width(), step
            assert here(strip).x() + strip.width() <= here(side).x(), step  # not under the column
            rects = [strip.visualItemRect(strip.item(row)) for row in range(strip.count())]
            assert len({rect.top() for rect in rects}) == 1 and all(strip.viewport().rect().contains(rect) for rect in rects), step
            heights.add((strip.minimumHeight(), strip.maximumHeight(), strip.height()))
        (smallest, largest, height), *others = heights
        assert others == [] and smallest == largest == height
    finally:
        window.close()
        app.processEvents()


def test_the_develop_column_fits_the_screen_without_scrolling(tmp_path, monkeypatch):
    """Base, corrections, tone, Hold, Match the roll and Give these to all frames, all at the window's
    smallest size, and so on a 1440 × 900 screen (the small-screen test keeps that minimum within it).
    The prints are on Finish."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path, frames=1)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        window.rail.buttons["develop"].click()
        window.resize(window.minimumSize())
        app.processEvents()
        assert window.height() == window.minimumHeight() <= window.screen().availableGeometry().height()
        column, scroll = window.develop_column, window._sides["develop"]
        assert column.isVisible() and scroll.verticalScrollBar().maximum() == 0, (column.minimumSizeHint(), scroll.viewport().size())
        bottom = lambda widget: widget.mapTo(scroll.viewport(), QPoint(0, widget.height())).y()  # noqa: E731
        assert all(bottom(widget) <= bottom(column.all_button) <= scroll.viewport().height() for widget in (column.match_box, column.tone_box))
        assert not hasattr(column, "write_button")
    finally:
        window.close()
        app.processEvents()


def test_the_roll_picker_over_every_step_switches_the_roll_and_is_held_in_a_session(tmp_path, monkeypatch):
    """The header's picker lists the folder's rolls: picking one shows it in the strip and stays on the
    step; New roll… gives a free name on Roll, as Continue in the page's list chooses a roll there. A
    session holds its roll."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.window.rail import LABELS

    rolls = rolled(tmp_path)  # R: frames 1 and 2
    assert cli(["--sim", "scan", str(rolls / "S"), "--preset", "Gold"]) == 0  # S: frame 1
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    page, picker = window.roll_page, window.header.picker

    def pick(text: str) -> None:
        picker.menu().aboutToShow.emit()
        next(action for action in picker.menu().actions() if action.text() == text).trigger()

    try:
        page.folder_edit.setText(str(rolls))
        page.roll_edit.setText("R")
        window._refresh_strip()
        assert picker.text() == "R · Gold"
        heights = set()
        for step, (roll, frames) in zip(("roll", "scan", "develop", "finish", "presets"), [("S", [1]), ("R", [1, 2])] * 3, strict=False):
            window.rail.buttons[step].click()
            app.processEvents()
            heights.add(window.header.height())
            if step == "presets":  # not about a roll: no picker, which keeps its roll
                assert not picker.isVisible() and window.header.title.text() == "Film presets" and picker.text() == "R · Gold"
                break
            assert picker.isVisible() and window.header.title.text() == LABELS[step]
            pick(f"{roll} · Gold")
            assert page.roll_edit.text() == roll and window._strip_roll == str(rolls / roll) and window.strip.numbers()[1:] == frames
            assert window._step == step and picker.text() == f"{roll} · Gold"
            if step == "develop":
                assert window._shown in frames and window.frame_page.isVisible()  # a frame of the roll picked
        assert len(heights) == 1  # the header as tall without the picker
        window.rail.buttons["scan"].click()
        assert picker.isVisible() and picker.text() == "R · Gold"
        pick("New roll…")
        assert page.roll_edit.text() == "Roll01" and window._step == "roll" and picker.text() == "Roll01 · Gold" and window.strip.numbers()[1:] == []
        page.continue_buttons["S"].click()
        assert page.roll_edit.text() == "S" and window._step == "roll" and page.continue_buttons["S"].text() == "Chosen"
        assert page.start_button.text() == "Continue session"
        # A session holds its roll: the picker and Continue are locked until it ends.
        page.start_button.click()
        assert window._session is not None and not picker.isEnabled() and not page.continue_buttons["R"].isEnabled()
        pick("R · Gold")
        assert page.roll_edit.text() == "S" and window._strip_roll == str(rolls / "S")
        window.scan_page.end_button.click()
        assert picker.isEnabled() and page.continue_buttons["R"].isEnabled()
    finally:
        window.close()
        app.processEvents()


def test_the_parts_ask_the_roll_part_for_the_session_and_the_presets_part_for_the_wizard():
    """Only roll.py keeps the session and only presets.py the wizard; the other parts ask them, so what
    a session or a calibration is stays written in one place."""
    from tetherinvert import window

    folder = Path(window.__file__).parent
    for module in sorted(folder.glob("*.py")):
        text = module.read_text()
        if module.name != "roll.py":
            assert "self._session" not in text.replace("self._session_", ""), module.name
        if module.name != "presets.py":
            assert "self._wizard" not in text.replace("self._wizard_", ""), module.name


def test_a_roll_folder_is_one_session_new_continued_or_in_the_way(tmp_path, monkeypatch):
    """A free name starts a new roll; a roll is continued with the preset it was started with,
    whatever the store says now; a folder that is not a roll is never scanned into."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from dataclasses import replace

    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.session import Preset

    store = PresetStore()
    started = Preset("Gold", "rgb", (200, 100, 90, 0), "1/4", roi=sim.BASE_ROI, iso="100", aperture="f/8", base=(50000.0, 40000.0, 30000.0))
    store.save(replace(started, levels=(255, 255, 255, 0)))  # calibrated again since the roll was started
    store.save(Preset("Other", "rgb", (10, 10, 10, 0), "1/2", roi=sim.BASE_ROI))
    rolls = tmp_path / "scans"
    Roll.start(rolls / "Old", started, flatfield=True)
    (rolls / "Holiday").mkdir()  # a folder that is not a roll

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window._choose_preset("Other")
        window.roll_page.roll_edit.setText("Holiday")
        assert not window.roll_page.start_button.isEnabled() and "is there already and is not a roll" in window.roll_page.note.text()
        window.roll_page.roll_edit.setText("Fresh")
        assert window.roll_page.start_button.isEnabled() and window.roll_page.start_button.text() == "Start new session"
        window.roll_page.roll_edit.setText("Old")
        assert window.roll_page.start_button.text() == "Continue session" and "preset Gold. Continue it" in window.roll_page.note.text()
        window.roll_page.start_button.click()
        assert window.scan_page.keep_flat_button.isHidden()  # it was to have one, but none was captured
        window.scan_page.without_button.click()
        assert window._session["preset"] == started and not window._session["flatfield"]  # its own copy of the preset, not the recalibrated one, not "Other"
        assert window._session["preset"] == started and window.presets_page.preset_list.chosen() == "Gold" and not window.presets_page.preset_list.isEnabled()
        assert not (rolls / "Holiday" / "roll.json").exists()
    finally:
        window.close()
        app.processEvents()


def test_corrections_made_outside_a_session_are_there_when_the_window_opens_again(tmp_path, monkeypatch):
    """No camera needed to correct a roll; closing at once loses nothing, and the frames come back
    as they were left: their base and corrections shown, as their prints will be."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls = tmp_path / "scans"
    base, picture = ",".join(str(v) for v in sim.BASE_ROI), ",".join(f"{v:.3f}" for v in sim.PICTURE_RECT)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold", "--picture", picture]) == 0
    app = QApplication.instance() or QApplication([])

    def opened() -> MainWindow:
        window = MainWindow(sim=True)
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        window._show(1)
        return window

    window = opened()
    try:
        window.develop_column.base_buttons["converted"].click()
        QTest.keyClick(window.strip, Qt.Key.Key_Q)
        QTest.keyClick(window.strip, Qt.Key.Key_Q)
    finally:
        window.close()  # at once, before the save that follows the last key
        app.processEvents()
    saved = Roll.open(rolls / "R").frame(1)
    assert saved.adjust == {**invert.NEUTRAL, "d": 2} and saved.develop == "converted"

    window = opened()
    try:
        wait(app, lambda: window.frame_page.frame_info.text().startswith("Frame 1 · Converted ·"))
        assert window.strip.item(1).text() == "1 · to print\nConv D+2" and window.develop_column.values["d"].text() == "+2"
        assert window.develop_column.base_buttons["converted"].isChecked()
        window.develop_column.hold_box.click()  # by mouse, not only with H
        assert window._corrections.hold == {**invert.NEUTRAL, "d": 2}
        window.develop_column.hold_box.click()
        assert window._corrections.hold is None
    finally:
        window.close()
        app.processEvents()


def test_frames_are_turned_as_set_on_the_live_view_and_can_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert import orient
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold"]) == 0
    assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold", "--turn", "90", "--mirror"]) == 0
    prompts = []
    monkeypatch.setattr(MainWindow, "_confirm", lambda self, title, text: prompts.append(text) or True)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        wait(app, lambda: window.view.image is not None)
        wide = (window.view.image.width(), window.view.image.height())
        # Turned and mirrored on the live view, as the files will be.
        window.live_page.live_orientation.buttons["right"].click()
        window.live_page.live_orientation.buttons["mirror"].click()
        turned = {"turn": 90, "mirror": True}
        assert window._orientation == turned and window.live_page.live_orientation.label.text() == "Turned right, mirrored"
        wait(app, lambda: (window.view.image.width(), window.view.image.height()) == wide[::-1])
        # The same with the keys, on the live view: N turns right, Shift+N left, M mirrors.
        QTest.keyClick(window.strip, Qt.Key.Key_N)
        assert window._orientation == {"turn": 0, "mirror": True}  # mirrored: right as seen is left for the camera
        QTest.keyClick(window.strip, Qt.Key.Key_N, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClick(window.strip, Qt.Key.Key_M)
        QTest.keyClick(window.strip, Qt.Key.Key_M)
        assert window._orientation == turned and window.live_page.live_orientation.label.text() == "Turned right, mirrored"
        # A rectangle drawn on the turned picture is kept in the camera's frame.
        window.live_page.picture_tool.click()
        area = window.view._area()
        at = lambda fx, fy: QPoint(int(area.x() + fx * area.width()), int(area.y() + fy * area.height()))  # noqa: E731
        QTest.mousePress(window.view, Qt.MouseButton.LeftButton, pos=at(0.1, 0.2))
        QTest.mouseMove(window.view, at(0.5, 0.6))
        QTest.mouseRelease(window.view, Qt.MouseButton.LeftButton, pos=at(0.5, 0.6))
        assert window.view.rects["picture"] == pytest.approx(orient.rect_back((0.1, 0.2, 0.4, 0.4), turned), abs=0.01)
        # Scanning takes the setting along.
        asked = []
        window.ask_scan.disconnect(window._hardware.scan)
        window.ask_scan.connect(lambda folder, preset, retake, settings: asked.append(settings))
        window._session = {"folder": str(rolls / "R"), "preset": PresetStore().get("Gold"), "flatfield": False, "flat": "none", "capturing": False, "problem": ""}
        window._refresh_next()
        window._update_buttons()
        window.scan_page.scan_button.click()
        window._session = None
        assert asked[0]["orientation"] == turned
        # A frame scanned turned is shown turned: its preview stays as the camera saw it.
        window._show(2)
        wait(app, lambda: window.frame_view.image is not None and window.frame_view.image.height() == 1600)
        assert window.frame_view.image.width() == 1067 and window.frame_page.frame_orientation.label.text() == "Turned right, mirrored"
        # Turned again after scanning, on the frame: its files, in place; a key and a click in quick succession add up.
        before = tifffile.imread(rolls / "R" / "scans" / "R_002.tif")
        QTest.keyClick(window.strip, Qt.Key.Key_M)
        window.frame_page.frame_orientation.buttons["left"].click()  # asked for before the first is done
        wait(app, lambda: 2 not in window._rendering and window._pending == 0)
        assert Roll.open(rolls / "R").frame(2).orientation == {"turn": 0, "mirror": False}
        assert np.array_equal(tifffile.imread(rolls / "R" / "scans" / "R_002.tif"), orient.image_back(before, turned))  # upright, losslessly
        wait(app, lambda: window.frame_view.image is not None and window.frame_view.image.width() == 1600)
        assert window.frame_page.frame_orientation.label.text() == "Upright" and window._orientation == turned  # the live view's setting is its own
        # Deleting a frame: asked first, then every file of it goes and the next frame is shown.
        window._show(1)
        QTest.keyClick(window.strip, Qt.Key.Key_Backspace)
        assert "Delete frame 1 of roll “R”?" in prompts[-1] and "RAW files, TIFFs, preview" in prompts[-1]
        assert [f.number for f in Roll.open(rolls / "R").frames] == [2] and not list(rolls.glob("R/**/R_001*"))
        assert window.strip.numbers() == [LIVE, 2] and window._shown == 2
    finally:
        window.close()
        app.processEvents()
    window = MainWindow(sim=True)  # the setting is the window's: there next time
    try:
        assert window._orientation == turned
    finally:
        window.close()
        app.processEvents()



def test_up_and_down_step_round_a_frames_bases(tmp_path, monkeypatch):
    """Each frame is printed from its own base; ↑ and ↓ step round them, on a frame only."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    for _ in range(2):
        assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold"]) == 0
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        QTest.keyClick(window.strip, Qt.Key.Key_Down)
        assert [dev.base for dev in window._developments.values()] == ["flat", "flat"]  # the live view: no function
        window._show(1)
        steps = []
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Down, Qt.Key.Key_Up):
            QTest.keyClick(window.strip, key)
            steps.append(window._developments[1].base)
        assert steps == ["converted", "converted white", "flat", "converted white"]  # round past the ends, both ways
        assert Roll.open(rolls / "R").frame(1).develop == "converted white" and window._developments[2].base == "flat"  # per frame, and saved
        wait(app, lambda: window.frame_page.frame_info.text().startswith("Frame 1 · Converted + white ·"))
        window._show(2)
        assert window.develop_column.base_buttons["flat"].isChecked()
    finally:
        window.close()
        app.processEvents()


def test_the_gauges_follow_the_corrections_and_colour_is_off_on_black_and_white(tmp_path, monkeypatch):
    """Beside each correction's keys a gauge, its marker at the value on a square-root scale; on a
    black-and-white frame C, M, Y and Sat are greyed out and their keys and buttons do nothing."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.window.viewer import COLOUR_ONLY, SATURATION_PLACE, gauge_position

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    assert cli(["--sim", "calibrate", "HP5", "--base", base, "--white"]) == 0
    assert cli(["--sim", "scan", str(rolls / "Colour"), "--preset", "Gold"]) == 0
    assert cli(["--sim", "scan", str(rolls / "BW"), "--preset", "HP5"]) == 0
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    bar = window.develop_column

    def open_frame(name: str) -> None:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText(name)
        window._refresh_strip()
        window._show(1)
        wait(app, lambda: window.frame_page.frame_info.text().startswith("Frame 1 · Flat ·"))

    # Saturation is the key right of T by its place (as the system reports it), whatever letter it types.
    field, right_of_t = SATURATION_PLACE[sys.platform]
    bottom_left = {"virtual": 0x06, "scan": 52}[field]  # the key right of shift: Z on a US keyboard, Y on a German one

    def press(place: int, key, text: str, shift: bool = False) -> None:
        scan, virtual = (place, 0) if field == "scan" else (0, place)
        modifiers = Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
        QApplication.sendEvent(window.strip, QKeyEvent(QEvent.Type.KeyPress, key, modifiers, scan, virtual, 0, text))

    def markers_at_values() -> bool:
        a = invert.adjustment(window._corrections.of(1))
        for key, gauge in bar.gauges.items():
            span = gauge.width() - 2 * gauge.MARK
            if gauge.value != a[key] or abs(gauge.marker_x() - (gauge.MARK + gauge_position(key, a[key]) * span)) > 1e-6:
                return False
        return True

    try:
        open_frame("Colour")
        assert all(gauge.isVisible() and gauge.isEnabled() for gauge in bar.gauges.values())
        for key in (Qt.Key.Key_Q, Qt.Key.Key_Q, Qt.Key.Key_W):  # D +2, C +1
            QTest.keyClick(window.strip, key)
        QTest.mouseClick(bar.buttons["c"][1], Qt.MouseButton.LeftButton)  # by mouse too: C +2
        QTest.mouseClick(bar.buttons["sat"][0], Qt.MouseButton.LeftButton)  # Sat -1
        assert window._corrections.of(1) == {**invert.NEUTRAL, "d": 2, "c": 2, "sat": -1}
        assert markers_at_values()
        sat = lambda: window._corrections.of(1)["sat"]  # noqa: E731
        press(right_of_t, Qt.Key.Key_Z, "z")  # the key right of T on a German keyboard: Sat 0
        assert sat() == 0
        press(bottom_left, Qt.Key.Key_Y, "y")  # Y, but at the bottom left: nothing
        assert sat() == 0
        press(right_of_t, Qt.Key.Key_Y, "Y", shift=True)  # right of T on a US keyboard, with Shift: Sat -1
        assert sat() == -1
        gauge = bar.gauges["c"]
        middle = gauge.MARK + (gauge.width() - 2 * gauge.MARK) / 2
        assert bar.gauges["m"].marker_x() == middle and gauge.marker_x() > middle and bar.gauges["sat"].marker_x() < middle
        window._show(LIVE)
        assert not bar.gauges["d"].isVisible()  # with the column, on Develop only

        open_frame("BW")
        for key in COLOUR_ONLY:
            assert not bar.gauges[key].isEnabled() and not any(button.isEnabled() for button in bar.buttons[key]), key
        assert bar.gauges["d"].isEnabled() and bar.gauges["grad"].isEnabled() and all(button.isEnabled() for button in bar.buttons["d"])
        for key in (Qt.Key.Key_W, Qt.Key.Key_E, Qt.Key.Key_R):  # C, M, Y: nothing
            QTest.keyClick(window.strip, key)
        press(right_of_t, Qt.Key.Key_Z, "z")  # Sat: nothing
        QTest.mouseClick(bar.buttons["y"][1], Qt.MouseButton.LeftButton)
        assert window._corrections.of(1) in (None, invert.NEUTRAL)
        QTest.keyClick(window.strip, Qt.Key.Key_Q)  # D still works
        QTest.mouseClick(bar.buttons["grad"][1], Qt.MouseButton.LeftButton)
        assert window._corrections.of(1) == {**invert.NEUTRAL, "d": 1, "grad": 1} and markers_at_values()

        shot = os.environ.get("TETHERINVERT_SCREENSHOT")
        if shot:
            open_frame("Colour")
            app.processEvents()
            window.grab().save(str(Path(shot).with_stem(Path(shot).stem + "-gauges")))
    finally:
        window.close()
        app.processEvents()


def test_a_lost_camera_clears_the_live_view_and_the_device_labels_and_the_rail_say_it_in_colour(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.workers import NOT_CONNECTED

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        wait(app, lambda: window.view.image is not None, 30)
        panel, dot = window.devices_panel, lambda: window.rail.buttons["devices"].dot  # noqa: E731
        assert panel.camera_label.property("tone") == "ok" and panel.light_label.property("tone") == "ok" and dot() == "ok"
        wait(app, lambda: window.devices_panel.temperature_label.text() != "—", 10)  # the light is asked every 3 s, and says its temperature
        assert window.devices_panel.temperature_label.text() == "35.0 °C" and "°C" not in window.devices_panel.light_label.text()

        window._on_devices(NOT_CONNECTED, window._light_text)  # as the hardware thread says it when the camera is lost
        assert window.devices_panel.temperature_label.text() == "35.0 °C"  # the light is still there
        assert window.view.image is None and window.view.waiting == "The camera is not connected."
        assert panel.camera_label.text() == NOT_CONNECTED and panel.camera_label.property("tone") == "danger" and dot() == "danger"
        assert panel.light_label.property("tone") == "ok"
        assert window.live_page.focus_label.text() == "Focus —" and window.scan_page.exposure_label.text() == "—"
        window.view.grab()  # paints the message instead of the last picture

        wait(app, lambda: window.view.image is not None, 10)  # the simulated camera is still there: live view comes back
        window._on_devices("Simulated Camera", NOT_CONNECTED)
        assert panel.temperature_label.text() == "—" and panel.light_label.property("tone") == "danger" and dot() == "danger"
    finally:
        window.close()


def test_frames_are_numbered_as_on_the_film_and_any_frame_can_be_rescanned(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    questions, answers = [], [None, -1]  # cancelled once, then answered 00
    monkeypatch.setattr(MainWindow, "_ask_first_number", lambda self: questions.append(1) or answers.pop(0))
    confirms, replies = [], [False, True]
    monkeypatch.setattr(MainWindow, "_confirm", lambda self, title, text: confirms.append(text) or replies.pop(0))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    asked = []
    try:
        window.ask_scan.disconnect(window._hardware.scan)  # what would be scanned, not the scan itself
        window.ask_scan.connect(lambda folder, preset, retake, settings: asked.append((settings["number"], retake)))
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        Roll.start(rolls / "R", PresetStore().get("Gold"), flatfield=False)
        window._session = {"folder": str(rolls / "R"), "preset": PresetStore().get("Gold"), "flatfield": False, "flat": "none", "capturing": False, "problem": ""}
        window._refresh_next()
        window._update_buttons()
        assert window.scan_page.counter.isHidden() and window.scan_page.scan_button.text() == "Scan"  # the question decides first

        # The roll's first scan asks; cancelled, nothing is scanned and nothing stored.
        window.scan_page.scan_button.click()
        assert questions == [1] and asked == [] and Roll.open(rolls / "R").next is None
        window.scan_page.scan_button.click()
        assert questions == [1, 1] and asked == [(-1, False)] and Roll.open(rolls / "R").next == -1
        assert not window.scan_page.counter.isHidden() and window.scan_page.next_label.text() == "00" and window.scan_page.scan_button.text() == "Scan frame 00"

        # Three frames, counting on from 00: the strip, the frame line and the buttons say 00, 0, 1.
        for _ in range(3):
            assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold"]) == 0
        window._refresh_strip()
        window._refresh_next()
        window._update_buttons()
        assert [window.strip.item(row).text().split(" ")[0] for row in range(1, 4)] == ["00", "0", "1"]
        assert window.scan_page.scan_button.text() == "Scan frame 2" and window.scan_page.retake_button.text() == "Retake frame 1"
        window.rail.buttons["finish"].click()  # Space scans from every step during a session, and opens Scan to show it
        window.activateWindow()  # shortcuts fire only in the active window
        assert QTest.qWaitForWindowActive(window)
        QTest.keyClick(window, Qt.Key.Key_Space)  # Space scans, whatever the button says by now
        assert questions == [1, 1] and asked[-1] == (2, False)  # the roll has frames: no question any more
        assert window.rail.buttons["scan"].isChecked() and window.view.isVisible()

        # The counter steps only onto free numbers: it can never turn Scan into a replacement.
        window.scan_page.next_plus.click()
        assert window.scan_page.next_label.text() == "3" and Roll.open(rolls / "R").next == 3
        window.scan_page.next_minus.click()
        assert window.scan_page.next_label.text() == "2" and not window.scan_page.next_minus.isEnabled()  # 1, 0, 00 are taken
        assert window.scan_page.scan_button.text() == "Scan frame 2"

        # "Rescan frame 0" on a frame, then back to the live view without pressing it: the button
        # says what it does there (retake the last), and does exactly that.
        window._show(0)
        rescan = window.frame_page.rescan_button
        assert window.frame_page.frame_info.text().startswith("Frame 0")
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Scan frame 2", "Rescan frame 0")
        assert rescan.isVisible() and rescan.text() == "Rescan frame 0"
        window._show_live()
        assert not rescan.isVisible()
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Scan frame 2", "Retake frame 1")
        window.scan_page.retake_button.click()
        assert asked[-1] == (None, True)  # a retake of the last frame, as the label said

        # On a frame, Scan scans the next new frame (from the live view).
        window._show(0)
        window.scan_page.scan_button.click()
        assert asked[-1] == (2, False) and window._shown == LIVE

        # Rescan frame 00: nothing is shot yet; on the live view Replace frame 00 and Cancel rescan.
        count = len(asked)
        window._show(-1)
        assert rescan.text() == "Rescan frame 00"
        rescan.click()
        assert len(asked) == count and window._shown == LIVE
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Replace frame 00", "Cancel rescan")
        window.scan_page.retake_button.click()
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Scan frame 2", "Retake frame 1")

        # A waiting rescan goes when a frame is selected: Scan (and Space) never replace unseen.
        window._show(-1)
        rescan.click()
        window._show(0)
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Scan frame 2", "Rescan frame 0")
        window._show_live()
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Scan frame 2", "Retake frame 1")

        # Replace frame 0: asked once (Space as the button); declined nothing happens, then it is sent.
        window._show(0)
        rescan.click()
        count = len(asked)
        QTest.keyClick(window.strip, Qt.Key.Key_Space)
        assert "Scan frame 0 again?" in confirms[-1] and len(asked) == count
        assert window.scan_page.scan_button.text() == "Replace frame 0"  # declined: still waiting
        window.scan_page.scan_button.click()
        assert asked[-1] == (0, False)
        assert (window.scan_page.scan_button.text(), window.scan_page.retake_button.text()) == ("Scan frame 2", "Retake frame 1")
    finally:
        window.close()
        app.processEvents()


def test_metadata_for_the_roll_and_its_frames_in_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from PySide6.QtWidgets import QLabel

    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.window.metaedit import RollForm
    from tetherinvert.tiffio import read_xmp

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    roll = rolls / "R"
    idle = lambda: not window._busy and window._pending == 0  # noqa: E731
    page, form = window.roll_page, window.roll_page.metadata_form
    try:
        page.folder_edit.setText(str(rolls))
        page.roll_edit.setText("R")
        window._choose_preset("Gold")
        window._refresh_strip()
        assert form.save_button.isHidden() and "Kept until the session starts" in form.footer.text()  # no roll yet: nothing to save
        assert "Simulated Camera" in [label.text() for label in form.findChildren(QLabel)]  # what the scans will say of the rig
        # A new roll's metadata, typed on the page, goes into roll.json when its session starts, not before.
        form.edits["film"].setText("Kodak Gold 200")
        window.activateWindow()
        assert QTest.qWaitForWindowActive(window)
        form.edits["camera"].setFocus()
        QTest.keyClicks(form.edits["camera"], "Nikon FM2")
        assert form.edits["camera"].text() == "Nikon FM2"
        form.edits["shot_from"].setText("2026-10-03")
        form.keywords.set_keywords(["berlin", "family"])
        page.roll_edit.setText("R2")
        window._refresh_strip()
        page.roll_edit.setText("R")  # a name typed on: still the one roll not started, its entries stay
        window._refresh_strip()
        assert form.edits["film"].text() == "Kodak Gold 200" and not roll.exists()
        page.start_button.click()
        assert Roll.open(roll).metadata == {"camera": "Nikon FM2", "shot_from": "2026-10-03", "film": "Kodak Gold 200", "keywords": ["berlin", "family"]}
        assert not form.save_button.isHidden() and not form.save_button.isEnabled() and window._recent()["camera"] == ["Nikon FM2"]
        for _ in range(2):
            assert cli(["--sim", "scan", str(roll), "--preset", "Gold"]) == 0
        assert "Kodak Gold 200" in read_xmp(roll / "scans" / "R_002.tif")
        # Continuing a session leaves the metadata as it is.
        window.scan_page.end_button.click()
        page.start_button.click()
        assert window._session is not None and form.edits["film"].text() == "Kodak Gold 200" and not form.edited

        # Save metadata: the files follow, the pixels stay.
        window._refresh_strip()
        pixels = tifffile.imread(roll / "scans" / "R_001.tif")
        form.edits["film"].setText("Kodak Gold 200 (expired)")
        assert form.save_button.isEnabled() and form.footer.text() == "Saving updates 2 files. Pixels are left untouched."
        form.save_button.click()
        assert not form.save_button.isEnabled() and Roll.open(roll).metadata["film"] == "Kodak Gold 200 (expired)"
        wait(app, idle)
        assert all("Kodak Gold 200 (expired)" in read_xmp(roll / "scans" / f"R_00{n}.tif") for n in (1, 2))
        assert np.array_equal(tifffile.imread(roll / "scans" / "R_001.tif"), pixels) and "Metadata written into 2 files" in window.log_bar.text()

        # The frame row: collapsed by default, one line; L or a click opens it, and that is remembered.
        window._refresh_strip()
        window._show(1)
        row = window.frame_page.frame_row
        assert not row.isHidden() and not row.is_open() and row.summary.text() == "2 keywords"
        window.activateWindow()
        assert QTest.qWaitForWindowActive(window)
        window.strip.setFocus()
        QTest.keyClick(window.strip, Qt.Key.Key_L)
        assert row.is_open() and window._settings.value("frame_metadata_open") == "true"
        row.header.click()
        assert not row.is_open() and window._settings.value("frame_metadata_open") == "false"
        row.header.click()
        assert row.edits["date"].placeholderText() == "2026-10-03 (roll)"
        # Typing into a field is typing: no correction key, no turn, no L.
        note, upright = row.edits["note"], Roll.open(roll).frame(1).orientation
        note.setFocus()
        QTest.keyClicks(note, "qawsed lom")
        assert note.text() == "qawsed lom" and window._corrections.of(1) is None and row.is_open() and Roll.open(roll).frame(1).orientation == upright
        assert window._shown == 1  # and Space neither scanned nor took the view home
        # A roll keyword left out on this frame only; a field that is not taken is marked and keeps what it had.
        row.keywords.chip("family").click()
        assert row.summary.text() == "qawsed lom · 1 keyword, 1 of the roll's left out"
        row.edits["shutter"].setText("fast")
        row.edits["aperture"].setText("5.6")
        window._save_frame_metadata()
        wait(app, idle)
        saved = Roll.open(roll).frame(1).metadata
        assert saved == {"aperture": "f/5.6", "note": "qawsed lom", "keywords_left_out": ["family"]} and Roll.open(roll).frame(2).metadata is None
        assert row.edits["shutter"].styleSheet() and "Frame 1: 'fast' is not a shutter speed" in window.log_bar.text()
        first, second = read_xmp(roll / "scans" / "R_001.tif"), read_xmp(roll / "scans" / "R_002.tif")
        assert "<rdf:li>family</rdf:li>" not in first and "<rdf:li>family</rdf:li>" in second and "qawsed lom" in first and "<exif:FNumber>28/5<" in first
        # Another frame shown: the row shows that one, after saving the first.
        row.edits["shutter"].setText("1/60")
        window._show(2)
        assert row.number == 2 and row.edits["note"].text() == "" and Roll.open(roll).frame(1).metadata["shutter"] == "1/60"
        wait(app, idle)
    finally:
        window.close()
        app.processEvents()

    # The row's state comes back with the window.
    window = MainWindow(sim=True)
    try:
        assert window.frame_page.frame_row.is_open()
    finally:
        window.close()
        app.processEvents()

    # The form itself: the push shown, a date not taken marked, Copy from roll.
    form = RollForm()
    form.copy.connect(lambda: form.copy_from({"camera": "Leica M6", "keywords": ["x"]}))
    form.set_recent({"camera": ["FM2", "F3"]})
    form.fill({"film": "HP5", "box_speed": "400", "ei": "1600"})
    assert form.push_label.text() == "push +2" and form.edits["camera"].parent().count() == 2 and not form.edited
    form.edits["shot_from"].setText("3.10.2026")
    assert form.edited and form.values() is None and not form.problem.isHidden() and "2026-10-03" in form.problem.text()
    form.copy_button.click()
    assert form.edits["camera"].text() == "Leica M6" and form.edits["film"].text() == "" and form.keywords.keywords() == ["x"] and form.edited
    form.keywords.entry.setText("lake")
    assert form.values() == {"camera": "Leica M6", "keywords": ["x", "lake"]}


def test_the_contact_sheet_from_the_finish_page(tmp_path, monkeypatch):
    import re

    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    for _ in range(2):
        assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold"]) == 0
    opened = []
    monkeypatch.setattr(MainWindow, "_open_file", lambda self, path: opened.append(path))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    idle = lambda: not window._busy and window._pending == 0  # noqa: E731
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        button = window.finish_page.contact_button
        # On Finish: the roll in the Roll fields, a frame shown or not.
        window.rail.buttons["finish"].click()
        assert window._shown == LIVE and button.isVisible() and button.isEnabled()
        assert window.finish_page.sheet_options["rebate"].isChecked() and not window.finish_page.sheet_options["sprockets"].isChecked()  # the defaults
        # The options are the roll's, in roll.json.
        # Every page of the sheet as it will be written, made on the preview thread while Finish is shown.
        pages = window.finish_page.sheet_pages
        wait(app, lambda: len(pages.pages) == 1 and window.finish_page.sheet_count.text() == "1 page", 60)
        first = pages.pages[0]
        window.finish_page.sheet_options["sprockets"].click()
        window.finish_page.sheet_options["rebate"].click()
        assert Roll.open(rolls / "R").sheet == {"sprockets": True, "rebate": False}
        assert window.finish_page.sheet_options["sprockets"].isChecked() and not window.finish_page.sheet_options["rebate"].isChecked()
        # Sprocket holes are drawn on the rebate: without it the box would change nothing.
        assert not window.finish_page.sheet_options["sprockets"].isEnabled()
        wait(app, lambda: pages.pages and pages.pages[0] is not first, 60)  # the page follows the options
        assert pages.pages[0] != first and window.finish_page.sheet_count.text() == "1 page"
        window.finish_page.sheet_options["rebate"].click()
        assert window.finish_page.sheet_options["sprockets"].isEnabled()
        roll = Roll.open(rolls / "R")
        roll.set_metadata({**roll.metadata, "format": "120 6×6"})  # 120 film has no sprocket holes
        window._update_buttons()
        assert not window.finish_page.sheet_options["sprockets"].isEnabled() and window.finish_page.sheet_options["rebate"].isEnabled()
        window.finish_page.sheet_options["rebate"].click()
        from tetherinvert import preview

        preview.path(Roll.open(rolls / "R"), 2).unlink()  # a frame without a preview is drawn empty, and the window says so
        button.click()
        assert not button.isEnabled()  # one at a time
        wait(app, lambda: idle() and opened)
        sheet = rolls / "R" / "R_contact.pdf"
        assert opened == [sheet] and sheet.read_bytes().startswith(b"%PDF") and button.isEnabled()
        assert len(re.findall(rb"/Type\s*/Page\b(?!s)", sheet.read_bytes())) == 1
        assert (rolls / "R" / "R_contact.png").read_bytes().startswith(b"\x89PNG")
        assert "Contact sheet written: R_contact.pdf, R_contact.png." in window.log_bar.text()
        assert "On the contact sheet without a picture (no preview; render them again): frame 2." in window.log_bar.text()
        # A roll without frames: nothing to put on a sheet.
        window.roll_page.roll_edit.setText("Other")
        window._refresh_strip()
        assert not button.isEnabled() and window.finish_page.sheet_options["rebate"].isChecked() and not window.finish_page.sheet_options["sprockets"].isChecked()
        assert pages.pages == [] and pages.message == "The contact sheet appears here once the roll has frames." and window.finish_page.sheet_count.text() == ""
    finally:
        window.close()
        app.processEvents()


def test_a_roll_moved_under_another_name_shows_its_pictures(tmp_path, monkeypatch):
    """A roll moved into another folder (Giro2 into Giro/2) and chosen again shows its frames' pictures."""
    import shutil

    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    for _ in range(2):
        assert cli(["--sim", "scan", str(rolls / "Giro2"), "--preset", "Gold"]) == 0
    moved = tmp_path / "Giro"
    moved.mkdir()
    shutil.move(str(rolls / "Giro2"), str(moved / "2"))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.roll_page.folder_edit.setText(str(moved))
        window.roll_page.roll_edit.setText("2")
        window._refresh_strip()
        wait(app, lambda: set(window._thumbs) == {1, 2}, 30)  # the strip's pictures
        window._show(2)
        wait(app, lambda: window._shown_seq > 0, 30)  # and the frame's
        assert window._ready_to_edit()  # its preview is found, so it can be corrected
    finally:
        window.close()


def test_a_correction_made_at_100_percent_is_shown_there(tmp_path, monkeypatch):
    """Zoomed in to 100 %, a correction is seen at once: the TIFF developed with the old ones does
    not stay up."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.cli import main as cli
    from tetherinvert.window.mainwindow import MainWindow

    rolls, base = tmp_path / "scans", ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold"]) == 0
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    view = window.frame_view
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        wait(app, lambda: set(window._thumbs) == {1}, 30)
        window._show(1)
        wait(app, lambda: window._shown_seq > 0, 30)
        QTest.mouseDClick(view, Qt.MouseButton.LeftButton)
        wait(app, lambda: view.full is not None, 30)
        before = view.full
        size = lambda image: (view._area(image).width(), view._area(image).height())  # noqa: E731
        full = size(before)
        seen = window._shown_seq
        for _ in range(3):
            QTest.keyClick(window.strip, Qt.Key.Key_Q)  # D +3: denser
        wait(app, lambda: window._shown_seq > seen, 30)
        # At once: the corrected preview, standing in at the TIFF's size; the old TIFF is gone.
        assert view.zoomed and view.full is None and size(view.image) == pytest.approx(full, abs=1)  # the preview's rows rounded
        # Once the keys rest: the TIFF again, with the corrections.
        wait(app, lambda: view.full is not None, 30)
        assert view.full.width() == before.width() and view.full != before and size(view.full) == full
        assert window._developed(1, shown=True).adjust["d"] == 3
        # Zoomed out, nothing of the TIFF is kept.
        QTest.mouseDClick(view, Qt.MouseButton.LeftButton)
        assert not view.zoomed and view.full is None and size(view.image)[0] < full[0]
    finally:
        window.close()


def test_the_help_menu_opens_the_manual_shows_the_shortcuts_runs_the_doctor_and_the_version(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert import __version__, about
    from tetherinvert.window.mainwindow import MainWindow

    opened, abouts = [], []
    monkeypatch.setattr(MainWindow, "_open_url", lambda self, url: opened.append(url))
    monkeypatch.setattr(MainWindow, "_ask_about", lambda self, html, plain: abouts.append((html, plain)))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        actions = {action.text(): action for action in window.help_menu.actions() if action.text()}
        assert list(actions) == ["TetherInvert Manual", "Keyboard Shortcuts", "Run Doctor…", "About TetherInvert"]
        actions["TetherInvert Manual"].trigger()
        assert opened == [about.MANUAL_URL]
        actions["Keyboard Shortcuts"].trigger()
        dialog = window._shortcuts
        assert dialog is not None and dialog.isVisible() and dialog.windowTitle() == "Keyboard Shortcuts"
        from PySide6.QtSvgWidgets import QSvgWidget

        drawing = dialog.findChild(QSvgWidget)
        size = drawing.renderer().defaultSize()
        assert drawing.renderer().isValid() and abs(drawing.width() / drawing.height() - size.width() / size.height()) < 0.01  # never stretched
        actions["Keyboard Shortcuts"].trigger()
        assert window._shortcuts is dialog  # one window, brought to the front again
        wait(app, lambda: window._camera_connected)  # the doctor uses the camera the window holds: nothing to close first
        actions["Run Doctor…"].trigger()
        doctor = window._doctor
        assert doctor.isVisible() and not actions["Run Doctor…"].isEnabled() and not doctor.copy_button.isEnabled()
        wait(app, lambda: not window._doctor_running)
        report = doctor.report.toPlainText()
        assert "decodes: yes" in report and "RESULT: this camera's files decode" in report and (tmp_path / "home" / "doctor" / "test-shot.dng").exists()
        assert actions["Run Doctor…"].isEnabled() and doctor.copy_button.isEnabled() and doctor.shot_button.isEnabled()
        doctor.copy_button.click()
        assert QApplication.clipboard().text() == about.text() + "\n\n" + report
        actions["About TetherInvert"].trigger()
        html, plain = abouts[-1]
        assert f"TetherInvert {__version__}" in html and "Build" in html and about.REPOSITORY in html and plain == about.text()
    finally:
        window.close()
        app.processEvents()


def test_a_debugger_is_told_of_every_thread_the_window_starts(monkeypatch):
    """debugpy stops only in threads it was told of; the window's are QThreads, not `threading` ones."""
    import sys
    import threading
    import types

    from tetherinvert.window.mainwindow import MainWindow

    told = []
    monkeypatch.setitem(sys.modules, "debugpy", types.SimpleNamespace(debug_this_thread=lambda: told.append(threading.get_ident())))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    try:
        wait(app, lambda: len(told) == 3, seconds=10)
        assert len(set(told)) == 3 and threading.main_thread().ident not in told
    finally:
        window.close()
        app.processEvents()


def test_text_can_be_read_on_every_ground_in_both_appearances():
    """WCAG's 4.5 : 1 for text, in light and in dark: the muted hints too, white on the accent, and
    green and red, which say whether a device is there."""
    from tetherinvert.window import style

    for tokens in (style.LIGHT, style.DARK):
        for ground in (tokens.chrome, tokens.panel):
            for text in (tokens.text, tokens.text2, tokens.muted, tokens.accent_text, tokens.ok, tokens.danger):
                assert style.contrast(text, ground) >= 4.5, (text, ground)
        assert style.contrast(tokens.on_accent, tokens.accent) >= 4.5
        assert style.contrast(tokens.on_accent, tokens.alert) >= 4.5  # a red count
        assert style.contrast(tokens.danger, tokens.danger_ground) >= 4.5  # a warning in the log
        for text in (tokens.muted, tokens.danger):  # the status line's time; a warning there
            assert style.contrast(text, tokens.rail) >= 4.5
        assert style.contrast(tokens.warning, tokens.warning_ground) >= 4.5
        assert style.contrast(tokens.text, tokens.rail) >= 4.5


def rolled(tmp_path, frames: int = 2) -> Path:
    """A roll of `frames` frames scanned on the command line with preset Gold; returns the rolls' folder."""
    from tetherinvert.cli import main as cli

    rolls = tmp_path / "scans"
    base, picture = ",".join(str(v) for v in sim.BASE_ROI), ",".join(f"{v:.3f}" for v in sim.PICTURE_RECT)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    for _ in range(frames):
        assert cli(["--sim", "scan", str(rolls / "R"), "--preset", "Gold", "--picture", picture]) == 0
    return rolls


def test_the_rail_opens_every_step_and_the_strip_and_the_keys_agree_with_it(tmp_path, monkeypatch):
    """Each step shows its page. A frame clicked in the strip opens Develop, which opens the frame looked
    at last; Esc, Home and the strip's Live go back to Scan."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    pages = {"roll": window.roll_page, "scan": window.scan_page, "develop": window.frame_page, "finish": window.finish_page, "presets": window.presets_page}
    showing = lambda: [step for step, page in pages.items() if page.isVisible()]  # noqa: E731
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        for step in pages:
            window.rail.buttons[step].click()
            assert showing() == [step] and window.rail.buttons[step].isChecked()
        for step in ("roll", "finish"):
            window.rail.buttons[step].click()
            assert not window.view.isVisible() and not window.strip.isVisible()  # no picture on these
        window.rail.buttons["develop"].click()
        assert window.frame_page.frame_info.text().startswith("Frame 2")  # none looked at yet: the last
        window.rail.buttons["scan"].click()
        assert showing() == ["scan"] and window.view.isVisible()
        window.strip.setCurrentRow(1)  # frame 1, by a click in the strip
        assert showing() == ["develop"] and window.frame_page.frame_info.text().startswith("Frame 1")
        window.activateWindow()  # shortcuts fire only in the active window, once it is
        app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert showing() == ["scan"] and window.rail.buttons["scan"].isChecked()
        window.rail.buttons["develop"].click()
        assert window.frame_page.frame_info.text().startswith("Frame 1")  # the one looked at last
        QTest.keyClick(window, Qt.Key.Key_Home)
        assert showing() == ["scan"]
        window.rail.buttons["develop"].click()
        window.strip.setCurrentRow(0)  # Live
        assert showing() == ["scan"]
        # The counts: frames scanned on Scan, frames to print on Develop, until they are printed.
        badges = lambda: (window.rail.buttons["scan"].badge, window.rail.buttons["develop"].badge)  # noqa: E731
        assert badges() == ("2", "2")
        window.finish_page.print_button.click()
        wait(app, lambda: "Wrote 2 prints." in window.log_bar.text())
        wait(app, lambda: badges() == ("2", ""), 10)
    finally:
        window.close()
        app.processEvents()


def test_the_window_writes_in_its_own_fonts():
    """Instrument Sans for text, IBM Plex Mono for numbers, both shipped with TetherInvert (SIL OFL 1.1,
    the licences beside them): the system's fonts differ from Mac to Linux."""
    from tetherinvert.window import style
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    try:
        assert QApplication.font().family() == "Instrument Sans" and window.roll_page.note.font().family() == "Instrument Sans"
        assert style.mono_family() == "IBM Plex Mono"
        assert {path.name for path in style.FONTS.glob("OFL-*.txt")} == {"OFL-InstrumentSans.txt", "OFL-IBMPlexMono.txt"}
    finally:
        window.close()
        app.processEvents()


def test_the_log_shows_each_kind_and_the_warnings_alone_and_copies_what_it_shows(tmp_path, monkeypatch):
    """A file written has its tick, a warning a red row, information a dot. The kind is the caller's,
    not read from the words: the render thread's own "Wrote 1 print." is a file written. All and
    Warnings say how many, and Copy takes the lines shown, with their times and marks."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window import style
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path, frames=1)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        window._say("Session started. Put the film in and scan.")
        window._on_failed("The camera does not answer any more. It is connected again as soon as it is back.")
        window._write_prints()
        wait(app, lambda: not window._pending and "Wrote 1 print." in window.log_bar.text())
        panel = window.log_bar.panel
        said = [(entry.kind, entry.text) for entry in panel.entries]
        assert ("info", "Printing 1 frame into prints/…") in said and ("file", "Wrote 1 print.") in said
        window.log_bar.log_button.click()
        app.processEvents()
        rows = {text: (mark, ground) for mark, ground, text in panel.view.rows()}
        assert rows["Wrote 1 print."] == ("file", "")
        assert rows["Session started. Put the film in and scan."] == ("info", "")
        assert rows["The camera does not answer any more. It is connected again as soon as it is back."] == ("warning", style.tokens().danger_ground)
        assert len(rows) == len(said) and panel.all_button.count.text() == str(len(said))
        warnings = panel.warnings_button.count
        assert warnings.text() == "1" and warnings.property("role") == "count"

        panel.warnings_button.click()
        assert [text for _, _, text in panel.view.rows()] == ["The camera does not answer any more. It is connected again as soon as it is back."]
        panel.copy_button.click()
        clock = panel.entries[1].clock()
        assert QApplication.clipboard().text() == f"{clock} ! The camera does not answer any more. It is connected again as soon as it is back."
        panel.all_button.click()
        assert len(panel.view.rows()) == len(said)
        panel.copy_button.click()
        copied = QApplication.clipboard().text().split("\n")
        assert len(copied) == len(said) and copied[0].endswith("   Session started. Put the film in and scan.")
        assert any(line.endswith(" ✓ Wrote 1 print.") for line in copied)
    finally:
        window.close()
        app.processEvents()


def test_a_warning_turns_the_status_line_red_and_leaves_a_count_on_log_until_the_log_is_opened():
    """Red for 8 s, or until a line that is not a warning takes its place; the count of warnings not seen
    yet stays on Log until the panel is opened. One that comes while it is open is seen there."""
    from tetherinvert.window.log import WARNING_S
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        bar = window.log_bar
        count = lambda: bar.log_button.count.text() if bar.log_button.count.isVisibleTo(bar.log_button) else ""  # noqa: E731
        red = lambda: [label.property("tone") for label in (bar.time, bar.status)] == ["danger", "danger"]  # noqa: E731
        window._say("Session started. Put the film in and scan.")
        assert not red() and count() == ""
        window._on_failed("The Scanlight does not answer any more. It is connected again as soon as it is back.")
        assert red() and bar.status.text().startswith("The Scanlight") and count() == "1"
        assert bar._red.isActive() and bar._red.interval() == WARNING_S * 1000 == 8000
        bar._red.timeout.emit()  # 8 s later
        assert not red() and count() == "1"
        window._on_failed("The camera does not answer any more. It is connected again as soon as it is back.")
        assert red() and count() == "2"
        window._say("Frame 4 captured.")  # red under this line would say it went wrong
        assert not red() and count() == "2"

        bar.log_button.click()
        app.processEvents()
        assert bar.panel.isVisible() and bar.log_button.isChecked() and count() == ""
        window._on_failed("Frame 4 could not be rendered: no RAW files")
        assert red() and count() == ""  # seen as it came
        bar.panel.close_button.click()
        assert not bar.panel.isVisible() and not bar.log_button.isChecked() and count() == ""
    finally:
        window.close()
        app.processEvents()


def test_the_log_keeps_its_last_lines_and_says_since_when(monkeypatch):
    """The oldest lines go beyond the limit, from the panel too, whichever lines it shows; "since" is the
    window's start until then, the oldest line kept after."""
    from tetherinvert.window import log
    from tetherinvert.window.mainwindow import MainWindow

    monkeypatch.setattr(log, "LINES", 5)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        bar, panel = window.log_bar, window.log_bar.panel
        assert panel.since.text().endswith(", when the window opened")
        bar.open_log()
        panel.warnings_button.click()
        for n in range(7):
            (window._on_failed if n % 3 != 1 else window._say)(f"line {n}")
        assert [entry.text for entry in panel.entries] == [f"line {n}" for n in range(2, 7)]
        assert [text for _, _, text in panel.view.rows()] == ["line 2", "line 3", "line 5", "line 6"]
        assert panel.since.text() == f"The last 5 lines, since {time.strftime('%H:%M', time.localtime(panel.entries[0].when))}"
        panel.all_button.click()
        assert [text for _, _, text in panel.view.rows()] == [f"line {n}" for n in range(2, 7)]
        assert bar.text().split("\n")[0].endswith("  line 2") and len(bar.text().split("\n")) == 5
        assert panel.all_button.count.text() == "5" and panel.warnings_button.count.text() == "4"
    finally:
        window.close()
        app.processEvents()


def test_the_log_follows_the_appearance(monkeypatch):
    """The panel's lines carry their colours in the document, not by the stylesheet: written again
    when the system's appearance changes, lines kept."""
    from tetherinvert.window import style
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window._on_failed("The camera does not answer any more. It is connected again as soon as it is back.")
        window._say("Frame 4 captured.")
        view = window.log_bar.panel.view
        assert [ground for _, ground, _ in view.rows()] == [style.LIGHT.danger_ground, ""]
        monkeypatch.setattr(style, "dark", lambda: True)
        style._restyle(app)  # as when the system turns dark
        app.processEvents()
        assert [ground for _, ground, _ in view.rows()] == [style.DARK.danger_ground, ""]
        assert [text for _, _, text in view.rows()][1:] == ["Frame 4 captured."]
    finally:
        monkeypatch.undo()
        style._restyle(app)
        window.close()
        app.processEvents()


def test_space_and_esc_keep_their_meaning_with_the_log_open(monkeypatch):
    """The panel stays open while working: Space still scans and Esc still opens Scan, also with the
    focus in its lines (read-only: no key of the window's is kept there)."""
    from tetherinvert.window.mainwindow import MainWindow

    spaces = []
    monkeypatch.setattr(MainWindow, "_space", lambda self: spaces.append(self._step))
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.activateWindow()  # shortcuts fire only in the active window
        window.rail.buttons["finish"].click()
        window._say("Contact sheet written: R_contact.pdf, R_contact.png.")
        window.log_bar.log_button.click()
        view = window.log_bar.panel.view
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton)
        app.processEvents()
        assert view.hasFocus()
        QTest.keyClick(view, Qt.Key.Key_Space)
        assert spaces == ["finish"]
        QTest.keyClick(view, Qt.Key.Key_Escape)
        app.processEvents()
        assert window._step == "scan" and window.log_bar.panel.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_the_popovers_open_inside_the_window():
    """Devices sits at the rail's foot: beside it, the popover would hang below the window. Each is
    kept inside the window, whatever its size."""
    from tetherinvert.window.mainwindow import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    try:
        window.resize(window.minimumSize())
        app.processEvents()
        inside = window.geometry()
        for name in ("lightroom", "devices"):
            window._open_popover(name)
            app.processEvents()
            popover = window._popovers[name]
            assert inside.contains(popover.geometry()), (name, popover.geometry(), inside)
            popover.close()
    finally:
        window.close()
        app.processEvents()


def test_frames_and_prints_are_named_as_on_the_film():
    """Runs of three or more as a span; 00 and 0 as the film prints them."""
    from tetherinvert.window.finish import frames_text, prints_line

    assert frames_text([1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 14, 15]) == "Frames 1–7, 9–12, 14 and 15"
    assert frames_text([-1]) == "Frame 00" and frames_text([-1, 0, 1]) == "Frames 00–1" and frames_text([4, 2]) == "Frames 2 and 4"
    assert prints_line([], [], []) == "Every print is up to date. 16-bit TIFF into prints/."
    assert prints_line([3], [5, 6], [7]) == (
        "Frame 3 changed since it was printed. Frames 5 and 6 have no print yet. Frame 7 is not ready to print: not rendered yet. 16-bit TIFF into prints/."
    )


def test_the_prints_are_made_on_finish_and_its_count_follows_them(tmp_path, monkeypatch):
    """Finish says how many prints are up to date, which frames changed since they were printed and
    which have none; Print N frames prints them. Develop has no print button: the rail's count says
    how many are due. A roll not started, and no roll, have nothing to print."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    page = window.finish_page
    shown = lambda: (page.prints_count.text(), page.prints_line.text(), page.print_button.text(), page.print_button.isEnabled())  # noqa: E731
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        window.rail.buttons["finish"].click()
        assert shown() == ("0 / 2", "Frames 1 and 2 have no print yet. 16-bit TIFF into prints/.", "Print 2 frames", True)
        steps = []
        window._renderer.print_step.connect(lambda _folder, done, total: steps.append((done, total)))
        page.print_button.click()
        assert shown()[2:] == ("Printing 1 of 2…", False)  # at once, before the first print is written
        wait(app, lambda: shown() == ("2 / 2", "Every print is up to date. 16-bit TIFF into prints/.", "Prints up to date", False))
        assert steps == [(1, 2), (2, 2)]  # told after each print, not only at the end
        assert page.prints_meter.fraction == 1.0 and window.rail.buttons["develop"].badge == ""
        window._set_edits(1, {**invert.NEUTRAL, "d": 2})  # corrected since it was printed
        window._save_edits()
        assert shown() == ("1 / 2", "Frame 1 changed since it was printed. 16-bit TIFF into prints/.", "Print 1 frame", True)
        assert window.rail.buttons["develop"].badge == "1" and page.prints_meter.fraction == 0.5
        window.roll_page.roll_edit.setText("New")
        window._refresh_strip()
        window._update_buttons()
        assert shown() == ("0 / 0", "New has no frames yet. Its session starts on Roll.", "Print", False)
        assert page.metadata_note.text() == "Written when its session starts; the fields are on Roll." and not page.contact_button.isEnabled()
        window.roll_page.roll_edit.setText("")
        window._refresh_strip()
        window._update_buttons()
        assert shown() == ("0 / 0", "No roll chosen: name one on Roll.", "Print", False) and page.path_label.text() == "—"
    finally:
        window.close()
        app.processEvents()


def test_edit_on_finish_opens_the_roll_form_and_what_is_saved_there_shows_on_finish(tmp_path, monkeypatch):
    """The roll's metadata is shown on Finish, read-only; Edit… opens the one form on Roll, with its first
    field to type in, and Save metadata there is what Finish shows."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow

    rolls = rolled(tmp_path, frames=1)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    window.activateWindow()  # focus goes to a field only in the active window
    page, form = window.finish_page, window.roll_page.metadata_form
    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        window.rail.buttons["finish"].click()
        assert page.metadata_values["camera"].text() == "—" and page.metadata_note.text() == "Written into every TIFF of the roll."
        assert page.path_label.toolTip() == str(rolls / "R")
        page.edit_button.click()
        wait(app, lambda: form.edits["camera"].hasFocus(), 10)  # the box it is in has the focus for it
        assert window._step == "roll" and window.roll_page.isVisible()
        form.edits["camera"].setText("Nikon F5")
        form.edits["shot_from"].setText("2026-10-03")
        form.edits["shot_to"].setText("2026-10-05")
        form.save_button.click()
        wait(app, lambda: not window._pending)
        window.rail.buttons["finish"].click()
        assert page.metadata_values["camera"].text() == "Nikon F5" and page.metadata_values["shot"].text() == "3–5 Oct 2026"
        assert page.metadata_values["camera"].property("role") == "" and page.metadata_values["lens"].property("role") == "muted"
    finally:
        window.close()
        app.processEvents()


def test_the_lightroom_popover_says_what_lightroom_watches_and_stop_watching_asks_for_nothing(tmp_path, monkeypatch):
    """The chip and the line in each state: not watching, asking, watching this roll, watching another,
    no answer. Stop watching leaves a request without folders, and the answer, or none at all (Lightroom
    is not running), leaves nothing watched. The dot is green while Lightroom watches."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    from tetherinvert import lightroom
    from tetherinvert.cli import main as cli
    from tetherinvert.window import lightroompanel
    from tetherinvert.window.mainwindow import MainWindow

    monkeypatch.setattr(lightroom, "modules_folder", lambda: tmp_path / "Modules")
    launched = []
    monkeypatch.setattr(lightroom, "launch", lambda: launched.append(1))
    monkeypatch.setattr(MainWindow, "_confirm", lambda self, title, text: True)
    rolls = rolled(tmp_path, frames=1)
    assert cli(["--sim", "scan", str(rolls / "S"), "--preset", "Gold"]) == 0
    exchange = tmp_path / "userhome" / ".tetherinvert" / "lightroom"
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    panel, dot = window.lightroom_panel, lambda: window.rail.buttons["lightroom"].dot  # noqa: E731
    state = lambda: (panel.status.text(), panel.status.property("tone"), panel.lightroom_note.text())  # noqa: E731

    def answer(watching: bool, *folders: Path) -> None:
        token = (exchange / "request.txt").read_text().split("\n")[0]
        (exchange / "status.txt").write_text("\n".join(["watching" if watching else "idle", "0", "0.4.0", token, *map(str, folders)]) + "\n")

    try:
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        assert state() == ("Not watching", "off", "Open hands Lightroom R's prints: it adds every TIFF written there to the catalog, where it is.")
        assert dot() == "off" and not panel.stop_button.isEnabled() and panel.lightroom_button.isEnabled()
        panel.lightroom_button.click()
        assert state() == ("Asking…", "warning", "Waiting for Lightroom Classic to pick up the request.") and launched == [1]
        assert not panel.lightroom_button.isEnabled() and not panel.stop_button.isEnabled() and not panel.lightroom_prints.isEnabled()
        answer(True, rolls / "R" / "prints")
        wait(app, lambda: state()[0] == "Watching", 10)
        assert state() == ("Watching", "ok", "Lightroom adds R's prints as they appear.") and dot() == "ok" and panel.stop_button.isEnabled()
        window.roll_page.roll_edit.setText("S")  # another roll in the fields
        assert state() == ("Watching R", "ok", "Lightroom adds R's prints as they appear, not S's. Open hands it S's prints instead.")

        # Stop watching: a request without folders, Lightroom not started for it.
        panel.stop_button.click()
        token, *folders = (exchange / "request.txt").read_text().split("\n")
        assert folders == [""] and launched == [1] and state()[0] == "Asking…"
        answer(False)
        wait(app, lambda: state()[0] == "Not watching", 10)
        assert dot() == "off" and window._watched == [] and "Lightroom Classic stopped watching." in window.log_bar.text()

        # No answer to Open: said in the chip until the next request.
        monkeypatch.setattr(lightroompanel, "OPEN_WAIT_S", 0.0)
        panel.lightroom_button.click()
        wait(app, lambda: state()[0] == "No answer", 10)
        assert state() == ("No answer", "danger", lightroompanel.NO_ANSWER) and dot() == "off" and panel.lightroom_button.isEnabled()
        assert not panel.stop_button.isEnabled()

        # No answer to Stop watching: Lightroom is not running, so it watches nothing.
        monkeypatch.setattr(lightroompanel, "OPEN_WAIT_S", 120.0)
        panel.lightroom_button.click()
        answer(True, rolls / "S" / "prints")
        wait(app, lambda: state()[0] == "Watching", 10)
        monkeypatch.setattr(lightroompanel, "STOP_WAIT_S", 0.0)
        panel.stop_button.click()
        wait(app, lambda: state()[0] == "Not watching", 10)
        assert dot() == "off" and "it is not running, so it watches nothing" in window.log_bar.text()
    finally:
        window.close()
        app.processEvents()


def test_the_devices_dot_is_grey_where_the_devices_are_not_needed(tmp_path, monkeypatch):
    """Red or green on Roll and Scan and in the wizard, which use the camera and the light; grey on
    Develop, Finish and the Presets list, the popover saying they are not needed there."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    from tetherinvert.window.mainwindow import MainWindow
    from tetherinvert.workers import NOT_CONNECTED

    rolls = rolled(tmp_path, frames=1)
    app = QApplication.instance() or QApplication([])
    window = MainWindow(sim=True)
    window.show()
    panel = window.devices_panel
    shown = lambda: (window.rail.buttons["devices"].dot, panel.status.text())  # noqa: E731
    try:
        wait(app, lambda: window._camera_connected and "Scanlight" in panel.light_label.text(), 30)
        window.roll_page.folder_edit.setText(str(rolls))
        window.roll_page.roll_edit.setText("R")
        window._refresh_strip()
        expected = {"roll": "ok", "scan": "ok", "develop": "off", "finish": "off", "presets": "off"}
        for step, tone in expected.items():
            window.rail.buttons[step].click()
            assert shown() == (tone, "Ready" if tone == "ok" else "Not needed here"), step
        assert panel.note.text() == "Not needed to develop or finish the roll. Scan, Roll and a new preset need them."
        window.presets_page.new_preset_button.click()  # the wizard calibrates with them
        assert shown() == ("ok", "Ready") and panel.note.text() == "Needed to scan, to start a session and to calibrate."
        window.wizard_column.wizard_panel.cancel_button.click()
        window.rail.buttons["finish"].click()
        window._on_devices(NOT_CONNECTED, window._light_text)  # the camera lost on Finish: grey still, the row red
        assert shown() == ("off", "Not needed here") and panel.camera_label.property("tone") == "danger"
        window.rail.buttons["scan"].click()
        assert shown() == ("danger", "Camera missing")
    finally:
        window.close()
        app.processEvents()
