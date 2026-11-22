"""The roll in the window: a strip of its frames, and a view that shows one of them large.

How the window moves between the camera and the photos follows the rhythm at the stand:
scan, glance at the result, advance the film, frame the next one, scan.

- The live view is home and the first entry of the strip. Looking at a photo is a short
  excursion from it.
- Scanning always shows the live view again: what is shot has to be seen.
- After a scan the new frame appears as a small inset on the live view while the film is
  advanced. A click on it opens the frame.
- Holding Tab on the live view shows the last frame for as long as the key is held.
- Tab, Esc or Home on a frame goes back to the live view; Left and Right step through the strip.

The pictures are developed from the previews `render()` leaves (development.py), on a thread
of their own, so a roll of 36 frames never stalls the window.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QEvent, QLocale, QObject, QPointF, QRectF, QSize, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QToolButton, QWidget,
)

from .. import development, invert, orient, paths
from ..session import Roll
from . import style
from .style import SURROUND

THUMB = QSize(96, 64)  # px in the strip: a 3:2 frame
SHEET_DPI = 96  # the contact sheet on Finish: an A4 page 794 px wide, twice the page's width there for a Retina screen
THUMB_EDGE = 240  # px, the long edge of a thumbnail made: twice the strip's for a Retina screen, and the live view's inset is made from it too
STRIP_PAD = 4  # px around a frame's picture and caption in the strip
# The correction keys, as on the SP-3000's keyboard one key per direction. Only while a frame is
# shown large and no text field is being typed in. Shift turns contrast, saturation and tone
# the other way.
KEYS = {
    Qt.Key.Key_Q: ("d", 1), Qt.Key.Key_A: ("d", -1),
    Qt.Key.Key_W: ("c", 1), Qt.Key.Key_S: ("c", -1),
    Qt.Key.Key_E: ("m", 1), Qt.Key.Key_D: ("m", -1),
    Qt.Key.Key_R: ("y", 1), Qt.Key.Key_F: ("y", -1),
    Qt.Key.Key_G: ("grad", 1), Qt.Key.Key_T: ("tone", 1),
}  # fmt: skip
# Saturation is the key right of T, by its place, whatever it types: Y on a US keyboard, Z on a German
# one. Qt names keys by the character typed, so the place
# comes from the system: macOS's virtual key code (kVK_ANSI_Y, the same place on every layout), on Linux
# the X11 key code (evdev KEY_Y 21 + 8; not yet tried there). Where neither is reported, the letter Y.
SATURATION_PLACE = {"darwin": ("virtual", 0x10), "linux": ("scan", 29)}


def saturation_key(event) -> bool:
    """Whether a key event is the key right of T."""
    field, code = SATURATION_PLACE.get(sys.platform, ("", 0))
    native = event.nativeVirtualKey() if field == "virtual" else event.nativeScanCode() if field == "scan" else 0
    return native == code if native else event.key() == Qt.Key.Key_Y


# Where the keyboard is QWERTZ the key right of T types Z: German, Swiss, Austrian and the central
# European layouts. Named by the keyboard's language (on macOS the input source's), a guess where
# that is not reported: the key itself is found by its place (saturation_key).
QWERTZ_LANGUAGES = {QLocale.Language.German, QLocale.Language.Czech, QLocale.Language.Slovak, QLocale.Language.Hungarian, QLocale.Language.Slovenian,
                    QLocale.Language.Croatian, QLocale.Language.Bosnian}  # fmt: skip


def saturation_letter() -> str:
    """The letter the key right of T types on this keyboard, to name it."""
    locale = QGuiApplication.inputMethod().locale()
    qwertz = locale.language() in QWERTZ_LANGUAGES or locale.territory() in (QLocale.Country.Switzerland, QLocale.Country.Liechtenstein)
    return "Z" if qwertz else "Y"


# Beside each correction's keys a gauge shows which way it moves the picture, − end left and + end
# right like its buttons: the SP-3000 names only the + end, and −C (more red) or −Y (more blue) has
# to be remembered. Muted, about half saturation, through neutral grey at 0: saturated colours right
# beside the photo would change how its colours look while they are being judged. The colour
# keys' ends follow invert.edit (+C takes red away); a test checks them against it.
GAUGE_GREY = "#8b8b88"  # no correction
GAUGES = {
    "d": ("#dcdad4", "#2b2a27"),  # lighter .. darker: a plain ramp, no grey stop
    "c": ("#b65a52", "#4f9ea6"),  # red .. cyan
    "m": ("#5f9a52", "#a35a9c"),  # green .. magenta
    "y": ("#5468ad", "#c2a94a"),  # blue .. yellow
    "grad": "wedge",  # four grey steps, flat on the left, black to white on the right
    "sat": "hues",  # bands of colour, grey on the left
}
COLOUR_ONLY = ("c", "m", "y", "sat")  # nothing to do on one channel (invert.edit): off for black-and-white frames
GAUGE_HEIGHT = 8  # px, the track; the marker stands 2 px over it above and below


def gauge_position(key: str, value: int) -> float:
    """Where a value sits on its gauge, 0 (left end) to 1 (right end), on a square-root scale:
    usual corrections are small (±10 of ±80), and on a straight scale the marker would hardly move."""
    limit = invert.KEYS[key][1 if value > 0 else 0]
    return 0.5 + 0.5 * float(np.sign(value)) * (abs(value) / abs(limit)) ** 0.5 if value else 0.5


def gauge_track(key: str, width: int, height: int, colour: bool = True) -> np.ndarray:
    """The gauge's track as RGB uint8 pixels; in grey when the key does nothing on the frame shown."""
    t = np.linspace(0.0, 1.0, max(width, 1), dtype=np.float32)[None, :, None]  # left to right
    rows = np.linspace(0.0, 1.0, max(height, 1), dtype=np.float32)[:, None, None]  # top to bottom
    grey = np.array(QColor(GAUGE_GREY).getRgbF()[:3], np.float32)
    style = GAUGES[key]
    if style == "wedge":  # four steps around the grey, spread from nearly flat to black and white
        step = np.minimum((rows * 4).astype(np.int32), 3).astype(np.float32)  # 0..3 from the top
        spread = 0.06 + 0.94 * t
        pixels = np.broadcast_to(0.5 + spread * (1.5 - step) / 3, (height, width, 1)) * np.ones(3, np.float32)
    elif style == "hues":  # bands of hues whose saturation grows from none to about 55 %
        hues = np.array([QColor(c).getRgbF()[:3] for c in ("#c96f5e", "#c9b45e", "#6fae6a", "#6782c4")], np.float32)
        band = hues[np.minimum((rows[..., 0] * len(hues)).astype(np.int32), len(hues) - 1)]  # height x 1 x 3
        pixels = grey + t * (band - grey)
    else:
        left, right = (np.array(QColor(c).getRgbF()[:3], np.float32) for c in style)
        if key == "d":
            pixels = left + t * (right - left)
        else:  # through grey at the middle
            pixels = np.where(t < 0.5, left + (t * 2) * (grey - left), grey + (t * 2 - 1) * (right - grey))
    pixels = np.broadcast_to(pixels, (height, width, 3))
    if not colour:
        luma = pixels @ np.array([0.299, 0.587, 0.114], np.float32)
        pixels = np.repeat((grey.mean() + 0.35 * (luma - grey.mean()))[..., None], 3, axis=-1)  # grey and faint
    return np.ascontiguousarray(np.clip(pixels * 255 + 0.5, 0, 255).astype(np.uint8))


