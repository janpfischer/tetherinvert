"""The Presets step: the film presets as a list by light, the one chosen in detail (its settings, its
film base, when and with which camera it was calibrated), deleting one, starting a roll with it, and
the calibration wizard that makes them.

The list is a page of its own, like Roll and Finish: it needs no live view. The wizard does (the film
base is drawn on it), so while it is open Presets shows the workspace with the wizard in the column
beside the live view (`WizardColumn`), and Cancel or Finish come back to the list.

The list holds the chosen preset (`PresetList.chosen`): `_preset`, `_reload_presets` and the Roll
page's picker all go through it.

The wizard's rules are in wizard.py (no Qt), its pages in wizardpanel.py. The window keeps the
Wizard while it is open (`_wizard`), tells it what only the window knows (`_refresh_wizard`: the
camera, the film base rectangle, a taken name) and drives the hardware thread's calibration.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .. import kinds
from ..calibrate import TARGET, Roi
from ..kinds import KINDS, Kind
from ..raw import FULL
from ..session import NO_LIGHT_LABEL, Preset, PresetStore, Roll
from ..wizard import CALIBRATE, DONE, EDITING, FILM, QUESTIONS, RUNNING, Wizard, replace_note
from ..workers import IDLE_LIGHT, NOT_CONNECTED
from . import style
from .roll import free_name
from .viewer import BASE_COLOUR
from .wizardpanel import CEILING_COLOUR, CHANNEL_COLOURS, WizardPanel

if TYPE_CHECKING:
    from .mainwindow import Now

PRESETS_PAGE_WIDTH = 1000  # px: the list and the chosen preset beside it, at most
LIST_WIDTH = 300  # px: the list of presets, a name, its film type and a date per row
PICTURE_SIZE = (240, 160)  # px: the film base picture in the detail, 3 : 2
# Beside a preset, a mark of how it scans (`method`), not a colour of its film base: a preset keeps none
# (its base is ~90 % in every channel after calibration, and the LED levels mostly say how weak the
# Scanlight's red is to the sensor: worked out from them an orange base comes out cyan), and a swatch by
# film type would be the same orange on every colour preset.
BW_ENDS = ("#1b1b1d", "#ededeb")  # black and white: from black to white, in either appearance


def method(kind: str) -> tuple[str, bool]:
    """How a kind is drawn beside its preset: "shots" (R, G, B one by one), "together" (colour in one
    shot) or "white" (black and white); and whether by an own light (drawn dashed)."""
    k = KINDS[kind]
    return ("white" if not k.colour else "together" if k.single else "shots"), not k.scanlight


def summary(kind: Kind) -> str:
    """A list row's second line: the film and its shots per frame."""
    if not kind.colour:
        return "Black and white"
    shots = len(kind.exposures)
    return f"Colour · {shots} shot{'s' if shots != 1 else ''}"


def divider() -> QFrame:
    line = QFrame()
    style.set_role(line, "divider")
    line.setFixedHeight(1)
    return line


def calibrated_on(created: str, long: bool = False) -> str:
    """"19 Oct" in the list, "19 Oct 2026, 09:34" in the detail; as stored if it does not read as a date."""
    try:
        when = datetime.strptime(created, "%Y-%m-%d %H:%M")
    except ValueError:
        return created
    return f"{when.day} {when:%b %Y, %H:%M}" if long else f"{when.day} {when:%b}"


