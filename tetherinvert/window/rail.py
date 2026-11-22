"""The rail down the window's left edge: the way around the window.

At the top the steps of every roll, Roll, Scan, Develop and Finish, with counts (frames scanned,
frames to print). They are modes, not a wizard: any of them can be opened at any time, as a frame
can be developed during a session. At the foot what is needed now and then, from every step:
Presets (once per film stock), and Lightroom and the devices, which open as popovers, each with a
dot that says whether it is there.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import style

WIDTH = 76  # px
MARGIN = 8  # px between a popover and the window's edge
STEPS = ("roll", "scan", "develop", "finish")  # the steps of a roll, top down
FOOT = ("presets", "lightroom", "devices")  # at the rail's foot; presets is a page, the other two open popovers
LABELS = {"roll": "Roll", "scan": "Scan", "develop": "Develop", "finish": "Finish", "presets": "Presets", "lightroom": "Lightroom", "devices": "Devices"}
TIPS = {
    "roll": "The roll: its name and folder, its preset and metadata; start or continue its session",
    "scan": "The live view and the session: scan frame after frame (Esc, Home)",
    "develop": "A frame as its print will be: its base and corrections",
    "finish": "The roll's prints, contact sheet and files",
    "presets": "Film presets: one per film stock, calibrated once on its film base",
    "lightroom": "Lightroom Classic: the roll's folders it watches",
    "devices": "The camera and the light",
}
# Line drawings on a 20 × 20 grid, stroked in the text colour. {c} is the colour, {g} the rail's ground.
ICONS = {
    "roll": '<path d="M3 6a1 1 0 0 1 1-1h4.5L10 7h6a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/>',
    "scan": '<path d="M3 7V4h3M14 4h3v3M17 13v3h-3M6 16H3v-3"/><circle cx="10" cy="10" r="3"/>',
    "develop": (
        '<path d="M3 5.5h14M3 10h14M3 14.5h14"/><rect x="6" y="4" width="3" height="3" rx="1" fill="{g}"/>'
        '<rect x="11.5" y="8.5" width="3" height="3" rx="1" fill="{g}"/><rect x="5" y="13" width="3" height="3" rx="1" fill="{g}"/>'
    ),
    "finish": '<path d="M6 8V3h8v5M6 15H3.5V8h13v7H14"/><rect x="6" y="12" width="8" height="5"/>',
    "presets": '<rect x="4" y="5" width="9" height="12" rx="2"/><path d="M13 8h3v6h-3M6.5 3h4v2"/>',
    "lightroom": '<path d="M16 8a6 6 0 0 0-11-2M4 12a6 6 0 0 0 11 2"/><path d="M5 3v3h3M15 17v-3h-3"/>',
    "devices": '<rect x="2.5" y="6" width="15" height="10" rx="2"/><path d="M7 6l1.2-2h3.6L13 6"/><circle cx="10" cy="11" r="2.6"/>',
}
LOGO = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30 30"><rect x="1" y="1" width="28" height="28" rx="7" fill="#1E2127"/>'
    '<g transform="rotate(-9 15 15)"><rect x="-4" y="9" width="38" height="12" fill="#D9692A"/>'
    + "".join(f'<rect x="{x}" y="10" width="2.4" height="1.6" rx="0.6" fill="#1E2127"/>' for x in (3, 8, 13, 18, 23))
    + '<rect x="11" y="13.2" width="8" height="5" rx="0.8" fill="#F7E6D6"/></g></svg>'
)


def icon_svg(name: str, colour: str, ground: str) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="none" stroke="{colour}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name].format(c=colour, g=ground)}</svg>'
    ).encode()


class RailButton(QAbstractButton):
    """An icon over its label. The current step stands out on a panel; a count (`badge`) and a dot
    (`dot`: ok, danger or off) sit at its top right."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.badge = ""
        self.badge_accent = False  # a count that asks for something: frames to print
        self.dot = ""
        self.setText(LABELS[name])
        self.setToolTip(TIPS[name])
        self.setAccessibleName(LABELS[name])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(WIDTH - 16, 54)

    def set_badge(self, text: str, accent: bool = False) -> None:
        if (self.badge, self.badge_accent) != (text, accent):
            self.badge, self.badge_accent = text, accent
            self.setAccessibleDescription(text)
            self.update()

    def set_dot(self, tone: str) -> None:
        """ok (green), danger (red), off (grey: not watching, not needed here) or "" (none)."""
        if self.dot != tone:
            self.dot = tone
            self.update()

    def sizeHint(self) -> QSize:
        return self.size()

    def paintEvent(self, _event) -> None:
        t = style.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        current = self.isChecked()
        if current:
            painter.setPen(QPen(QColor(t.line), 1))
            painter.setBrush(QColor(t.panel))
            painter.drawRoundedRect(box, 10, 10)
        elif self.underMouse() and self.isEnabled():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.rail_line))
            painter.drawRoundedRect(box, 10, 10)
        colour = t.accent_text if current else t.text2 if self.isEnabled() else t.muted
        QSvgRenderer(QByteArray(icon_svg(self.name, colour, t.panel if current else t.rail))).render(painter, QRectF((self.width() - 22) / 2, 8, 22, 22))
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Bold if current else QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(t.text if current else colour))
        painter.drawText(QRectF(0, 33, self.width(), 16), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.badge:
            font.setPixelSize(10)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            width = max(16.0, painter.fontMetrics().horizontalAdvance(self.badge) + 8.0)
            place = QRectF(self.width() - width - 2, 3, width, 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.accent if self.badge_accent else t.badge))
            painter.drawRoundedRect(place, 8, 8)
            painter.setPen(QColor(t.on_accent if self.badge_accent else t.text))
            painter.drawText(place, Qt.AlignmentFlag.AlignCenter, self.badge)
        if self.dot:
            painter.setPen(QPen(QColor(t.rail), 2))
            painter.setBrush(QColor({"ok": t.ok, "danger": t.danger}.get(self.dot, t.muted)))
            painter.drawEllipse(QRectF(self.width() - 18, 5, 9, 9))

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)