def summary(adjust: Optional[dict]) -> str:
    """A frame's corrections in a few characters, for the strip: 'D+1 M-2 Ct+1'."""
    a = invert.adjustment(adjust)
    parts = [f"{key.upper()}{a[key]:+d}" for key in ("d", "c", "m", "y") if a[key]]
    parts += [f"{name}{a[key]:+d}" for key, name in (("grad", "Ct"), ("sat", "S")) if a[key]]
    tone = "".join(word[0] for word in a["tone"].split()).upper()  # "shadow soft" -> "SS"
    return " ".join(parts + ([tone] if a["tone"] != "standard" else []))
LIVE = -100  # the strip's first entry: below every frame number, since frames are numbered as on the film from 00 (-1) and 0
BASE_COLOUR, PICTURE_COLOUR = QColor("#f5a623"), QColor("#35c2d6")  # the two rectangles; the picture area is outlined on a frame too
INSET_WIDTH = 220  # px: the last scan in the live view's corner
AROUND_SIZE = (360, 240)  # px: the picture of a preset's film base (LiveView.picture_around), shown at 240 × 160, sharp on Retina
# ...that many times the rectangle's narrow side across, between these shares of the frame's width: a close look at the rebate
# metered. Sized by the whole rectangle (a sliver 40 % of the frame high), the picture would be the whole frame and the rectangle a line.
AROUND_TIMES, AROUND_LEAST, AROUND_MOST = 8, 0.12, 0.35


