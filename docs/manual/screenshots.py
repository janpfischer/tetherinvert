"""Makes the manual's screenshots: the real window on the simulated rig, driven step by step.

    python docs/manual/screenshots.py            # writes docs/manual/img/*.png, and docs/hero.png for the README
    TETHERINVERT_SIM_PICTURES=~/photos python docs/manual/screenshots.py   # frames showing other photos

The simulated roll's frames show the photos in docs/manual/sim-pictures unless
TETHERINVERT_SIM_PICTURES names another folder.

Every picture is taken from the window as it is, offscreen at twice the size, with numbered markers
drawn where the widgets are now: run it again after the window changes and the pictures and their
markers follow. The manual's text refers to the markers by number. Nothing is written outside a
temporary folder except the pictures; the window's settings, presets and Lightroom's plug-in folder
are all temporary.
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE / "img"
TMP = Path(tempfile.mkdtemp(prefix="tetherinvert-manual-"))
os.environ["TETHERINVERT_SIM_PICTURES"] = str(  # before HOME moves: ~ is the user's
    Path(os.environ.get("TETHERINVERT_SIM_PICTURES") or HERE / "sim-pictures").expanduser().resolve()
)
os.environ.update(
    QT_QPA_PLATFORM="offscreen",
    QT_SCALE_FACTOR="2",  # pictures at twice the size: sharp on any screen
    TETHERINVERT_HOME=str(TMP / "home"),
    HOME=str(TMP / "home"),
    TETHERINVERT_SIM_MANUAL_LENS="1",  # a manual lens: the f-stop is asked for
)
os.environ.pop("TETHERINVERT_NO_LIGHT", None)
sys.path.insert(0, str(HERE.parent.parent))

from PySide6.QtCore import QPoint, QRect, QRectF, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from tetherinvert import lightroom, sim  # noqa: E402
from tetherinvert.window import mainwindow, style  # noqa: E402
from tetherinvert.wizard import DONE  # noqa: E402

MARK = QColor("#e8590c")  # the markers: orange, like the edge print on colour film
MARGIN = 14  # around a cropped picture, in window pixels
GUTTER = 30  # added around every picture, for the badges
ROLLS = TMP / "home" / "Scans"  # shown as ~/Scans

mainwindow.QSettings = lambda org, app: QSettings(str(TMP / "config" / f"{app}.ini"), QSettings.Format.IniFormat)
lightroom.modules_folder = lambda: TMP / "Modules"
lightroom.launch = lambda: None
mainwindow.MainWindow._confirm = lambda self, title, text: True  # installing the plug-in, deleting: yes
opened: list[Path] = []
mainwindow.MainWindow._open_file = lambda self, path: opened.append(path)

app = QApplication.instance() or QApplication([])
LIGHT = style.dark  # the system's appearance; offscreen: light


def wait(condition, seconds: float = 180.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("the window did not get there")
        app.processEvents()
        time.sleep(0.01)


def settle(seconds: float = 0.3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def area(widget: QWidget, top: QWidget) -> QRect:
    """Where a widget is in `top`, in its pixels: a widget of a popover too, which is a window of its own."""
    return QRect(widget.mapToGlobal(QPoint(0, 0)) - top.mapToGlobal(QPoint(0, 0)), widget.size())


def union(rects: list[QRect]) -> QRect:
    out = QRect(rects[0])
    for rect in rects[1:]:
        out = out.united(rect)
    return out


class At:
    """A mark whose badge goes inside a given corner of its outline ("tr", "br", "tl", "bl"), where the
    automatic choice would cover text."""

    def __init__(self, item, corner: str) -> None:
        self.item, self.corner = item, corner


def shot(name: str, top: QWidget, marks: list = (), crop: list = (), width: int = 0, over: list = (), cut: tuple = ()) -> None:
    """Grab `top`, draw numbered markers, crop to the given widgets or rectangles, save as name.png.
    A mark or crop entry is a widget, a QRect in `top`'s pixels, a list of either (their union), or At.
    `over`: popovers open on it, painted where they are (a popover is a window of its own, not in
    top.grab()). `cut`: two widgets; the rows between them are left out (the rail's empty middle)."""

    def rect(item) -> QRect:
        if isinstance(item, At):
            return rect(item.item)
        if isinstance(item, (list, tuple)):
            return union([rect(part) for part in item])
        if isinstance(item, QRectF):
            return item.toAlignedRect()
        return (QRect(item) if isinstance(item, QRect) else area(item, top)).translated(GUTTER, GUTTER)

    app.processEvents()
    grabbed: QPixmap = top.grab()
    ratio = grabbed.devicePixelRatio()
    # A gutter all round, in the window's own colour, so a badge always has room next to the window's edges.
    picture = QPixmap(int((top.width() + 2 * GUTTER) * ratio), int((top.height() + 2 * GUTTER) * ratio))
    picture.setDevicePixelRatio(ratio)
    picture.fill(top.palette().window().color())
    painter = QPainter(picture)
    painter.drawPixmap(GUTTER, GUTTER, grabbed)
    for popover in over:
        painter.drawPixmap(area(popover, top).topLeft() + QPoint(GUTTER, GUTTER), popover.grab())
    painter.end()
    plain = picture.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    pixels = np.frombuffer(plain.constBits(), np.uint8).reshape(plain.height(), plain.bytesPerLine())

    def busy(place: QRect) -> int:  # how much is under it: 0 on plain background, high on text or edges
        patch = pixels[int(place.top() * ratio): int((place.bottom() + 1) * ratio), int(place.left() * ratio): int((place.right() + 1) * ratio)]
        return int(patch.std() * 10 + (int(patch.max()) - int(patch.min()))) if patch.size else 10**6

    bounds = QRect(0, 0, top.width() + 2 * GUTTER, top.height() + 2 * GUTTER)
    boxes = [rect(item).adjusted(-3, -3, 3, 3) for item in marks]
    badges: list[QRect] = []
    size = 22
    for item, box in zip(marks, boxes, strict=True):
        if isinstance(item, At):
            x = box.right() - size - 3 if "r" in item.corner else box.left() + 3
            y = box.bottom() - size - 3 if "b" in item.corner else box.top() + 3
            badges.append(QRect(x, y, size, size))
            continue
        # Next to its outline: left, right, above or below, inside the picture and clear of every outline and
        # badge; of those the first over plain background, else the one over the least; else inside the corner.
        mid = box.top() + min(box.height(), 2 * size) // 2 - size // 2
        places = [QRect(box.left() - size - 4, mid, size, size), QRect(box.right() + 5, mid, size, size),
                  QRect(box.left(), box.top() - size - 4, size, size), QRect(box.left(), box.bottom() + 5, size, size)]  # fmt: skip
        clear = [p for p in places if bounds.contains(p) and not any(p.intersects(other) for other in boxes + badges)]
        quiet = [p for p in clear if busy(p) < 16]
        badges.append(quiet[0] if quiet else min(clear, key=busy) if clear else QRect(box.right() - size - 3, box.top() + 3, size, size))

    painter = QPainter(picture)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(13)
    painter.setFont(font)
    for box in boxes:
        painter.setPen(QPen(MARK, 2.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 5, 5)
    for number, badge in enumerate(badges, 1):
        painter.setPen(QPen(QColor("white"), 1.5))
        painter.setBrush(MARK)
        painter.drawEllipse(badge)
        painter.setPen(QColor("white"))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(number))
    painter.end()
    keep = union([rect(list(crop))] + badges).adjusted(-MARGIN, -MARGIN, MARGIN, MARGIN) if crop else union([bounds] + badges)
    keep = keep.intersected(bounds)
    if cut:  # the rows between two widgets left out, a line where they were
        start, end = rect(cut[0]).bottom() + MARGIN, rect(cut[1]).top() - MARGIN
        joined = QPixmap(picture.width(), picture.height() - int((end - start) * ratio))
        joined.setDevicePixelRatio(ratio)
        painter = QPainter(joined)
        painter.drawPixmap(QPoint(0, 0), picture.copy(0, 0, picture.width(), int(start * ratio)))
        painter.drawPixmap(QPoint(0, start), picture.copy(0, int(end * ratio), picture.width(), picture.height() - int(end * ratio)))
        painter.setPen(QPen(top.palette().mid().color(), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, start, joined.width(), start)
        painter.end()
        picture = joined
        keep.setBottom(keep.bottom() - (end - start))
    picture = picture.copy(QRect(int(keep.x() * ratio), int(keep.y() * ratio), int(keep.width() * ratio), int(keep.height() * ratio)))
    image = picture.toImage()
    if width and image.width() > width:
        image = image.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    image.save(str(OUT / f"{name}.png"), "PNG", 9)
    print(f"  {name}.png  {image.width()}×{image.height()}" + (f"  markers 1–{len(marks)}" if marks else ""))


def hero(live: QImage, develop: QImage, sheet: QImage) -> None:
    """The README's picture: the live view and a frame being developed side by side, the contact sheet
    under them, on a dark ground, without markers. Saved as docs/hero.png."""
    width, pad, gap, bar, radius = 2400, 72, 56, 34, 12
    win_w = (width - 2 * pad - gap) // 2
    win_h = round(win_w * live.height() / live.width()) + bar
    ink = np.frombuffer(sheet.convertToFormat(QImage.Format.Format_Grayscale8).constBits(), np.uint8)
    ink = ink.reshape(sheet.height(), -1)[:, : sheet.width()] < 245  # the sheet's ink, without its white page
    rows, cols = np.flatnonzero(ink.any(axis=1)), np.flatnonzero(ink.any(axis=0))
    margin = 40
    sheet = sheet.copy(QRect(int(cols[0]) - margin, int(rows[0]) - margin, int(cols[-1] - cols[0]) + 2 * margin, int(rows[-1] - rows[0]) + 2 * margin))
    sheet_w = width - 2 * pad
    sheet_h = round(sheet_w * sheet.height() / sheet.width())
    caption_h = 64
    height = pad + win_h + caption_h + gap // 2 + sheet_h + caption_h + pad // 2

    picture = QImage(width, height, QImage.Format.Format_RGB32)
    picture.fill(QColor("#1c1d20"))
    painter = QPainter(picture)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

    def card(place: QRect, image: QImage, title: str = "") -> None:
        for spread in range(18, 0, -3):  # a soft shadow
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 14))
            painter.drawRoundedRect(QRectF(place).adjusted(-spread, -spread + 8, spread, spread + 8), radius + spread, radius + spread)
        painter.save()
        path = QPainterPath()
        path.addRoundedRect(QRectF(place), radius, radius)
        painter.setClipPath(path)
        body = QRect(place)
        if title:  # a window's title bar
            painter.fillRect(QRect(place.x(), place.y(), place.width(), bar), QColor("#e4e4e4"))
            for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
                painter.setBrush(QColor(colour))
                painter.drawEllipse(QRectF(place.x() + 16 + i * 22, place.y() + bar / 2 - 6.5, 13, 13))
            font = QFont(painter.font())
            font.setPixelSize(16)
            painter.setFont(font)
            painter.setPen(QColor("#555"))
            painter.drawText(QRect(place.x(), place.y(), place.width(), bar), Qt.AlignmentFlag.AlignCenter, title)
            body.setTop(place.y() + bar)
        else:
            painter.fillRect(place, QColor("white"))
        painter.drawImage(body, image.scaled(body.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
        painter.restore()

    def caption(x: int, y: int, w: int, head: str, text: str) -> None:
        font = QFont(painter.font())
        font.setPixelSize(26)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(MARK)
        painter.drawText(QRect(x, y, w, caption_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, head)
        lead = painter.fontMetrics().horizontalAdvance(head + "  ")
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#c8cad0"))
        painter.drawText(QRect(x + lead, y, w - lead, caption_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    left, right = pad, pad + win_w + gap
    card(QRect(left, pad, win_w, win_h), live, "TetherInvert")
    card(QRect(right, pad, win_w, win_h), develop, "TetherInvert")
    y = pad + win_h
    caption(left, y, win_w, "Scan", "the negative live, the frame just scanned in its corner")
    caption(right, y, win_w, "Develop", "corrections like a Frontier's, printed to 16-bit TIFF")
    y += caption_h + gap // 2
    card(QRect(pad, y, sheet_w, sheet_h), sheet)
    caption(pad, y + sheet_h, sheet_w, "Contact sheet", "the roll as PDF and PNG, with its metadata")
    painter.end()
    picture.save(str(HERE.parent / "hero.png"), "PNG", 9)
    print(f"  ../hero.png  {picture.width()}×{picture.height()}")


def answer(handler) -> None:
    """Hand the next modal dialog to `handler`, from inside its own event loop."""

    def poll() -> None:
        dialog = QApplication.activeModalWidget()
        if dialog is None or not dialog.isVisible():
            QTimer.singleShot(20, poll)
            return
        settle(0.2)
        handler(dialog)

    QTimer.singleShot(0, poll)


def collect() -> None:
    """Free the dialogs just closed, here on the GUI thread. Left to Python's collector they are freed
    on whichever thread runs it next, often the render thread, and PySide crashed the process deleting
    them from there (a segmentation fault in shiboken's mainThreadDeletionHandler)."""
    gc.collect()


def button(dialog: QWidget, text: str) -> QPushButton:
    return next(b for b in dialog.findChildren(QPushButton) if b.text() == text)


def drag(view, rect) -> QRect:
    """Draw a rectangle on the live view with the mouse, as a user would; returns it in window pixels."""
    picture = view._area()
    x, y, w, h = rect
    corner = lambda fx, fy: QPoint(int(picture.x() + fx * picture.width()), int(picture.y() + fy * picture.height()))  # noqa: E731
    QTest.mousePress(view, Qt.MouseButton.LeftButton, pos=corner(x, y))
    QTest.mouseMove(view, corner(x + w, y + h))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, pos=corner(x + w, y + h))
    top_left = view.mapTo(view.window(), corner(x, y))
    return QRect(top_left, view.mapTo(view.window(), corner(x + w, y + h)))


def strip_item(window, row: int) -> QRect:
    strip = window.strip
    item = strip.visualItemRect(strip.item(row))
    return QRect(strip.viewport().mapTo(window, item.topLeft()), item.size())


def click_frame(window, row: int) -> None:
    strip = window.strip
    QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=strip.visualItemRect(strip.item(row)).center())


def log_row(window, text: str) -> QRect:
    """The log panel's line that starts with `text`, in window pixels."""
    view = window.log_bar.panel.view
    block = view.document().begin()
    while block.isValid() and text not in block.text():
        block = block.next()
    line = view.document().documentLayout().blockBoundingRect(block).toAlignedRect()
    line.translate(0, -view.verticalScrollBar().value())
    line.setLeft(0)
    line.setWidth(view.viewport().width())
    return line.translated(area(view.viewport(), window).topLeft())


def type_into(edit, text: str) -> None:
    """A field filled in as if typed. Typed key by key offscreen, the keys went to the window's correction keys:
    the field never had the focus."""
    edit.setText(text)
    edit.textEdited.emit(text)


def inside(widget: QWidget, top: QWidget) -> QRect:
    """A crop of exactly a widget: `shot` adds a margin around a crop, which would take in its neighbours."""
    return area(widget, top).adjusted(MARGIN - 6, MARGIN - 6, 6 - MARGIN, 6 - MARGIN)


def with_heading(widget: QWidget, top: QWidget) -> QRect:
    """A widget and the panel's heading above it, which a crop to the widget would cut in half."""
    return area(widget, top).adjusted(0, -36, 0, 0)


def dark(on: bool) -> None:
    """The window's dark look, as when the system turns dark (offscreen it is unknown: light)."""
    style.dark = (lambda: True) if on else LIGHT
    style._restyle(app)
    settle(1.0)


def main() -> None:
    if OUT.exists():
        for old in OUT.glob("*.png"):
            old.unlink()
    OUT.mkdir(exist_ok=True)
    ROLLS.mkdir(parents=True)
    window = mainwindow.MainWindow(sim=True)
    window.menuBar().hide()  # on macOS the menu bar is at the top of the screen, not in the window
    window.resize(1400, 900)
    window.show()
    window.activateWindow()  # typing and shortcuts need it
    QTest.qWaitForWindowActive(window)
    rail, roll_page, scan, frame, presets = window.rail.buttons, window.roll_page, window.scan_page, window.frame_page, window.presets_page
    idle = lambda: not window._busy and window._pending == 0 and not window._flat_capturing()  # noqa: E731
    try:
        wait(lambda: window.view.image is not None and "iso" in window._camera)
        roll_page.folder_edit.setText("~/Scans")
        roll_page.roll_edit.setText("Roll01")
        settle(1.0)

        print("The Help menu")
        menu = window.help_menu
        menu.popup(QPoint(0, 0))
        settle()
        shot("help-menu", menu)
        menu.hide()

        print("Calibrating")
        panel = window.wizard_column.wizard_panel
        rail["presets"].click()
        settle()
        shot("calibrate-1-new-preset", window, [presets.new_preset_button], crop=[presets.new_preset_button, presets.preset_list])
        presets.new_preset_button.click()
        settle()
        panel.name_edit.setText("Portra 400")
        settle()
        shot("calibrate-2-film", window, [panel.name_edit, list(panel.light_buttons.values()), panel.kind_buttons["rgb"], panel.next_button], crop=[panel])
        panel.next_button.click()
        settle()
        panel.fields["aperture"].setText("8")
        settle()
        shot("calibrate-3-camera", window, [panel.camera_rows["aperture"], panel.fields["aperture"], panel.next_button], crop=[panel])
        panel.next_button.click()
        settle()
        base = drag(window.view, sim.BASE_ROI)
        settle()
        shot("calibrate-4-film-base", window, [window.live_page.base_tool, base, panel.rect_check, panel.next_button], width=2000)
        panel.next_button.click()
        wait(lambda: len(window._wizard.histogram()[0] or []) > 0 and len(panel.histogram.histograms) >= 2)
        shot("calibrate-5-running", window, [panel.stage_labels, panel.histogram, panel.stop_button], crop=[panel])
        wait(lambda: window._wizard.state == DONE and idle())
        settle()
        shot("calibrate-6-done", window, [At(panel.result, "tr"), panel.next_button], crop=[panel])
        panel.next_button.click()
        settle()
        settings = [widget for row in presets.setting_rows.values() for widget in row]
        film_base, change = [presets.base_picture, presets.base_levels], [presets.recalibrate_button, presets.delete_button]
        shot("calibrate-7-preset", window, [presets.preset_list, settings, film_base, change, presets.start_button],
             crop=[presets.preset_list, presets.new_preset_button, presets.start_button])  # fmt: skip

        print("Starting a session")
        rail["roll"].click()
        settle()
        form = roll_page.metadata_form
        shot("session-1-roll", window, [roll_page.roll_edit, [roll_page.folder_edit, roll_page.choose_button], [roll_page.preset_picker, roll_page.preset_label],
                                        roll_page.metadata_panel, [roll_page.start_note, roll_page.start_button]],
             crop=[inside(roll_page.scroll.viewport(), window)])  # fmt: skip

        # The roll's metadata is on the Roll page, filled in before the session starts; its flat field is the session's first step, on Scan.
        for key, value in {"camera": "Nikon FM2", "lens": "Nikkor 50mm f/1.4", "shot_from": "2026-10-03", "shot_to": "2026-10-05", "film": "Kodak Portra 400",
                           "box_speed": "400", "ei": "400", "developer": "Lab: Photo Studio", "developed": "2026-10-10", "author": "Your Name"}.items():  # fmt: skip
            form.edits[key].setText(value)
        form.format_box.setCurrentIndex(form.format_box.findData("135"))
        form.keywords.entry.setText("berlin")
        QTest.keyClick(form.keywords.entry, Qt.Key.Key_Return)
        form.notes.setPlainText("Walk along the Spree.")
        settle()
        shot("session-2-metadata", window, [form.edits["camera"], form.keywords], crop=[form])
        settle()
        shot("session-3-start", window, [roll_page.start_note, roll_page.start_button], crop=[with_heading(roll_page.start_note, window), roll_page.start_button])
        roll_page.start_button.click()
        collect()
        settle(1.0)
        shot("session-3-flat-field", window, [scan.flat_text, scan.flat_button, scan.without_button, scan.end_button], crop=[scan.flat_heading, scan.end_button])
        scan.flat_button.click()
        wait(lambda: not window._flat_pending() and idle())
        settle()
        shot("session-4-started", window, [scan.session_label, scan.end_button, scan.scan_button], crop=[scan.counter_heading, scan.end_button])

        print("Scanning")
        window.live_page.picture_tool.click()
        picture = drag(window.view, sim.PICTURE_RECT)
        settle()
        shot("scan-1-picture-area", window, [window.live_page.picture_tool, picture, window.live_page.live_orientation], width=2000)

        def first_number(dialog) -> None:
            shot("scan-2-first-number", dialog, [[button(dialog, text) for text in ("00", "0", "1")], button(dialog, "Skip: count from 1"), button(dialog, "Scan")])
            button(dialog, "00").click()  # the roll starts at 00, as many films do: the strip and the files show it
            button(dialog, "Scan").click()

        answer(first_number)
        scan.scan_button.click()
        collect()
        wait(lambda: idle() and -1 in window._thumbs and window.view.inset is not None)
        settle()
        inset = window.view._inset_area().toAlignedRect().translated(area(window.view, window).topLeft())
        shot("scan-3-first-frame", window, [scan.scan_button, scan.counter, inset, strip_item(window, 1), scan.retake_button, At(rail["scan"], "tl")],
             width=2000)  # fmt: skip
        for n in (0, 1, 2, 3, 4):  # the strip: 00, 0, 1, 2, 3, 4
            scan.scan_button.click()
            wait(lambda n=n: idle() and n in window._thumbs)
        wait(lambda: window.view.inset is not None)  # frame 4 live, its print in the corner
        settle(1.0)
        hero_live = window.grab().toImage()  # for the README

        print("The window")
        live_tools = [window.live_page.picture_tool, window.live_page.live_orientation]  # not the focus reading: the badge goes there
        shot("window", window, [window.rail, window.header.picker, live_tools, window.view, window.strip, window.side, window.log_bar], width=2000)
        dark(True)
        shot("window-dark", window, width=2000)
        dark(False)

        click_frame(window, 4)
        wait(lambda: frame.frame_info.text().startswith("Frame 2"))
        settle(1.0)
        shot("scan-4-rescan", window, [strip_item(window, 4), frame.rescan_button], width=2000)
        frame.rescan_button.click()
        settle()
        shot("scan-5-replace", window, [scan.scan_button, scan.retake_button], crop=[scan.scan_button, scan.retake_button])
        scan.retake_button.click()  # Cancel rescan
        settle()

        print("Developing")
        click_frame(window, 3)
        wait(lambda: frame.frame_info.text().startswith("Frame 1") and window.develop_column.isVisible())
        settle(1.0)
        bar = window.develop_column
        keys = [button for pair in bar.buttons.values() for button in pair]
        hold = At([bar.hold_box, bar.match_box], "tr")
        shot("develop-1-frame", window, [window.strip, frame.frame_info, list(bar.base_buttons.values()), keys, hold, At(rail["develop"], "tl")],
             width=2000)  # fmt: skip
        plain, shift = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier
        for key, modifier in ((Qt.Key.Key_Q, plain), (Qt.Key.Key_Q, plain), (Qt.Key.Key_D, plain), (Qt.Key.Key_G, shift), (Qt.Key.Key_Down, plain)):
            QTest.keyClick(window.strip, key, modifier)  # D +2, M -1, contrast softer, then Conv
        wait(lambda: frame.frame_info.text().startswith("Frame 1 · Converted"))
        settle(1.5)
        control = lambda key: [bar.buttons[key][0], bar.values[key], bar.buttons[key][1]]  # noqa: E731
        shot("develop-2-corrected", window, [list(bar.base_buttons.values()), control("d"), control("m"), control("grad")],
             crop=[*bar.base_buttons.values(), bar.all_button])  # fmt: skip

        print("Metadata, and a warning in the log")
        frame.frame_row.header.click()
        settle()
        row = frame.frame_row.edits
        type_into(row["note"], "Oberbaum bridge at dusk")
        settle(2.5)  # saved: "Metadata written into 1 file." would follow the warning below and end its red at once
        type_into(row["aperture"], "1/60")  # the shutter, in the aperture's field: not taken, and the log says so
        wait(lambda: window.log_bar.unseen > 0, 10)
        settle(0.5)
        shot("window-warning", window, [[window.log_bar.time, window.log_bar.status], window.log_bar.log_button], crop=[window.log_bar])
        type_into(row["aperture"], "f/2.8")
        type_into(row["shutter"], "1/60")
        window.strip.setFocus()
        settle(2.5)  # saved, and written into the frame's files
        wait(idle)
        shot("metadata-frame", window, [frame.frame_row.header, row["note"]], crop=[frame.frame_row])
        frame.frame_row.set_open(False)
        window.log_bar.log_button.click()
        settle(1.0)
        log = window.log_bar.panel
        shot("window-log", window, [[log.all_button, log.warnings_button], log_row(window, "Frame 1: "), log.copy_button, log.close_button, window.log_bar.log_button],
             width=2000)  # fmt: skip
        window.log_bar.log_button.click()
        settle()

        print("Finishing the roll")
        finish = window.finish_page
        rail["finish"].click()
        wait(lambda: finish.sheet_count.text().endswith("page"))
        settle(1.0)
        shot("finish-1-page", window, [[finish.prints_count, finish.prints_meter, finish.prints_line, finish.print_button], finish.contact_button,
                                       list(finish.sheet_options.values()), finish.edit_button, [finish.roll_button, finish.clean_button], finish.sheet_pages],
             width=2000)  # fmt: skip
        finish.print_button.click()
        wait(lambda: finish.print_button.text() == "Prints up to date" and idle())
        settle()
        shot("finish-2-printed", window, [finish.prints_count, At(finish.print_button, "tl")],
             crop=[inside(finish.prints_count.parentWidget(), window)])  # fmt: skip
        rail["develop"].click()  # for the README: a frame being developed, its picture large
        click_frame(window, 1)
        wait(lambda: frame.frame_info.text().startswith("Frame 00"))
        QTest.keyClick(window.strip, Qt.Key.Key_Down)
        wait(lambda: frame.frame_info.text().startswith("Frame 00 · Converted") and idle())
        settle(1.5)
        hero_develop = window.grab().toImage()
        window.rail.set_current("develop")

        print("Contact sheet")
        rail["finish"].click()
        finish.contact_button.click()
        wait(lambda: opened and idle())
        page = QImage(str(opened[-1].with_suffix(".png")))
        page = page.copy(0, 0, page.width(), page.height() // 4)  # the header and the first strips: a short roll leaves the rest of the A4 blank
        page.scaledToWidth(1400, Qt.TransformationMode.SmoothTransformation).save(str(OUT / "contact-2-sheet.png"), "PNG", 9)
        print("  contact-2-sheet.png")
        hero(hero_live, hero_develop, page)

        print("The rail, Lightroom Classic and the devices")
        rail["develop"].click()
        settle(1.0)
        shot("window-rail", window, [At(rail[name], "tl") for name in ("scan", "develop", "presets", "lightroom", "devices")], crop=[window.rail],
             cut=(rail["finish"], rail["presets"]))  # fmt: skip
        lr = window.lightroom_panel
        lr.lightroom_button.click()
        exchange = TMP / "home" / ".tetherinvert" / "lightroom"
        token = (exchange / "request.txt").read_text().split("\n")[0]
        roll = ROLLS / "Roll01"
        (exchange / "status.txt").write_text(f"watching\n6\n0.4.0\n{token}\n{roll / 'scans'}\n{roll / 'prints'}\n")
        wait(lambda: lr.status.text() == "Watching", 10)
        window._open_popover("lightroom")
        settle()
        popover = window._popovers["lightroom"]
        boxes = [lr.lightroom_scans, lr.lightroom_prints]
        shot("lightroom", window, [lr.status, boxes, At(lr.lightroom_button, "tl"), lr.lightroom_note, At(lr.stop_button, "tl"), At(rail["lightroom"], "tl")],
             crop=[popover, rail["presets"], rail["devices"]], over=[popover])  # fmt: skip
        popover.hide()
        window._open_popover("devices")
        settle()
        devices, popover = window.devices_panel, window._popovers["devices"]
        shot("devices", window, [devices.status, [devices.camera_label, devices.light_label, devices.temperature_label], devices.note, At(rail["devices"], "tl")],
             crop=[popover, rail["presets"], rail["devices"]], over=[popover])  # fmt: skip
        popover.hide()

        print("Continuing a roll")
        rail["scan"].click()
        scan.end_button.click()
        wait(lambda: window._session is None and idle())
        rail["roll"].click()
        roll_page.roll_edit.setText("")
        settle()
        roll_page.continue_buttons["Roll01"].click()
        settle(1.0)
        roll_page.scroll.ensureWidgetVisible(roll_page.rolls_list)
        settle()
        shot("session-5-continue", window, [roll_page.start_note, roll_page.start_button, roll_page.rolls_list],
             crop=[with_heading(roll_page.start_note, window), roll_page.start_button, roll_page.rolls_list])  # fmt: skip
        picker = window.header.picker.menu()
        picker.popup(area(window.header.picker, window).bottomLeft())
        settle()
        shot("session-6-picker", picker)
        picker.hide()
    finally:
        window.close()
        app.processEvents()

    print("Without a Scanlight")
    os.environ["TETHERINVERT_NO_LIGHT"] = "1"
    window = mainwindow.MainWindow(sim=True)
    window.menuBar().hide()
    window.resize(1400, 900)
    window.show()
    roll_page, scan, presets = window.roll_page, window.scan_page, window.presets_page
    panel = window.wizard_column.wizard_panel
    try:
        wait(lambda: window.view.image is not None and "iso" in window._camera)
        settle(1.0)
        window.rail.buttons["presets"].click()
        presets.new_preset_button.click()
        settle()
        panel.name_edit.setText("Portra 400 own light")
        panel.kind_buttons["own-rgb"].click()
        settle()
        shot("own-1-film", window, [list(panel.light_buttons.values()), [panel.kind_buttons[k] for k in ("own-rgb", "own-colour", "own-bw")]], crop=[panel])
        panel.next_button.click()
        settle()
        panel.fields["aperture"].setText("8")
        panel.fields["light"].setText("RGB LED panel, each colour at 100 %")
        settle()
        shot("own-2-camera", window, [panel.fields["light"]], crop=[panel])
        panel.next_button.click()
        settle()
        drag(window.view, sim.BASE_ROI)
        panel.next_button.click()
        wait(lambda: window._wizard.step.key == "R" and idle())
        settle()
        shot("own-3-colour", window, [panel.position, panel.colour_prompt, panel.next_button], crop=[panel])
        for letter in ("R", "G", "B"):  # the simulator sets its light to each colour as asked, as the operator would
            wait(lambda letter=letter: window._wizard.step.key == letter and idle())
            panel.next_button.click()
        wait(lambda: window._wizard.state == DONE and idle())
        settle()
        shot("own-4-done", window, [At(panel.result, "tr")], crop=[panel])
        panel.next_button.click()
        settle()

        roll_page.roll_edit.setText("Roll02")
        settle()

        roll_page.metadata_form.fill({})  # the roll's metadata: left empty
        window.rail.buttons["roll"].click()
        settle()
        shot("own-5-session", window, [roll_page.start_note, roll_page.start_button],
             crop=[with_heading(roll_page.start_note, window), roll_page.start_button])  # fmt: skip
        roll_page.start_button.click()
        scan.flat_button.click()
        for colour in ("red", "green", "blue"):  # the flat field: a prompted shot per colour
            wait(lambda colour=colour: scan.flat_button.text() == f"Shoot: {colour} only" and scan.flat_button.isEnabled())
            scan.flat_button.click()
        wait(lambda: not window._flat_pending() and idle())
        window.live_page.picture_tool.click()
        drag(window.view, sim.PICTURE_RECT)
        settle()
        answer(lambda dialog: button(dialog, "Scan").click())  # the first frame: 00, as checked
        scan.scan_button.click()
        collect()
        wait(lambda: scan.scan_button.text() == "Shoot: red only" and idle())
        settle(1.0)
        shot("own-6-shoot", window, [scan.scan_button, scan.retake_button, window.log_bar.status],
             crop=[scan.session_label, window.log_bar.time, window.log_bar.status])  # fmt: skip
        scan.retake_button.click()  # Cancel frame
        settle()
    finally:
        window.close()
        app.processEvents()
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