class Logo(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(30, 30)
        self.setToolTip("TetherInvert")

    def paintEvent(self, _event) -> None:
        QSvgRenderer(QByteArray(LOGO.encode())).render(QPainter(self), QRectF(self.rect()))


class Rail(QFrame):
    """The steps (`step` when one is chosen) and the foot (`foot`: Lightroom or Devices was clicked)."""

    step = Signal(str)
    foot = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(WIDTH)
        self.setAutoFillBackground(False)
        self.buttons: dict[str, RailButton] = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(8, 14, 8, 12)
        column.setSpacing(4)
        column.addWidget(Logo(), 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(14)
        for name in STEPS + FOOT:
            if name == FOOT[0]:
                column.addStretch(1)
            button = self.buttons[name] = RailButton(name)
            if name in STEPS or name == "presets":
                button.setCheckable(True)
                button.clicked.connect(lambda _checked=False, n=name: self.step.emit(n))
            else:
                button.clicked.connect(lambda _checked=False, n=name: self.foot.emit(n))
            column.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_current(self, step: str) -> None:
        for name, button in self.buttons.items():
            if button.isCheckable():
                button.setChecked(name == step)

    def paintEvent(self, _event) -> None:
        t = style.tokens()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(t.rail))
        painter.setPen(QColor(t.rail_line))
        painter.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        first = self.buttons[FOOT[0]].geometry().top() - 6
        painter.drawLine(10, first, self.width() - 11, first)  # the foot apart from the steps


class Popover(QFrame):
    """A small panel opened beside a button at the rail's foot; a click elsewhere closes it. A `chip`
    beside the title says the state of what it is about (Lightroom watching, the devices there)."""

    def __init__(self, title: str, content: QWidget, parent: QWidget, chip: QLabel | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        style.set_role(self, "panel")
        self.content = content
        column = QVBoxLayout(self)
        column.setContentsMargins(16, 14, 16, 16)
        column.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(style.set_role(QLabel(title), "heading"))
        top.addStretch(1)
        if chip is not None:
            top.addWidget(chip)
        column.addLayout(top)
        column.addWidget(content)
        self.setMinimumWidth(320)

    def show_beside(self, button: QWidget) -> None:
        """Beside the button, its top at the button's, unless that would hang out of the window."""
        self.adjustSize()
        self._show_at(button, button.mapToGlobal(QPoint(button.width() + 12, 0)))

    def _show_at(self, button: QWidget, place: QPoint) -> None:
        """At `place`, moved inside the button's window: at the rail's foot it would hang below it."""
        window = button.window()
        inside = QRect(window.mapToGlobal(QPoint(0, 0)), window.size()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
        place.setX(max(inside.left(), min(place.x(), inside.right() + 1 - self.width())))
        place.setY(max(inside.top(), min(place.y(), inside.bottom() + 1 - self.height())))
        self.move(place)
        self.show()