def around(rect: tuple[float, float, float, float], width: int, height: int) -> tuple[float, float, float, float]:
    """The part of a `width` × `height` frame `LiveView.picture_around` shows, left, top, across, down in
    pixels: 3 : 2, centred on the rectangle as far as the frame allows. A long rectangle runs off it."""
    x, y, w, h = rect[0] * width, rect[1] * height, rect[2] * width, rect[3] * height
    across = min(max(AROUND_TIMES * min(w, h * 1.5), AROUND_LEAST * width), AROUND_MOST * width, width, height * 1.5)
    down = across / 1.5
    left = min(max(x + w / 2 - across / 2, 0.0), width - across)
    top = min(max(y + h / 2 - down / 2, 0.0), height - down)
    return left, top, across, down


def to_qimage(pixels: np.ndarray) -> QImage:
    height, width = pixels.shape[:2]
    return QImage(pixels.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()  # copy: own the memory


@dataclass(frozen=True)
class Request:
    """A picture the window asks for: of a frame by its development (development.py, with its
    corrections as they are being made, or none while C is held), and `seq`, which counts up with
    every picture the window asks for."""

    development: development.Development
    seq: int = 0


@dataclass(frozen=True)
class SheetRequest:
    """The contact sheet's pages of the roll in `folder`, as they would be written now (Finish)."""

    folder: str
    seq: int


class Previewer(QObject):
    """Makes screen images off the window's thread: every frame as its print will be, and the pages
    of the contact sheet.

    While a key is held the window asks faster than pictures can be made; the requests queue up
    here and all but the newest are skipped (`newest` is set by the window before it asks).
    """

    thumbnail = Signal(object, object)  # the Request, QImage
    picture = Signal(object, object, object)  # the Request, QImage, the picture area to outline on it (None where it is cropped to it)
    full = Signal(object, object)  # the Request, the print at full size
    sheet = Signal(object, object)  # the SheetRequest, its pages (QImages; none when it could not be made)

    def __init__(self) -> None:
        super().__init__()
        self.newest = 0
        self.newest_full = 0  # the same for the pictures at 100 %: one takes about 2 s, so only the newest is made
        self.newest_sheet = 0  # and for the contact sheet: 1.5 s for 36 frames

    @Slot(object)
    def make_thumbnail(self, request: Request) -> None:
        made = _picture(request.development, THUMB_EDGE)
        if made is not None:
            self.thumbnail.emit(request, to_qimage(made.rgb))

    @Slot(object)
    def make_picture(self, request: Request) -> None:
        if request.seq < self.newest:
            return  # a newer picture is already asked for
        made = _picture(request.development, development.SCREEN)
        if made is not None:
            self.picture.emit(request, to_qimage(made.rgb), made.outline)

    @Slot(object)
    def make_full(self, request: Request) -> None:
        """The print at full size, as it will be written: from the flat TIFF."""
        if request.seq < self.newest_full:
            return  # corrected again since: a newer one is asked for
        made = _picture(request.development, None)
        if made is not None:
            self.full.emit(request, to_qimage(made.rgb))

    @Slot(object)
    def make_sheet(self, request: SheetRequest) -> None:
        """Every page, painted as the PDF's (contact.py) at `SHEET_DPI`."""
        from .. import contact  # here: contact.py takes to_qimage from this module

        if request.seq < self.newest_sheet:
            return  # the roll changed again since
        try:
            sheet = contact.contents(Roll.open(Path(request.folder)))
            pages = [contact.page_image(sheet, page, SHEET_DPI) for page in range(len(contact.sheet_layout(sheet).pages))]
        except Exception:  # a roll moved or a file being written right now: asked for again when roll.json changes
            pages = []
        self.sheet.emit(request, pages)


def _picture(dev: development.Development, edge: Optional[int]) -> Optional[development.Picture]:
    try:
        return development.picture(dev, edge)
    except Exception:  # not rendered yet, or a file being written right now: it is asked for again when it is done
        return None


class StripCaption(QStyledItemDelegate):
    """A frame in the strip: its picture, its number with one tag beside it, and its base and
    corrections under them. The strip lies on the picture's dark surround in both appearances, so
    it is written in the dark appearance's colours."""

    def paint(self, painter: QPainter, option, index) -> None:
        t = style.DARK
        cell = QRectF(option.rect).adjusted(2, 2, -2, -2)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(QPen(QColor(t.accent_text), 2))
            painter.setBrush(QColor(t.panel))
            painter.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 6, 6)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        top = cell.top() + STRIP_PAD
        if isinstance(icon, QIcon):
            pixmap = icon.pixmap(THUMB)
            size = pixmap.deviceIndependentSize().scaled(THUMB.width(), THUMB.height(), Qt.AspectRatioMode.KeepAspectRatio)
            place = QRectF(cell.center().x() - size.width() / 2, top + (THUMB.height() - size.height()) / 2, size.width(), size.height())
            painter.drawPixmap(place, pixmap, QRectF(pixmap.rect()))
        first, _, details = (index.data(Qt.ItemDataRole.DisplayRole) or "").partition("\n")
        number, _, tag = first.partition(" · ")
        line = QFontMetrics(option.font).height()
        text = QRectF(cell.left() + STRIP_PAD, top + THUMB.height() + 3, cell.width() - 2 * STRIP_PAD, line)
        mono = QFont(option.font)
        mono.setFamily(style.mono_family())
        painter.setFont(mono)
        painter.setPen(QColor(t.text))
        painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, number)
        small = QFont(option.font)
        small.setPointSizeF(option.font.pointSizeF() * 0.85)
        small.setBold(True)
        painter.setFont(small)
        painter.setPen(QColor(t.accent_text))
        painter.drawText(text, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, tag)
        small.setBold(False)
        painter.setFont(small)
        painter.setPen(QColor(t.muted))
        below = text.translated(0, line)
        details = QFontMetrics(small).elidedText(details, Qt.TextElideMode.ElideRight, int(below.width()))
        painter.drawText(below, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, details)
        painter.restore()

    def sizeHint(self, option, _index) -> QSize:
        return QSize(THUMB.width() + 2 * STRIP_PAD + 4, THUMB.height() + 2 * QFontMetrics(option.font).height() + 2 * STRIP_PAD + 7)


