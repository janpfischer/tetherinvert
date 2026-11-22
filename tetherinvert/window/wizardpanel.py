"""The calibration wizard on Presets: a page per step, the step bar, and W4's histogram. The rules
are in wizard.py; this shows a Wizard and passes on what the operator does, the way metaedit.py does
for metadata.

While a preset is calibrated, Presets shows the panel in the column beside the live view
(presets.WizardColumn), where the film base rectangle is drawn, instead of the list.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)  # fmt: skip

from .. import kinds, wizard as rules
from ..calibrate import HISTOGRAM_BINS, HISTOGRAM_STOPS, TARGET, Histogram

CHANNEL_COLOURS = ("#d0504a", "#4ea35a", "#4f74c9")  # sensor channels R, G, B in the histogram: clear on light and dark ground alike
CEILING_COLOUR = "#d9534f"  # the sensor's ceiling, the red of error messages
NOTE_COLOUR = "#b36b00"  # W1's note that a preset is replaced: a warning, not an error
HISTOGRAM_HEIGHT = 110  # px; W4 has to fit the 900 px tall window with the result under it
STAGE_MARKS = {"done": "✓", "now": "…", "todo": "·"}
# W2's instructions for an own light: what keeps a calibration true for it.
OWN_LIGHT_ADVICE = (
    "Constant light: it must not flicker; run a light dimmed by pulse width at full. Keep the setting you note (tape the "
    "knob). Mask the light around the film and switch the room lights off."
)


class StepBar(QWidget):
    """One segment per step: those done and the current one filled, the rest grey."""

    def __init__(self) -> None:
        super().__init__()
        self.count, self.at = 4, 0
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def show_position(self, at: int, count: int) -> None:
        if (at, count) != (self.at, self.count):
            self.at, self.count = at, count
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        gap = 4
        width = (self.width() - gap * (self.count - 1)) / max(self.count, 1)
        highlight, rest = self.palette().color(QPalette.ColorRole.Highlight), self.palette().color(QPalette.ColorRole.Mid)
        for index in range(self.count):
            painter.setBrush(highlight if index <= self.at else rest)
            painter.drawRoundedRect(QRectF(index * (width + gap), 0, width, self.height()), 2, 2)
        painter.end()


class HistogramView(QWidget):
    """A calibration shot's histogram (calibrate.Histogram): x in stops below the sensor's
    ceiling, y on a log scale; a dashed line at the target, a red one at the ceiling, and a mark
    on top where each channel's film base landed. Expose to the right: the film base just under
    the ceiling, the picture to its left, nothing piled up at the right edge."""

    MARGIN, LABELS = 6, 13  # px around the plot; px under it for the stops

    def __init__(self) -> None:
        super().__init__()
        self.histograms: dict[int, Histogram] = {}
        self.setFixedHeight(HISTOGRAM_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def show_histograms(self, histograms: dict[int, Histogram]) -> None:
        self.histograms = dict(histograms)
        self.update()

    def plot(self) -> QRectF:
        return QRectF(self.MARGIN, self.MARGIN, self.width() - 2 * self.MARGIN, self.height() - self.MARGIN - self.LABELS)

    def x_of(self, stops: float) -> float:
        """Where a value, in stops below the ceiling, sits across the plot."""
        plot = self.plot()
        return plot.left() + (min(max(stops, -HISTOGRAM_STOPS), 0.0) + HISTOGRAM_STOPS) / HISTOGRAM_STOPS * plot.width()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = self.plot()
        text = self.palette().color(QPalette.ColorRole.Text)
        painter.fillRect(plot, self.palette().color(QPalette.ColorRole.Base))
        faint = QColor(text)
        faint.setAlpha(35)
        painter.setPen(QPen(faint, 1))
        for stop in range(-int(HISTOGRAM_STOPS) + 1, 0):
            painter.drawLine(QPointF(self.x_of(stop), plot.top()), QPointF(self.x_of(stop), plot.bottom()))
        font = painter.font()
        font.setPointSizeF(max(font.pointSizeF() - 2.5, 7.0))
        painter.setFont(font)
        label = QColor(text)
        label.setAlpha(160)
        painter.setPen(label)
        width = 50  # px for each label, from its stop to the left, the right or both sides
        flags = Qt.AlignmentFlag
        for stop, left, align in ((-HISTOGRAM_STOPS, 0, flags.AlignLeft), (-5.0, width / 2, flags.AlignHCenter), (0.0, width, flags.AlignRight)):
            box = QRectF(self.x_of(stop) - left, plot.bottom() + 1, width, self.LABELS)
            painter.drawText(box, align | Qt.AlignmentFlag.AlignTop, "0 EV" if stop == 0 else f"−{abs(stop):g}")
        if not self.histograms:
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Each shot's histogram appears here.")
        bins = HISTOGRAM_BINS
        for channel in sorted(self.histograms):
            heights = rules.scaled(self.histograms[channel].counts[channel])
            colour = QColor(CHANNEL_COLOURS[channel])
            points = [QPointF(self.x_of(-HISTOGRAM_STOPS), plot.bottom())]
            for index, height in enumerate(heights):
                left, right = self.x_of(-HISTOGRAM_STOPS * (1 - index / bins)), self.x_of(-HISTOGRAM_STOPS * (1 - (index + 1) / bins))
                y = plot.bottom() - float(height) * (plot.height() - 6)
                points += [QPointF(left, y), QPointF(right, y)]
            points.append(QPointF(self.x_of(0.0), plot.bottom()))
            fill = QColor(colour)
            fill.setAlpha(60)
            painter.setBrush(fill)
            painter.setPen(QPen(colour, 1.2))
            painter.drawPolygon(QPolygonF(points))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        target = QColor(text)
        target.setAlpha(150)
        painter.setPen(QPen(target, 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(self.x_of(rules.stops(TARGET)), plot.top()), QPointF(self.x_of(rules.stops(TARGET)), plot.bottom()))
        painter.setPen(QPen(QColor(CEILING_COLOUR), 1.5))
        painter.drawLine(QPointF(self.x_of(0.0) - 0.75, plot.top()), QPointF(self.x_of(0.0) - 0.75, plot.bottom()))
        for channel in sorted(self.histograms):  # the film base: a small triangle pointing down at it
            x = self.x_of(self.histograms[channel].base[channel])
            path = QPainterPath()
            path.addPolygon(QPolygonF([QPointF(x - 4, plot.top()), QPointF(x + 4, plot.top()), QPointF(x, plot.top() + 6)]))
            painter.fillPath(path, QColor(CHANNEL_COLOURS[channel]))
        painter.end()


def muted(label: QLabel) -> QLabel:
    label.setWordWrap(True)
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


class WizardPanel(QWidget):
    """The wizard's pages and buttons. The window keeps the Wizard: `load` fills the fields
    from it when a step is entered, `read` copies what was typed back, `refresh` shows the rest."""

    edited = Signal()  # a field changed
    forward = Signal()  # Next, Calibrate or Finish
    backward = Signal()
    cancel = Signal()
    stop = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.heading = QLabel()
        self.heading.setStyleSheet("font-weight: bold;")
        self.heading.setWordWrap(True)
        self.bar = StepBar()
        self.position = muted(QLabel())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._film_page())
        self.pages.addWidget(self._camera_page())
        self.pages.addWidget(self._base_page())
        self.pages.addWidget(self._calibrate_page())
        self.pages.addWidget(self._colour_page())
        self._page_of = {rules.FILM: 0, rules.CAMERA: 1, rules.FILM_BASE: 2, rules.CALIBRATE: 3, **{letter: 4 for letter in rules.COLOUR_STEPS}}
        self.missing = muted(QLabel())
        self.cancel_button, self.back_button, self.next_button, self.stop_button = QPushButton("Cancel"), QPushButton("Back"), QPushButton("Next"), QPushButton("Stop")
        self.next_button.setDefault(True)
        self.stop_button.setToolTip("Stops the calibration at its next shot; nothing is saved.")
        self.cancel_button.clicked.connect(self.cancel)
        self.back_button.clicked.connect(self.backward)
        self.next_button.clicked.connect(self.forward)
        self.stop_button.clicked.connect(self.stop)
        buttons = QHBoxLayout()
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        for button in (self.back_button, self.stop_button, self.next_button):
            buttons.addWidget(button)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        for widget in (self.heading, self.bar, self.position, self.pages, self.missing):
            column.addWidget(widget)
        column.addLayout(buttons)

    # ----- the pages -----

    def _film_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Film name, e.g. Portra 400")
        self.name_edit.textChanged.connect(self.edited)
        self.name_edit.returnPressed.connect(self.forward)
        column.addWidget(QLabel("Film name"))
        column.addWidget(self.name_edit)
        # First the light, then one of its three kinds: one choice of three, not one of six.
        self.lights = QButtonGroup(page)
        self.light_buttons: dict[str, QRadioButton] = {}
        lights = QHBoxLayout()
        column.addWidget(QLabel("Light"))
        for group in kinds.GROUPS:
            button = self.light_buttons[group] = QRadioButton(group)
            self.lights.addButton(button)
            button.toggled.connect(lambda checked, group=group: checked and self._light_chosen(group))
            lights.addWidget(button)
        lights.addStretch(1)
        column.addLayout(lights)
        self.no_scanlight = muted(QLabel("No Scanlight is connected: your own light only. Plug in its data port to use it."))
        column.addWidget(self.no_scanlight)
        self.kinds = QButtonGroup(page)
        self.kind_buttons: dict[str, QRadioButton] = {}
        self.kind_notes: dict[str, QLabel] = {}
        column.addWidget(QLabel("Film type"))
        for kind, words in kinds.KINDS.items():
            button = self.kind_buttons[kind] = QRadioButton(words.label)
            button.setToolTip(words.note)
            self.kinds.addButton(button)
            button.toggled.connect(lambda _checked: self.edited.emit())
            column.addWidget(button)
            note = self.kind_notes[kind] = muted(QLabel(words.note))
            note.setContentsMargins(22, 0, 0, 0)
            column.addWidget(note)
        self.replace_note = QLabel()
        self.replace_note.setWordWrap(True)
        self.replace_note.setStyleSheet(f"color: {NOTE_COLOUR};")
        column.addWidget(self.replace_note)
        column.addWidget(muted(QLabel("The film type is fixed once the preset is calibrated.")))
        column.addStretch(1)
        return page

    def _camera_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        self.camera_rows = {key: QLabel() for key in ("iso", "shutter", "aperture")}
        form = QFormLayout()
        form.addRow("ISO", self.camera_rows["iso"])
        form.addRow("Shutter", self.camera_rows["shutter"])
        form.addRow("Aperture", self.camera_rows["aperture"])
        column.addLayout(form)
        self.hand_box = QFrame()
        self.hand_box.setFrameShape(QFrame.Shape.StyledPanel)
        hand = QVBoxLayout(self.hand_box)
        self.fields: dict[str, QLineEdit] = {}
        self.field_labels: dict[str, QLabel] = {}
        for key, (question, example, _name) in rules.QUESTIONS.items():
            label = self.field_labels[key] = QLabel(question)
            edit = self.fields[key] = QLineEdit()
            edit.setPlaceholderText(example)
            edit.textChanged.connect(self.edited)
            edit.returnPressed.connect(self.forward)
            hand.addWidget(label)
            hand.addWidget(edit)
        hand.addWidget(muted(QLabel("Set it now and leave it. TetherInvert cannot set or check it, and every scan with this preset will remind you of it.")))
        column.addWidget(self.hand_box)
        # An own light is a setting by hand too: what it is and how it is set, stored and reminded of.
        self.light_box = QFrame()
        self.light_box.setFrameShape(QFrame.Shape.StyledPanel)
        light = QVBoxLayout(self.light_box)
        light.addWidget(QLabel("Which light is it, and how is it set?"))
        self.fields["light"] = QLineEdit()
        self.fields["light"].setPlaceholderText("Kaiser slimlite plano, full")
        self.fields["light"].textChanged.connect(self.edited)
        self.fields["light"].returnPressed.connect(self.forward)
        light.addWidget(self.fields["light"])
        light.addWidget(muted(QLabel(OWN_LIGHT_ADVICE)))
        column.addWidget(self.light_box)
        column.addStretch(1)
        return page

    def _base_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(
            "Insert the film with clear rebate in view: the unexposed edge, or the gap between two frames. Focus, and draw the "
            "film base rectangle on the rebate in the live view."
        )
        intro.setWordWrap(True)
        column.addWidget(intro)
        self.rect_check = QLabel()
        column.addWidget(self.rect_check)
        column.addWidget(muted(QLabel("A sloppy rectangle is fine: the film base is taken from its brightest part.")))
        column.addStretch(1)
        return page

    def _colour_page(self) -> QWidget:
        """own-rgb's prompted colour steps: one page for all three, filled per colour."""
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        self.colour_prompt = QLabel()
        self.colour_prompt.setWordWrap(True)
        column.addWidget(self.colour_prompt)
        self.colour_done = muted(QLabel())
        column.addWidget(self.colour_done)
        self.colour_problem = QLabel()
        self.colour_problem.setWordWrap(True)
        self.colour_problem.setStyleSheet(f"color: {CEILING_COLOUR};")
        column.addWidget(self.colour_problem)
        column.addWidget(muted(QLabel("Touch only the light's controls, never the holder or the stand: the three shots are aligned for small "
                                      "shifts, not for a turn. Space shoots too.")))  # fmt: skip
        column.addStretch(1)
        return page

    def _light_chosen(self, group: str) -> None:
        """The other light: the same mode of its three (colour in 3 shots, 1 shot, black and white)."""
        now = next((kind for kind, button in self.kind_buttons.items() if button.isChecked()), "rgb")
        if kinds.KINDS[now].group != group:
            self.kind_buttons[kinds.same_mode(now, group)].setChecked(True)
        self._show_group(group)
        self.edited.emit()

    def _show_group(self, group: str) -> None:
        for kind, entry in kinds.KINDS.items():
            self.kind_buttons[kind].setVisible(entry.group == group)
            self.kind_notes[kind].setVisible(entry.group == group)

    def _calibrate_page(self) -> QWidget:
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        self.stage_labels = [QLabel() for _ in range(4)]
        for label in self.stage_labels:
            column.addWidget(label)
        self.histogram = HistogramView()
        column.addWidget(self.histogram)
        self.caption = muted(QLabel())
        column.addWidget(self.caption)
        self.result = QLabel()
        self.result.setTextFormat(Qt.TextFormat.RichText)
        self.result.setWordWrap(True)
        column.addWidget(self.result)
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet(f"color: {CEILING_COLOUR};")
        column.addWidget(self.problem)
        column.addStretch(1)
        return page

    # ----- showing a Wizard -----

    def load(self, wizard: rules.Wizard) -> None:
        """The fields from the wizard, when it opens or a step is entered."""
        quiet = [self.name_edit, *self.fields.values(), *self.kind_buttons.values(), *self.light_buttons.values()]
        for widget in quiet:
            widget.blockSignals(True)
        self.name_edit.setText(wizard.name)
        self.kind_buttons[wizard.kind].setChecked(True)
        self.light_buttons[kinds.KINDS[wizard.kind].group].setChecked(True)
        self._show_group(kinds.KINDS[wizard.kind].group)
        for key, edit in self.fields.items():
            edit.setText(wizard.values.get(key, ""))
        for widget in quiet:
            widget.blockSignals(False)
        shown = self._page_of.get(wizard.step.key, 0)
        for index in range(self.pages.count()):  # as tall as the page shown, not as the tallest of them
            policy = QSizePolicy.Policy.Preferred if index == shown else QSizePolicy.Policy.Ignored
            self.pages.widget(index).setSizePolicy(policy, policy)
        self.pages.setCurrentIndex(shown)
        self.pages.adjustSize()

    def read(self, wizard: rules.Wizard) -> None:
        """What was typed and chosen, into the wizard."""
        if not wizard.fixed:
            wizard.name = self.name_edit.text()
            wizard.kind = next((kind for kind, button in self.kind_buttons.items() if button.isChecked()), wizard.kind)
        for key, edit in self.fields.items():
            wizard.values[key] = edit.text()

    def refresh(self, wizard: rules.Wizard, camera: dict, note: str, idle: bool, preset=None) -> None:
        """Everything but the fields: where it is, what the camera reports, what is missing, the
        buttons, and W4. `note`: W1's note on a taken name; `preset`: the saved one, for W4."""
        self.heading.setText(wizard.heading())
        self.bar.show_position(wizard.at, len(wizard.steps))
        self.position.setText(wizard.position())
        key = wizard.step.key
        if key == rules.FILM:
            self.replace_note.setText(note)
            self.replace_note.setVisible(bool(note))
            self.light_buttons["Scanlight"].setVisible(wizard.scanlight)  # the list follows the connection
            self.no_scanlight.setVisible(not wizard.scanlight)
        elif wizard.step.shoot:
            self._show_colour(wizard)
        elif key == rules.CAMERA:
            self._show_camera(wizard, camera)
        elif key == rules.FILM_BASE:
            self.rect_check.setText("✓ Film base rectangle drawn" if wizard.rect is not None else "No film base rectangle yet")
        elif key == rules.CALIBRATE:
            self._show_calibration(wizard, preset)
        missing = wizard.missing() if wizard.state == rules.EDITING else ""
        self.missing.setText(missing)
        self.missing.setVisible(bool(missing))
        label = wizard.next_label()
        self.next_button.setText(label or "Next")
        self.next_button.setVisible(bool(label))
        self.next_button.setEnabled(not missing and not wizard.shooting and (idle or label == "Finish"))
        self.back_button.setVisible(wizard.can_back() or (wizard.at > 0 and wizard.state != rules.DONE))
        self.back_button.setEnabled(wizard.can_back() and idle)
        self.cancel_button.setVisible(wizard.state != rules.DONE)
        self.cancel_button.setEnabled(wizard.can_cancel())
        self.stop_button.setVisible(wizard.state == rules.RUNNING)
        self.stop_button.setEnabled(not wizard.stopping)
        self.stop_button.setText("Stopping…" if wizard.stopping else "Stop")

    def _show_camera(self, wizard: rules.Wizard, camera: dict) -> None:
        if not wizard.camera:
            for row in self.camera_rows.values():
                row.setText("—")
        else:
            iso = camera.get("iso") or ""
            self.camera_rows["iso"].setText(
                "not reported by the camera"
                if "iso" in wizard.unknown
                else f"{iso} · as set on the camera" if iso.isdigit() else f"{iso}: TetherInvert sets the lowest ISO"
            )
            self.camera_rows["aperture"].setText("not reported by the lens" if "aperture" in wizard.unknown else f"{camera.get('aperture')} · as set on the camera")
        own = not kinds.KINDS[wizard.kind].scanlight
        self.camera_rows["shutter"].setText("found by calibrating, one per colour" if kinds.KINDS[wizard.kind].prompted else "found by calibrating")
        for key in rules.QUESTIONS:
            asked = key in wizard.unknown
            self.field_labels[key].setVisible(asked)
            self.fields[key].setVisible(asked)
        self.hand_box.setVisible(bool(wizard.unknown))
        self.light_box.setVisible(own)

    def _show_colour(self, wizard: rules.Wizard) -> None:
        letter = wizard.step.key
        name = kinds.EXPOSURES[letter].name
        setting = f" ({wizard.light_note})" if wizard.light_note else ""
        self.colour_prompt.setText(
            f"Shooting {name}…" if wizard.shooting else f"Set your light to <b>{name} only</b>, at full or the setting you noted{setting}, then <b>Shoot</b>."
        )
        done = [f"{kinds.EXPOSURES[key].name}: {shot['shutter']} s" for key, shot in wizard.colour_shots.items()]
        self.colour_done.setText(("Done: " + ", ".join(done)) if done else "")
        self.colour_problem.setText(wizard.shot_problem)
        self.colour_problem.setVisible(bool(wizard.shot_problem))

    def _show_calibration(self, wizard: rules.Wizard, preset) -> None:
        stages = wizard.stages()  # four, or three for a kind that only finds its shutter
        for index, label in enumerate(self.stage_labels):
            text, state = stages[index] if index < len(stages) else ("", "todo")
            label.setText(f"{STAGE_MARKS[state]}  {text}")
            label.setEnabled(state != "todo")
            label.setVisible(index < len(stages))
        histograms, caption = wizard.histogram()
        self.histogram.show_histograms(histograms)
        self.caption.setText(caption or ("Stopping at the next shot…" if wizard.stopping else "Calibrating: the first shot is on its way."))
        if wizard.state == rules.DONE and preset is not None:
            rows = "".join(f"<tr><td>{name}&nbsp;&nbsp;</td><td>{value}</td></tr>" for name, value in rules.result(preset))
            self.result.setText(f"<b>{preset.name} is calibrated and saved.</b><table>{rows}</table>{wizard.clipping()}")
        self.result.setVisible(wizard.state == rules.DONE and preset is not None)
        self.problem.setText(f"{wizard.problem}\nBack goes to the film base rectangle." if wizard.state == rules.FAILED else "")
        self.problem.setVisible(wizard.state == rules.FAILED)
