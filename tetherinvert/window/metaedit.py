"""Entering metadata in the window: the roll's form on the Roll page, and the row above a frame (metadata.py
holds the fields and what goes into the files).

The frame row is collapsed by default into one line; a click or L opens it. Its text fields are
plain QLineEdits, so the window's key filter, which leaves editable text fields alone, never
takes the letters typed there for correction keys.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QComboBox, QFormLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)  # fmt: skip

from .. import metadata

PROBLEM_STYLE = "border: 1px solid #d9534f;"  # a field whose text is not taken: the red of error messages
RECENT = 10  # cameras and lenses offered from those typed before


def muted(label: QLabel) -> QLabel:
    """Grey text that follows light and dark appearance: the palette's, not a colour of our own."""
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


class FlowLayout(QLayout):
    """Widgets in rows that wrap, so many keywords make the row taller, never the window wider."""

    def __init__(self, parent: Optional[QWidget] = None, spacing: int = 4) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 (Qt's name)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, move=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, move: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() and line > 0:
                x, y, line = rect.x(), y + line + self.spacing(), 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line = max(line, hint.height())
        return y + line - rect.y()


class KeywordBox(QWidget):
    """Keywords as chips, and a field to add one (Enter or a comma). With `inherited` (a frame),
    the roll's keywords come first, outlined: × leaves one out on this frame, ↺ gives it back.
    The box's own keywords are filled; × removes one."""

    changed = Signal()

    def __init__(self, placeholder: str = "Add a keyword") -> None:
        super().__init__()
        self.own: list[str] = []
        self.inherited: list[str] = []
        self.left_out: list[str] = []
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(placeholder)
        self.entry.setMinimumWidth(110)
        self.entry.returnPressed.connect(self._take_entry)
        self.entry.textEdited.connect(lambda text: self._take_entry() if "," in text else None)
        self._flow = FlowLayout(self)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._rebuild()

    def set_keywords(self, own: list[str], inherited: Optional[list[str]] = None, left_out: Optional[list[str]] = None) -> None:
        self.own, self.inherited, self.left_out = list(own), list(inherited or []), list(left_out or [])
        self._rebuild()

    def pending(self) -> str:
        """Text typed but not yet taken: counts as one more keyword when the box is read."""
        return self.entry.text().strip()

    def keywords(self) -> list[str]:
        return metadata.unique(self.own + [self.pending()])

    def left_out_keywords(self) -> list[str]:
        roll = {k.lower() for k in self.inherited}
        return [k for k in self.left_out if k.lower() in roll]

    def _take_entry(self) -> None:
        words = [word.strip() for word in self.entry.text().split(",") if word.strip()]
        self.entry.clear()
        if words:
            self.own = metadata.unique(self.own + words)
            self._rebuild()
            self.changed.emit()

    def _toggle(self, word: str) -> None:
        if word.lower() in {k.lower() for k in self.left_out}:
            self.left_out = [k for k in self.left_out if k.lower() != word.lower()]
        else:
            self.left_out.append(word)
        self._rebuild()
        self.changed.emit()

    def _remove(self, word: str) -> None:
        self.own = [k for k in self.own if k != word]
        self._rebuild()
        self.changed.emit()

    def chip(self, word: str) -> Optional[QToolButton]:
        """The chip's button for a keyword (× or ↺), for the tests to click."""
        return self.findChild(QToolButton, f"chip:{word}")

    def _rebuild(self) -> None:
        focused = self.entry.hasFocus()
        while self._flow.count():
            item = self._flow.takeAt(0)
            if item.widget() is not None and item.widget() is not self.entry:
                item.widget().deleteLater()
        out = {k.lower() for k in self.left_out}
        for word in self.inherited:
            dropped = word.lower() in out
            self._flow.addWidget(self._make_chip(word, "inherited-out" if dropped else "inherited", "↺" if dropped else "×", lambda w=word: self._toggle(w)))
        for word in self.own:
            self._flow.addWidget(self._make_chip(word, "own", "×", lambda w=word: self._remove(w)))
        self._flow.addWidget(self.entry)
        if focused:
            self.entry.setFocus()
        self.updateGeometry()

    def _make_chip(self, word: str, kind: str, mark: str, action: Callable[[], None]) -> QWidget:
        chip = QWidget()
        row = QHBoxLayout(chip)
        row.setContentsMargins(6, 0, 0, 0)
        row.setSpacing(0)
        label = QLabel(word)
        button = QToolButton()
        button.setObjectName(f"chip:{word}")
        button.setText(mark)
        button.setAutoRaise(True)
        button.clicked.connect(lambda _checked=False: action())  # clicked passes `checked`: straight into `action` it would take the keyword's place
        chip.setObjectName("chip")
        chip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)  # a plain QWidget paints no style sheet background otherwise
        if kind == "own":
            chip.setStyleSheet("QWidget#chip { background: palette(highlight); border-radius: 9px; } QLabel, QToolButton { color: palette(highlighted-text); }")
            button.setToolTip(f"Remove {word}")
        elif kind == "inherited":
            chip.setStyleSheet("QWidget#chip { border: 1px solid palette(highlight); border-radius: 9px; }")
            button.setToolTip(f"Leave out {word} on this frame (it is the roll's)")
        else:
            font = label.font()
            font.setStrikeOut(True)
            label.setFont(font)
            muted(label)
            chip.setStyleSheet("QWidget#chip { border: 1px dashed palette(mid); border-radius: 9px; }")
            button.setToolTip(f"Give {word} back to this frame")
        row.addWidget(label)
        row.addWidget(button)
        return chip