class Strip(QListWidget):
    """Live first, then the roll's frames, left to right under the picture in one row at a height of its
    own: as wide as the picture, it never changes the window's size. Each caption says what is known
    about the frame: its number, one tag (rendering, to print, RAW deleted), its base and corrections."""

    def __init__(self) -> None:
        super().__init__()
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setIconSize(THUMB)
        self.setItemDelegate(StripCaption(self))
        style.set_role(self, "strip")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        cell = self.itemDelegate().sizeHint(self._options(), None)
        self.setGridSize(QSize(cell.width() + 6, cell.height()))
        # Room for the scroll bar from the start: shown only once the roll is longer than the picture is
        # wide, it would otherwise cover the captions, or change the strip's height.
        self.setFixedHeight(cell.height() + 2 * self.frameWidth() + self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent) + 4)
        blank = QPixmap(THUMB)
        blank.fill(QColor(style.DARK.badge))  # a frame whose picture has not come yet: a step lighter than the strip
        self._blank = QIcon(blank)
        self._add(LIVE, "Live")

    def _options(self) -> QStyleOptionViewItem:
        option = QStyleOptionViewItem()
        option.font = self.font()
        return option

    def _add(self, number: int, caption: str) -> QListWidgetItem:
        item = QListWidgetItem(self._blank, caption)
        item.setData(Qt.ItemDataRole.UserRole, number)
        self.addItem(item)
        return item

    def numbers(self) -> list[int]:
        return [self.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self.count())]

    def shown(self) -> int:
        item = self.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else LIVE

    def set_frames(self, captions: dict[int, str]) -> None:
        """The roll's frames, by number. Icons of frames still there are kept."""
        icons = {self.item(row).data(Qt.ItemDataRole.UserRole): self.item(row).icon() for row in range(1, self.count())}
        selected = self.shown()
        self.blockSignals(True)
        while self.count() > 1:
            self.takeItem(1)
        for number, caption in sorted(captions.items()):
            self._add(number, caption).setIcon(icons.get(number, self._blank))
        self.select(selected if selected in captions else LIVE)
        self.blockSignals(False)

    def set_icon(self, number: int, image: Optional[QImage]) -> None:
        if number in self.numbers():
            icon = QIcon(QPixmap.fromImage(image)) if image is not None else self._blank
            self.item(self.numbers().index(number)).setIcon(icon)

    def select(self, number: int) -> None:
        if number in self.numbers():
            self.setCurrentRow(self.numbers().index(number))