class MethodMark(QWidget):
    """A small square that says how a preset scans (`method`): three stripes, red, green and blue, for
    one shot each; the three blended for colour in one shot; black to white for black and white. An own
    light's mark has a dashed outline. Its tooltip says the kind in words."""

    def __init__(self, size: int) -> None:
        super().__init__()
        self.setFixedSize(size, size)
        self.kind = "rgb"

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.setToolTip(("Own light. " if not KINDS[kind].scanlight else "Scanlight. ") + KINDS[kind].label)
        self.update()

    def paintEvent(self, _event) -> None:
        how, own = method(self.kind)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = self.width() / 4
        outline = QPainterPath()
        outline.addRoundedRect(area, radius, radius)
        painter.setClipPath(outline)
        if how == "shots":
            gap = max(1.0, self.width() / 14)
            width = (area.width() - 2 * gap) / 3
            for index, colour in enumerate(CHANNEL_COLOURS):
                painter.fillRect(QRectF(area.left() + index * (width + gap), area.top(), width, area.height()), QColor(colour))
        else:
            blend = QLinearGradient(area.topLeft(), area.topRight() if how == "together" else area.bottomRight())
            stops = CHANNEL_COLOURS if how == "together" else BW_ENDS
            for index, colour in enumerate(stops):
                blend.setColorAt(index / (len(stops) - 1), QColor(colour))
            painter.fillRect(area, blend)
        painter.setClipping(False)
        t = style.tokens()
        pen = QPen(QColor(t.text2 if own else t.line), 1.5 if own else 1)
        if own:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(area, radius, radius)


class PresetRow(QPushButton):
    """A preset in the list: the mark of how it scans, its name, film type and shots, and the date it was calibrated. A
    checkable button, so it takes the keyboard and says it is chosen; laid out inside, so it is as
    big as its layout (a button's own size hint knows only its text)."""

    def __init__(self, name: str, about: str, date: str, kind: str) -> None:
        super().__init__()
        style.set_role(self, "choice")
        self.setCheckable(True)
        self.setAccessibleName(f"{name}, {about}")
        self.mark = MethodMark(28)
        self.mark.set_kind(kind)
        words = QVBoxLayout()
        words.setSpacing(0)
        words.addWidget(style.label(name, "heading", wrap=False))
        words.addWidget(style.label(about, "muted", wrap=False))
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(12)
        row.addWidget(self.mark)
        row.addLayout(words, 1)
        row.addWidget(style.label(date, "muted", wrap=False))

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()


class PresetList(QWidget):
    """The presets grouped by light (`kinds.GROUPS`), one of them chosen. `picked` when the choice
    changes, by a click or by `choose`."""

    picked = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(LIST_WIDTH)
        self.rows: dict[str, PresetRow] = {}
        self._entries: tuple = ()
        self._chosen: Optional[str] = None
        self._group = QButtonGroup(self)  # one checked at a time
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(6)

    def chosen(self) -> Optional[str]:
        return self._chosen

    def names(self) -> list[str]:
        """The presets in the order listed."""
        return list(self.rows)

    def choose(self, name: Optional[str], emit: bool = True) -> None:
        """Choose a preset (None: none); `picked` follows a change unless `emit` is False."""
        name = name if name in self.rows else None
        for row_name, row in self.rows.items():
            row.setChecked(row_name == name)
        if name != self._chosen:
            self._chosen = name
            if emit:
                self.picked.emit(name or "")

    def set_presets(self, groups: list[tuple[str, list[tuple[str, str, str, str]]]]) -> None:
        """Per group its presets: name, film type, date, kind. Built again only when they changed: a
        row added to a page on screen is shown a turn of the event loop later, and the page is laid out
        short until then."""
        entries = tuple((group, tuple(rows)) for group, rows in groups)
        if entries == self._entries:
            return
        self._entries = entries
        while self._column.count():
            item = self._column.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()  # at once: deleted only at the event loop's next turn, it would be drawn over the new rows until then
                item.widget().deleteLater()
        for row in self.rows.values():
            self._group.removeButton(row)
        self.rows = {}
        for group, rows in entries:
            heading = style.label(group, "muted", wrap=False)
            heading.setContentsMargins(4, 6, 0, 0)
            self._column.addWidget(heading)
            heading.show()
            frame, inside = style.panel()
            inside.setContentsMargins(4, 4, 4, 4)
            inside.setSpacing(2)
            for name, about, date, kind in rows:
                row = self.rows[name] = PresetRow(name, about, date, kind)
                self._group.addButton(row)
                row.clicked.connect(lambda _checked=False, picked=name: self.choose(picked))
                inside.addWidget(row)
            if not rows:
                none = style.label(f"No presets for {'the Scanlight' if group == 'Scanlight' else 'an own light'} yet.", "muted")
                none.setContentsMargins(8, 6, 8, 6)
                inside.addWidget(none)
            self._column.addWidget(frame)
            frame.show()  # at once, rows and all
        self._column.addStretch(1)
        chosen, self._chosen = self._chosen, None
        self.choose(chosen, emit=False)