def _check(field: QLineEdit, key: str) -> Optional[str]:
    """The field's text as stored, or None (and the field marked) when it is not taken."""
    try:
        value = metadata.clean(key, field.text())
    except ValueError as exc:
        field.setStyleSheet(PROBLEM_STYLE)
        field.setToolTip(str(exc))
        return None
    field.setStyleSheet("")
    field.setToolTip("")
    return value


class RollForm(QWidget):
    """The roll's metadata, on the Roll page. Every field is optional. The scanning rig is shown, not
    edited. A new roll's fields wait in the form until its session starts; a roll's own are saved
    with Save (`save`), which rewrites every TIFF of the roll, so not after every key."""

    changed = Signal()  # a field edited by the operator, not by `fill`
    save = Signal()
    copy = Signal()  # Copy from roll…: the window asks which roll

    def __init__(self) -> None:
        super().__init__()
        self.edits: dict[str, QLineEdit] = {}
        self._recent_boxes: dict[str, QComboBox] = {}
        self.edited = False  # something typed since the last fill
        self._filling = False
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        def line(key: str, placeholder: str = "", width: int = 0) -> QLineEdit:
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            if width:
                edit.setFixedWidth(width)
            edit.textChanged.connect(self._edited)
            self.edits[key] = edit
            return edit

        def recent_box(key: str) -> QComboBox:
            box = QComboBox()
            box.setEditable(True)
            box.lineEdit().setPlaceholderText("as on the body" if key == "camera" else "")
            box.lineEdit().textChanged.connect(self._edited)
            self.edits[key] = box.lineEdit()
            self._recent_boxes[key] = box
            return box

        def group(title: str, where: str) -> QFormLayout:
            column.addSpacing(6)
            heading = QLabel(f"<b>{title}</b>  ·  {where}")
            column.addWidget(heading)
            form = QFormLayout()
            column.addLayout(form)
            return form

        form = group("Shooting", "standard fields")
        form.addRow("Camera", recent_box("camera"))
        form.addRow("Lens", recent_box("lens"))
        days = QHBoxLayout()
        days.addWidget(line("shot_from", "2026-10-03", 120))
        days.addWidget(QLabel("to"))
        days.addWidget(line("shot_to", "", 120))
        days.addStretch(1)
        form.addRow("Date shot", days)

        form = group("Film and processing", "TetherInvert Film")
        form.addRow("Film stock", line("film", "Ilford HP5 Plus"))
        self.format_box = QComboBox()
        self.format_box.addItem("—", "")
        for name in metadata.FORMATS:
            self.format_box.addItem(name, name)
        self.format_box.setToolTip("The film format: how the contact sheet lays the frames out (none: 135)")
        self.format_box.currentIndexChanged.connect(self._edited)
        form.addRow("Format", self.format_box)
        speeds = QHBoxLayout()
        speeds.addWidget(line("box_speed", "400", 64))
        speeds.addWidget(QLabel("Shot at EI"))
        speeds.addWidget(line("ei", "", 64))
        self.push_label = muted(QLabel())
        speeds.addWidget(self.push_label)
        speeds.addStretch(1)
        form.addRow("Box speed", speeds)
        form.addRow("Developer or lab", line("developer"))
        form.addRow("Date developed", line("developed", "", 120))
        for key in ("box_speed", "ei"):
            self.edits[key].textChanged.connect(self._show_push)

        form = group("Rights", "standard fields")
        form.addRow("Author", line("author"))
        form.addRow("Copyright", line("copyright"))

        form = group("Description", "standard fields")
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)  # as wide as the group: at the widest chip's, the entry would wrap
        self.keywords = KeywordBox()
        self.keywords.changed.connect(self._edited)
        form.addRow("Keywords", self.keywords)
        given = muted(QLabel("Given to every frame. A frame can add its own and leave out any of these."))
        given.setWordWrap(True)
        form.addRow("", given)
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(64)
        self.notes.textChanged.connect(self._edited)
        form.addRow("Notes", self.notes)

        self._scanning = group("Scanning", "TetherInvert Scan, added automatically")
        self._scanning_lines: list[tuple[str, str]] = []
        self._scanning.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)  # the group's width: on macOS they would wrap at half of it

        self.problem = QLabel()
        self.problem.setStyleSheet("color: #d9534f;")
        self.problem.setWordWrap(True)
        self.problem.hide()
        column.addWidget(self.problem)
        buttons = QHBoxLayout()
        self.copy_button = QPushButton("Copy from roll…")
        self.copy_button.setToolTip("Takes another roll's fields (not its frames' ones)")
        self.copy_button.clicked.connect(lambda _checked=False: self.copy.emit())
        self.footer = muted(QLabel())
        self.footer.setWordWrap(True)
        self.save_button = QPushButton("Save metadata")
        self.save_button.clicked.connect(lambda _checked=False: self.save.emit())
        buttons.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        buttons.addWidget(self.footer, 1)
        buttons.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(buttons)
        self.set_files(None)

    def set_recent(self, recent: dict[str, list[str]]) -> None:
        """The cameras and lenses typed before, offered in their boxes; what is in the box stays."""
        filling, self._filling = self._filling, True  # the text is put back: not an edit
        for key, box in self._recent_boxes.items():
            text = box.currentText()
            box.clear()
            box.addItems(recent.get(key, []))
            box.setCurrentText(text)
        self._filling = filling

    def set_scanning(self, lines: list[tuple[str, str]]) -> None:
        if lines == self._scanning_lines:
            return
        self._scanning_lines = lines
        while self._scanning.rowCount():
            self._scanning.removeRow(0)
        for label, value in lines:
            name, shown = QLabel(label), QLabel(value)
            shown.setWordWrap(True)
            self._scanning.addRow(name, shown)
            for widget in (name, shown):
                widget.show()  # at once, as the page is laid out: Qt would show them a turn of the event loop later

    def set_files(self, files: Optional[int]) -> None:
        """How many files Save rewrites; None for a roll not started yet, which has nothing to save."""
        if files is None:
            text = "Kept until the session starts: the roll is written then, and its scans carry it."
        elif files:
            text = f"Saving updates {files} file{'s' if files != 1 else ''}. Pixels are left untouched."
        else:
            text = "No files yet: the scans will carry it."
        self.footer.setText(text)
        self.save_button.setVisible(files is not None)

    def fill(self, fields: dict) -> None:
        self._filling = True
        for key, edit in self.edits.items():
            edit.setText(str(fields.get(key, "")))
            edit.setStyleSheet("")
            edit.setToolTip("")
        self.keywords.set_keywords(fields.get("keywords", []))
        self.keywords.entry.clear()
        self.notes.setPlainText(fields.get("notes", ""))
        self.format_box.setCurrentIndex(max(0, self.format_box.findData(fields.get("format", ""))))
        self.problem.hide()
        self._show_push()
        self._filling = False
        self.edited = False

    def _edited(self, *_args) -> None:
        if not self._filling:
            self.edited = True
            self.changed.emit()

    def _show_push(self) -> None:
        self.push_label.setText(metadata.push(self.edits["box_speed"].text().strip(), self.edits["ei"].text().strip()))

    def copy_from(self, found: dict) -> None:
        """Another roll's fields, as edits of this one: Save (or the session's start) keeps them."""
        self.fill(found)
        self.edited = True
        self.changed.emit()

    def values(self) -> Optional[dict]:
        """The fields as stored, or None when one is not taken (it is marked, the problem shown)."""
        fields, problems = {}, []
        for key, edit in self.edits.items():
            value = _check(edit, key)
            if value is None:
                problems.append(edit.toolTip())
            elif value:
                fields[key] = value
        if self.format_box.currentData():
            fields["format"] = self.format_box.currentData()
        words = self.keywords.keywords()
        if words:
            fields["keywords"] = words
        if self.notes.toPlainText().strip():
            fields["notes"] = self.notes.toPlainText().strip()
        self.problem.setText("\n".join(problems))
        self.problem.setVisible(bool(problems))
        return None if problems else fields