class LiveView(QWidget):
    """Shows the camera's live view and lets the two rectangles be drawn with the mouse. After a
    scan, the new frame sits in a corner (the inset) while the film is advanced."""

    changed = Signal()
    inset_clicked = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(640, 430)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # a click into the picture takes the keys away from a text field
        self.image: Optional[QImage] = None
        self.rects: dict[str, Optional[tuple[float, float, float, float]]] = {"base": None, "picture": None}
        self.tool = "base"
        self.hidden: set[str] = set()  # rectangles not shown and not drawable right now
        self._drag: Optional[QPointF] = None
        self._best_focus = 0.0
        self.focus = 0.0
        self.inset: Optional[tuple[int, QImage]] = None  # frame number and its picture
        # The picture is shown turned as the frames will be stored; the rectangles stay in the
        # camera's frame (orient.py), so they are turned for drawing and back when drawn.
        self.orientation: dict = dict(orient.UPRIGHT)
        self._camera: Optional[QImage] = None  # as the camera sends it
        self.waiting = "Waiting for the camera…"  # shown while there is no picture

    def set_orientation(self, orientation: Optional[dict]) -> None:
        self.orientation = orient.normal(orientation)
        self._turn()
        self.update()

    def _turn(self) -> None:
        if self._camera is None:
            return
        image, o = self._camera, self.orientation
        if o["turn"]:
            image = image.transformed(QTransform().rotate(o["turn"]))  # clockwise, as orient.image
        if o["mirror"]:
            image = image.transformed(QTransform().scale(-1, 1))
        self.image = image

    def set_inset(self, inset: Optional[tuple[int, QImage]]) -> None:
        self.inset = inset
        self.update()

    def _inset_area(self) -> Optional[QRectF]:
        if self.inset is None:
            return None
        image = self.inset[1]
        width = INSET_WIDTH
        height = width * image.height() / max(image.width(), 1)
        return QRectF(self.width() - width - 12, self.height() - height - 12, width, height)

    def no_camera(self) -> None:
        """The camera is gone: its last picture must not stay up looking live."""
        self._camera = self.image = None
        self.focus = self._best_focus = 0.0
        self.waiting = "The camera is not connected."
        self.update()

    def picture_around(self, key: str) -> Optional[QImage]:
        """The camera's picture around a rectangle, the rectangle drawn on it, turned as the live view
        shows it: what the film base was metered on, kept with its preset (presets.py). The rectangles
        drawn are slivers of rebate (1 % of the frame across, often a third of it high), so the picture is a
        close look around their middle (`around`)."""
        rect, image = self.rects.get(key), self._camera
        if rect is None or image is None:
            return None
        width, height = image.width(), image.height()
        x, y, w, h = rect[0] * width, rect[1] * height, rect[2] * width, rect[3] * height
        left, top, across, down = around(rect, width, height)
        picture = image.copy(QRectF(left, top, across, down).toAlignedRect()).scaled(
            AROUND_SIZE[0], AROUND_SIZE[1], Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        ).convertToFormat(QImage.Format.Format_RGB32)
        scale = AROUND_SIZE[0] / across
        painter = QPainter(picture)
        painter.setPen(QPen(BASE_COLOUR, 2))
        painter.drawRect(QRectF((x - left) * scale, (y - top) * scale, max(w * scale, 2.0), max(h * scale, 2.0)))
        painter.end()
        if self.orientation["turn"]:
            picture = picture.transformed(QTransform().rotate(self.orientation["turn"]))
        if self.orientation["mirror"]:
            picture = picture.transformed(QTransform().scale(-1, 1))
        return picture

    def show_frame(self, data: bytes) -> None:
        image = QImage.fromData(data)
        if image.isNull():
            return
        self._camera = image
        self._turn()
        self._measure_focus(image)
        self.update()

    def _measure_focus(self, image: QImage) -> None:
        """Sharpness of the centre of the frame, as a share of the best value seen."""
        gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
        width, height = gray.width(), gray.height()
        pixels = np.frombuffer(gray.constBits(), np.uint8).reshape(height, gray.bytesPerLine())[:, :width]
        centre = pixels[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4].astype(np.float32)
        if centre.mean() < 4:
            self.focus = 0.0
            return
        laplacian = 4 * centre[1:-1, 1:-1] - centre[:-2, 1:-1] - centre[2:, 1:-1] - centre[1:-1, :-2] - centre[1:-1, 2:]
        sharpness = float(np.mean(laplacian**2)) / float(centre.mean() ** 2)
        self._best_focus = max(self._best_focus, sharpness)
        self.focus = sharpness / self._best_focus if self._best_focus else 0.0

    def reset_focus(self) -> None:
        self._best_focus = 0.0

    def _area(self) -> QRectF:
        """Where the image sits inside the widget, keeping its proportions."""
        if self.image is None:
            return QRectF(self.rect())
        scale = min(self.width() / self.image.width(), self.height() / self.image.height())
        width, height = self.image.width() * scale, self.image.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(SURROUND))  # neutral: it tints nothing judged on it
        area = self._area()
        if self.image is None:
            painter.setPen(QColor("#9a9a9a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.waiting)
            return
        painter.drawImage(area, self.image)
        for key, colour, label in (("picture", PICTURE_COLOUR, "Picture area"), ("base", BASE_COLOUR, "Film base")):
            rect = self.rects[key]
            if rect is None or key in self.hidden:
                continue
            x, y, w, h = orient.rect(rect, self.orientation)
            box = QRectF(area.x() + x * area.width(), area.y() + y * area.height(), w * area.width(), h * area.height())
            painter.setPen(QPen(colour, 2))
            painter.drawRect(box)
            painter.drawText(box.topLeft() + QPointF(4, -5), label)
        inset = self._inset_area()
        if inset is not None:
            painter.drawImage(inset, self.inset[1])
            painter.setPen(QPen(QColor("#f0f0f0"), 1))
            painter.drawRect(inset)
            painter.drawText(inset.topLeft() + QPointF(0, -5), f"Frame {paths.label(self.inset[0])} · click, or hold Tab")

    def _fraction(self, position: QPointF) -> QPointF:
        area = self._area()
        x = min(max((position.x() - area.x()) / area.width(), 0.0), 1.0)
        y = min(max((position.y() - area.y()) / area.height(), 0.0), 1.0)
        return QPointF(x, y)

    def mousePressEvent(self, event) -> None:
        inset = self._inset_area()
        if inset is not None and inset.contains(event.position()):
            self.inset_clicked.emit(self.inset[0])
            return
        if self.image is not None and self.tool not in self.hidden:
            self._drag = self._fraction(event.position())

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        end = self._fraction(event.position())
        x, y = min(self._drag.x(), end.x()), min(self._drag.y(), end.y())
        w, h = abs(end.x() - self._drag.x()), abs(end.y() - self._drag.y())
        if w > 0.005 and h > 0.005:
            self.rects[self.tool] = orient.rect_back((x, y, w, h), self.orientation)  # kept in the camera's frame
            self.update()

    def mouseReleaseEvent(self, _event) -> None:
        if self._drag is not None:
            self._drag = None
            self.changed.emit()


class FrameView(QWidget):
    """One frame, fitted to the window; a double click shows it at 100 %, dragging moves it."""

    wants_full = Signal()

    def __init__(self) -> None:
        super().__init__()
        # Lower than the live view: with its tools above and below, the frame metadata open too, a
        # frame then needs no more height than the live view, and the window's minimum stays put.
        self.setMinimumSize(640, 200)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # a click into the picture takes the keys away from a text field
        self.image: Optional[QImage] = None  # the preview, or a thumbnail until it has arrived
        self.full: Optional[QImage] = None  # the TIFF itself, while zoomed in
        self._full_width = 0  # px of the last TIFF shown zoomed: a newer preview stands in for it at its size
        self.outline: Optional[tuple[float, float, float, float]] = None  # the picture area on the preview
        self.zoomed = False
        self._centre = QPointF(0.5, 0.5)  # which point of the picture sits in the middle when zoomed
        self._drag: Optional[tuple[QPointF, QPointF]] = None

    def show_image(self, image: Optional[QImage], outline=None, same_frame: bool = False) -> None:
        self.image, self.outline = image, outline
        if not same_frame:
            self.full, self.zoomed, self._centre, self._full_width = None, False, QPointF(0.5, 0.5), 0
        elif self.full is not None:
            # A newer picture of this frame (a correction, a base, a render) while zoomed in: the TIFF
            # shown is out of date. Until the window has the new one (about 2 s for 24 MP), the
            # preview stands in at the same size: the change is seen at once, only softer. Kept,
            # the old TIFF would hide every correction until the frame was zoomed out.
            self.full = None
        self.update()

    def show_full(self, image: QImage) -> None:
        if self.zoomed:
            self.full, self._full_width = image, image.width()
            self.update()

    def _shown(self) -> Optional[QImage]:
        return self.full if self.zoomed and self.full is not None else self.image

    def _area(self, image: QImage) -> QRectF:
        if self.zoomed:  # one picture pixel per screen pixel
            if image is self.full or self._full_width:  # the TIFF, or the preview standing in for it at its size
                scale = (1.0 if image is self.full else self._full_width / image.width()) / self.devicePixelRatioF()
            else:
                scale = max(1.0, self._fit(image))
            width, height = image.width() * scale, image.height() * scale
            return QRectF(self.width() / 2 - self._centre.x() * width, self.height() / 2 - self._centre.y() * height, width, height)
        scale = self._fit(image)
        width, height = image.width() * scale, image.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _fit(self, image: QImage) -> float:
        return min(self.width() / image.width(), self.height() / image.height())

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(SURROUND))  # neutral: it tints nothing judged on it
        image = self._shown()
        if image is None:
            painter.setPen(QColor("#9a9a9a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No picture of this frame yet.")
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        area = self._area(image)
        painter.drawImage(area, image)
        if self.outline is not None and image is not self.full:  # a cropped TIFF has no picture area left to mark
            x, y, w, h = self.outline
            painter.setPen(QPen(PICTURE_COLOUR, 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(area.x() + x * area.width(), area.y() + y * area.height(), w * area.width(), h * area.height()))

    def mouseDoubleClickEvent(self, event) -> None:
        image = self._shown()
        if image is None:
            return
        area = self._area(image)
        point = event.position()
        if not self.zoomed:
            self._centre = QPointF(min(max((point.x() - area.x()) / area.width(), 0.0), 1.0), min(max((point.y() - area.y()) / area.height(), 0.0), 1.0))
        self.zoomed = not self.zoomed
        if self.zoomed:
            self.wants_full.emit()
        else:
            self.full, self._full_width = None, 0  # 24 million pixels are not kept for nothing
        self.update()

    def mousePressEvent(self, event) -> None:
        if self.zoomed:
            self._drag = (event.position(), QPointF(self._centre))

    def mouseMoveEvent(self, event) -> None:
        image = self._shown()
        if self._drag is None or image is None:
            return
        start, centre = self._drag
        area = self._area(image)
        moved = event.position() - start
        self._centre = QPointF(min(max(centre.x() - moved.x() / area.width(), 0.0), 1.0), min(max(centre.y() - moved.y() / area.height(), 0.0), 1.0))
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None


class Gauge(QWidget):
    """A correction key's gauge (GAUGES): its track, a tick at 0 and a marker at the value. Display
    only, no dragging, so the steps stay exact. The track is made once per size, not per key
    press: a press costs about 20 ms to the screen, and the gauge must not add to that."""

    MARK = 3  # px each side of the marker's centre

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key, self.value = key, 0
        self._track: Optional[QImage] = None
        self.setFixedHeight(GAUGE_HEIGHT + 4)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # as wide as its key's buttons, never wider

    def set_value(self, value: int) -> None:
        if value != self.value:
            self.value = value
            self.update()

    def marker_x(self) -> float:
        """The marker's centre in widget pixels."""
        return self.MARK + gauge_position(self.key, self.value) * (self.width() - 2 * self.MARK)

    def resizeEvent(self, event) -> None:
        self._track = None
        super().resizeEvent(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.EnabledChange:
            self._track = None
            self.update()
        super().changeEvent(event)

    def paintEvent(self, _event) -> None:
        width = self.width() - 2 * self.MARK
        if width <= 0:
            return
        if self._track is None:
            ratio = self.devicePixelRatioF()
            self._track = to_qimage(gauge_track(self.key, round(width * ratio), round(GAUGE_HEIGHT * ratio), self.isEnabled()))
            self._track.setDevicePixelRatio(ratio)
        painter = QPainter(self)
        painter.drawImage(QPointF(self.MARK, 2), self._track)
        middle = self.MARK + width / 2
        painter.setPen(QPen(QColor(0, 0, 0, 150), 1))
        painter.drawLine(QPointF(middle, 1), QPointF(middle, 3 + GAUGE_HEIGHT))
        # Light with a dark edge: stands out on the darkest and the lightest part of every track,
        # in light and dark appearance alike.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#1f1f1d" if self.isEnabled() else "#6a6a68"), 1))
        painter.setBrush(QColor("#f4f3ef" if self.isEnabled() else "#b8b8b5"))
        painter.drawRoundedRect(QRectF(self.marker_x() - self.MARK + 0.5, 0.5, 2 * self.MARK - 1, self.height() - 1), 1.5, 1.5)
        painter.end()


class OrientationBar(QWidget):
    """↺ Left, Right ↻ and Mirror, with what they have made of the picture. One on the live view
    (how the next frames are stored) and one on a frame (that frame's files); both send the same
    change by name (orient.CHANGES), and the window decides what it applies to."""

    change = Signal(str)  # "left", "right" or "mirror"

    def __init__(self, tip: str) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.buttons: dict[str, QToolButton] = {}
        buttons = (("left", "↺ Left", "Turn a quarter to the left"), ("right", "Right ↻", "Turn a quarter to the right"), ("mirror", "Mirror", "Mirror left to right"))
        for name, text, hint in buttons:
            button = self.buttons[name] = QToolButton()
            button.setText(text)
            button.setToolTip(f"{hint}. {tip}")
            button.clicked.connect(lambda _checked=False, n=name: self.change.emit(n))
            row.addWidget(button)
        self.label = QLabel()
        # As wide as its longest words from the start: grown by the text, it would change the window's minimum size.
        every = [orient.label({"turn": turn, "mirror": mirror}).capitalize() for turn in (0, 90, 180, 270) for mirror in (False, True)]
        self.label.setMinimumWidth(max(self.label.fontMetrics().horizontalAdvance(text) for text in every))
        row.addWidget(self.label)

    def show_orientation(self, orientation: Optional[dict]) -> None:
        self.label.setText(orient.label(orientation).capitalize())