class BasePicture(QWidget):
    """Where the film base was metered: the live view around its rectangle as it was kept at
    calibration, or, for a preset calibrated on the command line, the rectangle on a
    plain frame with a ring around it (the rectangles drawn are slivers of rebate, hard to find)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(*PICTURE_SIZE)
        self.image: Optional[QImage] = None
        self.roi: Optional[tuple[float, float, float, float]] = None

    def show_base(self, image: Optional[QImage], roi: Optional[tuple[float, float, float, float]]) -> None:
        self.image, self.roi = image, roi
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(self.rect()), 8, 8)
        painter.setClipPath(outline)
        painter.fillRect(self.rect(), QColor(style.SURROUND))
        if self.image is not None:
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            area = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2, size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(area, self.image)
        elif self.roi is not None:
            x, y, w, h = self.roi
            rect = QRectF(x * self.width(), y * self.height(), max(w * self.width(), 2.0), max(h * self.height(), 2.0))
            painter.setPen(QPen(BASE_COLOUR, 2))
            painter.drawRect(rect)
            painter.drawEllipse(rect.center(), 14, 14)


class BaseLevels(QWidget):
    """The film base per channel against the sensor's ceiling, as calibration left it: a bar each, the
    target dotted, the ceiling in red, the share in numbers."""

    ROW = 20  # px per channel

    def __init__(self) -> None:
        super().__init__()
        self.base: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.setFixedHeight(3 * self.ROW)
        self.setMinimumWidth(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def show_base(self, base: tuple[float, float, float]) -> None:
        self.base = base
        self.update()

    def shares(self) -> list[float]:
        return [min(value / FULL, 1.0) for value in self.base]

    def paintEvent(self, _event) -> None:
        t = style.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mono = painter.font()
        mono.setFamily(style.mono_family())
        painter.setFont(mono)
        left, right = 22, self.width() - 56
        for index, (letter, share) in enumerate(zip("RGB", self.shares(), strict=True)):
            middle = index * self.ROW + self.ROW / 2
            painter.setPen(QColor(t.text))
            painter.drawText(QRectF(0, middle - 9, 16, 18), Qt.AlignmentFlag.AlignCenter, letter)
            painter.drawText(QRectF(right + 8, middle - 9, 48, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{share:.1%}")
            track = QRectF(left, middle - 4, right - left, 8)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.divider))
            painter.drawRoundedRect(track, 4, 4)
            painter.setBrush(QColor(CHANNEL_COLOURS[index]))
            painter.drawRoundedRect(QRectF(left, middle - 4, track.width() * share, 8), 4, 4)
            target = left + track.width() * TARGET
            painter.setPen(QPen(QColor(t.muted), 1.5, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(target, middle - 7), QPointF(target, middle + 7))
            painter.setPen(QPen(QColor(CEILING_COLOUR), 2))
            painter.drawLine(QPointF(right, middle - 7), QPointF(right, middle + 7))


class PresetsPage(QWidget):
    """The presets by light on the left; the chosen one on the right: its settings with what to set by
    hand, its film base, when it was calibrated, and what can be done with it."""

    SETTINGS = ("ISO", "Shutter", "Aperture", "Light")  # Preset.settings, in its order

    def __init__(self) -> None:
        super().__init__()
        # The way into the calibration wizard is a button, here and in the Roll page's picker. As the
        # list's last entry ("+ New preset…") it could not be chosen again after a cancel, and with no
        # presets it would be the list's only entry.
        self.new_preset_button = QPushButton("New preset…")
        style.set_role(self.new_preset_button, "primary")
        self.new_preset_button.setToolTip("Calibrate a new film preset on the film base of a strip in the holder")
        scroll, column = style.full_page(
            "One per film stock, calibrated once on its film base. Every roll of that film is then exposed the same way.", PRESETS_PAGE_WIDTH, self.new_preset_button
        )
        self.preset_list = PresetList()

        detail, inside = style.panel()
        inside.setContentsMargins(22, 18, 22, 18)
        inside.setSpacing(10)
        self.mark = MethodMark(48)
        self.name_label = style.label("", "title")
        self.kind_label = style.label("", "muted")
        self.recalibrate_button = QPushButton("Recalibrate…")
        self.recalibrate_button.setToolTip("Calibrate this preset again, under its name and film type; rolls scanned with it keep their own copy")
        self.delete_button = QPushButton("Delete…")
        style.set_role(self.delete_button, "danger")
        self.delete_button.setToolTip("Delete this preset; rolls scanned with it keep their own copy")
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(self.name_label)
        names.addWidget(self.kind_label)
        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(names, 1)
        top.addWidget(self.recalibrate_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.delete_button, 0, Qt.AlignmentFlag.AlignTop)
        inside.addLayout(top)
        self.empty_note = style.label("No film preset yet. <b>New preset…</b> calibrates one on the film base of a strip in the holder.", "muted")
        self.empty_note.setTextFormat(Qt.TextFormat.RichText)
        inside.addWidget(self.empty_note)

        # Everything about the chosen preset, hidden while there is none.
        self.about = QWidget()
        about = QVBoxLayout(self.about)
        about.setContentsMargins(0, 4, 0, 0)
        about.setSpacing(10)
        about.addWidget(style.label("Settings", "heading"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(3, 1)  # "set this yourself" right after its value
        self.setting_rows: dict[str, tuple] = {}  # name: its label, its value, "set this yourself"
        for index, name in enumerate(self.SETTINGS):
            row = self.setting_rows[name] = (style.label(name, "muted", wrap=False), style.label("", "data"), style.label("set this yourself", "chip", wrap=False))
            grid.addWidget(row[0], index, 0)
            grid.addWidget(row[1], index, 1)
            grid.addWidget(row[2], index, 2, Qt.AlignmentFlag.AlignLeft)
        about.addLayout(grid)
        self.kind_note = style.label("", "muted")
        about.addWidget(self.kind_note)
        about.addWidget(divider())
        about.addWidget(style.label("Film base", "heading"))
        self.base_picture = BasePicture()
        self.base_levels = BaseLevels()
        self.base_caption = style.label("", "muted")
        levels = QVBoxLayout()
        levels.setSpacing(6)
        levels.addWidget(self.base_levels)
        levels.addWidget(style.label("Each channel of the film base against the sensor's ceiling (red); calibration aims at the dotted line.", "muted"))
        levels.addStretch(1)
        base = QHBoxLayout()
        base.setSpacing(18)
        pictured = QVBoxLayout()
        pictured.setSpacing(4)
        pictured.addWidget(self.base_picture)
        pictured.addWidget(self.base_caption)
        self.base_caption.setFixedWidth(PICTURE_SIZE[0])
        base.addLayout(pictured)
        base.addLayout(levels, 1)
        about.addLayout(base)
        about.addWidget(divider())
        self.calibrated_label = style.label("", "muted")
        self.start_button = QPushButton("Start a roll")
        style.set_role(self.start_button, "primary")
        self.start_button.setMinimumHeight(36)
        self.start_button.setToolTip("Open Roll with this preset chosen, for a new roll")
        foot = QHBoxLayout()
        foot.addWidget(self.calibrated_label, 1)
        foot.addWidget(self.start_button)
        about.addLayout(foot)
        inside.addWidget(self.about)
        inside.addStretch(1)

        sides = QHBoxLayout()
        sides.setSpacing(20)
        sides.addWidget(self.preset_list, 0, Qt.AlignmentFlag.AlignTop)
        sides.addWidget(detail, 1, Qt.AlignmentFlag.AlignTop)
        column.addLayout(sides)
        column.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def show_preset(self, preset: Optional[Preset], picture: Optional[QImage]) -> None:
        """The chosen preset, or what to do while there is none. Whatever goes is hidden before the
        other is shown."""
        about = [self.about, self.mark, self.name_label, self.kind_label, self.recalibrate_button, self.delete_button]
        going, coming = ([self.empty_note], about) if preset is not None else (about, [self.empty_note])
        for widget in going:
            widget.setVisible(False)
        for widget in coming:
            widget.setVisible(True)
        if preset is None:  # the header says "Film presets" already
            return
        kind = KINDS[preset.kind]
        self.mark.set_kind(preset.kind)
        self.name_label.setText(preset.name)
        self.kind_label.setText(kind.label)
        for name, value, by_hand in preset.settings():
            label, shown, chip = self.setting_rows[name]
            label.setText("LEDs" if name == "Light" and kind.scanlight else name)
            shown.setText(value)
            chip.setVisible(by_hand)
        self.kind_note.setText(kind.note)
        self.base_picture.setVisible(True)
        self.base_picture.show_base(picture, preset.roi)
        self.base_caption.setVisible(self.base_picture.isVisibleTo(self))
        self.base_caption.setText("The live view at calibration." if picture is not None else "Where on the frame it was metered.")
        self.base_levels.show_base(preset.base)
        when = calibrated_on(preset.created, long=True)
        self.calibrated_label.setText(" · ".join(filter(None, [f"Calibrated {when}" if when else "Calibration date not recorded", preset.camera])))
        self.start_button.setText(f"Start a roll with {preset.name}")

    def settings_lines(self) -> list[str]:
        """The settings as shown, a line each: "Aperture: f/8 — set this yourself"."""
        return [f"{label.text()}: {value.text()}" + (" — set this yourself" if not chip.isHidden() else "") for label, value, chip in self.setting_rows.values()]


class WizardColumn(QWidget):
    """Beside the live view while a preset is calibrated: the wizard (wizardpanel.py). The panel is shown
    only then: a stack of columns counts each one's preferred height, shown or not, and the wizard's
    whole height would make Develop's workspace too tall for the screen with a frame's metadata open."""

    def __init__(self) -> None:
        super().__init__()
        self.wizard_panel = WizardPanel()
        self.wizard_panel.hide()
        column = QVBoxLayout(self)
        column.setContentsMargins(style.COLUMN_MARGIN, style.COLUMN_MARGIN, style.COLUMN_MARGIN, style.COLUMN_MARGIN)
        column.addWidget(self.wizard_panel)
        column.addStretch(1)


class PresetsPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_presets(self) -> None:
        self._store = PresetStore()
        self._wizard: Optional[Wizard] = None  # the calibration wizard (wizard.py), while it is open
        self._metered: Optional[QImage] = None  # the live view around the film base, from leaving W3: kept with the preset when it is saved

    def _calibrating(self) -> bool:
        """The calibration wizard is open."""
        return self._wizard is not None

    def _wizard_shoots(self) -> bool:
        """The wizard is at one of own-rgb's colour steps, where Space shoots."""
        return self._wizard is not None and self._wizard.step.shoot

    def _preset(self) -> Optional[Preset]:
        name = self.presets_page.preset_list.chosen()
        try:
            return self._store.get(name) if name else None
        except KeyError:
            return None

    def _choose_preset(self, name: str) -> None:
        self.presets_page.preset_list.choose(name)

    def _reload_presets(self, select: str = "") -> None:
        """The list again from the store, with `select` chosen, else the first listed."""
        presets = [self._store.get(name) for name in self._store.names()]
        groups = []
        for group in kinds.GROUPS:
            rows = [(p.name, summary(KINDS[p.kind]), calibrated_on(p.created), p.kind) for p in presets if KINDS[p.kind].group == group]
            groups.append((group, rows))
        listed = self.presets_page.preset_list
        listed.set_presets(groups)
        chosen = self._store.find(select) if select else None
        listed.choose(chosen if chosen in listed.rows else (listed.names() or [None])[0], emit=False)
        self._on_preset_chosen()

    def _delete_preset(self) -> None:
        name = self.presets_page.preset_list.chosen()
        if name and self._confirm("Delete preset", f"Delete the preset “{name}”?"):
            self._store.delete(name)
            self._reload_presets()

    def _on_preset_chosen(self) -> None:
        preset = self._preset()
        picture = None
        if preset is not None:
            path = self._store.picture(preset.name)
            picture = QImage(str(path)) if path.exists() else None
        self.presets_page.show_preset(preset, picture if picture is not None and not picture.isNull() else None)
        self.ask_framing.emit(IDLE_LIGHT if preset is None else preset.framing_light())
        self._show_light()
        self._update_buttons()

    def _start_roll(self) -> None:
        """Start a roll with …: Roll, with this preset chosen. A roll that exists keeps its own preset,
        so a roll in the fields that is not new gives way to a free name."""
        if self._in_session() or self._calibrating() or self._preset() is None:
            return
        if Roll.state(self._roll_folder()) != "new":
            self._pick_roll(free_name(self._rolls_folder()))
        self._go("roll")

    def _new_preset(self) -> None:
        self._open_wizard(Wizard.new_preset(self._store.newest(), self.view.rects["base"]), IDLE_LIGHT)

    def _recalibrate(self) -> None:
        preset = self._preset()
        if preset is not None:
            self._open_wizard(Wizard.recalibrate(preset), preset.framing_light())

    def _open_wizard(self, wizard: Wizard, light: tuple) -> None:
        self._wizard = wizard
        self._metered = None
        if wizard.rect is not None:  # Recalibrate…: the preset's own rectangle
            self.view.rects["base"] = wizard.rect
            self.view.update()
        self.ask_framing.emit(light)
        self.wizard_column.wizard_panel.load(wizard)
        self._go("presets")  # beside the live view, where the film base is drawn
        self._update_buttons()
        if wizard.step.key == FILM:
            self.wizard_column.wizard_panel.name_edit.setFocus()

    def _refresh_wizard(self) -> None:
        """Tell the wizard what only the window knows, and show it."""
        wizard = self._wizard
        panel = self.wizard_column.wizard_panel
        wizard.scanlight = self._light_text not in (NOT_CONNECTED, NO_LIGHT_LABEL)
        if not wizard.scanlight and not wizard.fixed and wizard.step.key == FILM and KINDS[wizard.kind].scanlight:
            wizard.kind = kinds.same_mode(wizard.kind, "Own light")  # W1 offers only an own light while no Scanlight is connected
            panel.load(wizard)
        wizard.camera = self._camera_connected and bool(self._camera)
        wizard.unknown = tuple(key for key in QUESTIONS if not self._camera.get(key)) if wizard.camera else ()
        if wizard.step.base_tool and wizard.state == EDITING:
            wizard.rect = self.view.rects["base"]
        taken = None if wizard.fixed or not wizard.name.strip() else self._store.find(wizard.name)
        note = replace_note(wizard.name, wizard.kind, self._store.get(taken)) if taken else ""
        saved = self._store.get(wizard.saved) if wizard.saved and self._store.find(wizard.saved) else None
        panel.refresh(wizard, self._camera, note, not self._busy, saved)

    def _wizard_edited(self) -> None:
        if self._wizard is not None:
            self.wizard_column.wizard_panel.read(self._wizard)
            self._update_buttons()

    def _wizard_forward(self) -> None:
        """Next, Calibrate or Finish (also Enter in a field)."""
        wizard = self._wizard
        if wizard is None:
            return
        if wizard.state == DONE:
            self._close_wizard(wizard.saved)
            return
        self.wizard_column.wizard_panel.read(wizard)
        self._refresh_wizard()
        if self._busy and wizard.next_label() in ("Calibrate", "Shoot"):  # a job of the hardware thread is still running
            return
        if wizard.step.shoot:  # own-rgb: the operator has set the light to this colour
            letter = wizard.shoot()
            if letter:
                self.ask_calibrate_colour.emit(Roi(*wizard.rect), wizard.kind, letter)
            self._update_buttons()
            return
        # The film base as the operator saw it when the rectangle was settled: by the time W4 ends an own
        # light may be set to one colour, and the film moved on.
        metered = self.view.picture_around("base") if wizard.step.base_tool else None
        if not wizard.next():
            self._update_buttons()  # says what is missing
            return
        if metered is not None:
            self._metered = metered
        self._entered_step()

    def _entered_step(self) -> None:
        wizard = self._wizard
        self.wizard_column.wizard_panel.load(wizard)
        if wizard.step.base_tool:
            self._go("presets")
        if wizard.step.key == CALIBRATE and wizard.state == RUNNING:
            self._hardware.stop_calibration.clear()
            extra = {"light_note": wizard.light_note, "shots": dict(wizard.colour_shots)}
            self.ask_calibrate.emit(wizard.name.strip(), wizard.kind, Roi(*wizard.rect), wizard.by_hand(), extra)
        self._update_buttons()

    def _on_colour_shot(self, letter: str, shot: Optional[dict], problem: str) -> None:
        if self._wizard is None:
            return
        if self._wizard.shot(letter, shot, problem):
            self._entered_step()  # the next colour, or Calibrate after the last
        else:
            self._update_buttons()

    def _wizard_back(self) -> None:
        wizard = self._wizard
        if wizard is None:
            return
        self.wizard_column.wizard_panel.read(wizard)
        if wizard.back():
            self.wizard_column.wizard_panel.load(wizard)
            if wizard.step.base_tool:
                self._go("presets")
        self._update_buttons()

    def _wizard_stop(self) -> None:
        """Stop: the calibration ends at its next shot; nothing is saved."""
        if self._wizard is not None and self._wizard.state == RUNNING:
            self._hardware.stop_calibration.set()  # straight from here: the hardware thread is busy calibrating
            self._wizard.stopping = True
            self._update_buttons()

    def _wizard_cancel(self) -> None:
        if self._wizard is not None and self._wizard.can_cancel():
            self._close_wizard(None)

    def _close_wizard(self, saved: Optional[str]) -> None:
        """Finish (the saved preset chosen, its framing light on) or Cancel (as before the wizard); back
        to the list."""
        self._wizard, self._metered = None, None
        if saved:
            self._reload_presets(saved)
        else:
            self._on_preset_chosen()
        if self._step == "presets":
            self._go("presets")

    def _on_calibration_step(self, step) -> None:
        if self._wizard is not None:
            self._wizard.record(step)
            self._refresh_wizard()

    def _on_calibrated(self, name: str, problem: str) -> None:
        if name:
            self._say(f"Saved preset “{name}”.", "file")
            if self._metered is not None:  # the store dropped the picture of the calibration it replaced
                path = self._store.picture(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                self._metered.save(str(path), "JPG", 85)
        if self._wizard is None:
            return
        if name:
            self._wizard.done(name)
        else:
            self._wizard.failed(problem or "Calibration did not finish.")
        self._update_buttons()


    def _update_presets_page(self, now: "Now") -> None:
        page = self.presets_page
        free = not now.in_session  # a session keeps its preset, as it keeps its roll
        chosen = self._preset() is not None
        page.preset_list.setEnabled(free)
        page.new_preset_button.setEnabled(now.idle and free and not now.calibrating)
        for button in (page.recalibrate_button, page.delete_button):
            button.setEnabled(now.idle and free and not now.calibrating and chosen)
        page.start_button.setEnabled(free and not now.calibrating and chosen)
        self.wizard_column.wizard_panel.setVisible(now.calibrating)
        # The film base rectangle and its tool exist in the wizard's film base step only (W3).
        drawing = now.calibrating and self._wizard.step.base_tool and self._wizard.state == EDITING
        hidden = set() if drawing else {"base"}
        if hidden != self.view.hidden:
            self.view.hidden = hidden
            self.live_page.base_tool.setVisible(drawing)
            (self.live_page.base_tool if drawing else self.live_page.picture_tool).setChecked(True)
            self.view.tool = "base" if drawing else "picture"
            self.view.update()
        if now.calibrating:
            self._refresh_wizard()