class FrameRow(QWidget):
    """The frame's metadata above the frame view: one line, opened by a click or L. `changed`
    goes out with every edit; the window saves soon after the last one."""

    changed = Signal()
    toggled = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._roll: dict = {}
        self._loading = False
        self.header = QToolButton()
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.header.setAutoRaise(True)
        self.header.setStyleSheet("font-weight: bold;")
        self.header.clicked.connect(lambda: self.set_open(not self.is_open()))
        self.summary = QLabel()
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # cut short rather than widen the window
        self.hint = muted(QLabel("click or L to open"))
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.header)
        top.addWidget(self.summary, 100)
        top.addWidget(self.hint)
        top.addStretch(1)  # keeps the heading on the left while the summary is hidden
        self.edits: dict[str, QLineEdit] = {}
        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        for key, label, width, placeholder in (("aperture", "Aperture", 60, "—"), ("shutter", "Shutter", 70, "—"), ("date", "Date", 150, ""), ("note", "Note", 0, "")):
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            if width:
                edit.setFixedWidth(width)
            edit.textEdited.connect(self._edited)
            self.edits[key] = edit
            fields.addWidget(QLabel(label))
            fields.addWidget(edit, 0 if width else 1)
        self.keywords = KeywordBox()
        self.keywords.changed.connect(self._edited)
        self.keywords.entry.textEdited.connect(self._edited)
        words = QHBoxLayout()
        words.setContentsMargins(0, 0, 0, 0)
        words.addWidget(QLabel("Keywords"))
        words.addWidget(self.keywords, 1)
        self.body = QWidget()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(16, 0, 0, 4)
        body.addLayout(fields)
        body.addLayout(words)
        legend = QLabel("Outlined: from the roll, × leaves it out on this frame only. Filled: this frame's own. Fields left empty are not written.")
        muted(legend)
        body.addWidget(legend)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addLayout(top)
        column.addWidget(self.body)
        self.number: Optional[int] = None
        self.set_open(False)

    def is_open(self) -> bool:
        return not self.body.isHidden()

    def set_open(self, on: bool) -> None:
        if on == self.is_open() and self.header.text():
            return
        self.body.setVisible(on)
        self.hint.setVisible(not on)
        self.summary.setVisible(not on)
        self.header.setText(("▾ " if on else "▸ ") + "Frame metadata")
        self.toggled.emit(on)

    def show_frame(self, number: int, roll: dict, fields: Optional[dict]) -> None:
        """Load a frame's fields; the roll's give the date's placeholder and the outlined keywords."""
        self._loading = True
        self.number, self._roll, fields = number, roll, fields or {}
        for key, edit in self.edits.items():
            edit.setText(fields.get(key, ""))
            edit.setStyleSheet("")
            edit.setToolTip("")
        shot = roll.get("shot_from", "")
        self.edits["date"].setPlaceholderText(f"{shot} (roll)" if shot else "2026-10-03")
        self.keywords.entry.clear()
        self.keywords.set_keywords(fields.get("keywords", []), roll.get("keywords", []), fields.get("keywords_left_out", []))
        self._loading = False
        self._show_summary()

    def values(self, before: Optional[dict]) -> dict:
        """The frame's fields as stored. A field whose text is not taken keeps what it had
        (`before`) and stays marked, so the others are not held up by it."""
        before, fields = before or {}, {}
        for key, edit in self.edits.items():
            value = _check(edit, key)
            value = before.get(key, "") if value is None else value
            if value:
                fields[key] = value
        for key, value in (("keywords", self.keywords.keywords()), ("keywords_left_out", self.keywords.left_out_keywords())):
            if value:
                fields[key] = value
        return fields

    def problems(self) -> list[str]:
        return [edit.toolTip() for edit in self.edits.values() if edit.toolTip()]

    def _edited(self, *_args) -> None:
        if not self._loading:
            self._show_summary()
            self.changed.emit()

    def _show_summary(self) -> None:
        current = {key: edit.text().strip() for key, edit in self.edits.items() if edit.text().strip()}
        current |= {"keywords": self.keywords.keywords(), "keywords_left_out": self.keywords.left_out_keywords()}
        self.summary.setText(metadata.frame_summary(self._roll, current))
