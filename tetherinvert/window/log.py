"""The window's log: the status bar under everything says what just happened; its Log button opens
every line before it in a panel laid over the bottom of the page.

Each line is an `Entry` with a kind: information, a file written (a tick) or a warning (a red row).
The kind is known where the line is said, not read from its words: every line of the render thread's
`done` is files written (`_on_written`), every `_on_failed` a warning, the rest information. A
reworded message keeps its kind.

A warning turns the status line red for `WARNING_S` (until a line that is not a warning replaces it:
red under another line would say that one went wrong) and leaves a red count on Log, the warnings not
seen yet, until the panel is opened. A warning that comes while the panel is open is seen there.

The panel lies over the page instead of taking height from it: docked above the status bar it would
squeeze the Develop column at the window's minimum, and the minimum must stay the same on every step. It
stays open while working (Space still scans, Esc still opens Scan) until Log or × closes it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QByteArray, QEvent, QObject, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument, QTextImageFormat, QTextOption
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget

from . import style

LINES = 1000  # kept in the log: a roll of 15 frames with its prints and sheet makes about 40 lines
WARNING_S = 8  # the status line stays red this long after a warning
PANEL_HEIGHT = 300  # px; never more than half the page, so the picture's top and the column's stay in view
COPIED = {"info": " ", "file": "✓", "warning": "!"}  # a line's kind in copied text: the panel's marks as characters
MARK_SIZE = 13  # px
TIME_COLUMN, TEXT_COLUMN = 70, 92  # px from the left: the mark's column and the text's, so wrapped lines stay under the text
MARKS = {  # a tick for a file written, a triangle for a warning, a small dot for the rest; {c} is the colour
    "file": '<path d="M2.5 6l2.5 2.5 4.5-5" stroke="{c}" stroke-width="1.9" fill="none"/>',
    "warning": '<path d="M6 1.5l5 9H1z" stroke="{c}" stroke-width="1.6" fill="none"/><path d="M6 5v2.4M6 9v.1" stroke="{c}" stroke-width="1.6" fill="none"/>',
    "info": '<circle cx="6" cy="7" r="2.5" fill="{c}"/>',
}


@dataclass(frozen=True)
class Entry:
    when: float  # time.time()
    kind: str  # info, file or warning
    text: str

    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.when))

    def copied(self) -> str:
        return f"{self.clock()} {COPIED[self.kind]} {self.text}"


def mark_image(kind: str, colour: str) -> QImage:
    """A line's mark, drawn at twice its size for a Retina screen."""
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12">{MARKS[kind].format(c=colour)}</svg>'
    image = QImage(MARK_SIZE * 2, MARK_SIZE * 2, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, image.width(), image.height()))
    painter.end()
    image.setDevicePixelRatio(2)
    return image


class CountButton(QPushButton):
    """A button with a count beside its name: grey (role value), red for warnings (role count), or none.
    Laid out inside, so it is as big as its layout (a button's own size hint knows only its text)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.setAccessibleName(name)
        self.name = QLabel(name)
        self.count = QLabel()
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count.setMinimumSize(18, 18)  # a red count is a pill around its digits, not a box round them
        self.count.hide()
        row = QHBoxLayout(self)
        row.setContentsMargins(11, 4, 11, 4)
        row.setSpacing(6)
        row.addWidget(self.name)
        row.addWidget(self.count)

    def set_count(self, number: Optional[int], alert: bool = False) -> None:
        """None hides the count."""
        if number is None:
            self.count.hide()
        else:
            self.count.setText(str(number))
            style.set_role(self.count, "count" if alert else "value")
            self.count.show()
        self.updateGeometry()  # the size hint is the layout's: the bar lays the button out again

    def sizeHint(self):
        return self.layout().sizeHint()

    def minimumSizeHint(self):
        return self.layout().minimumSize()


class LogView(QTextEdit):
    """The lines, one paragraph each: the time, the mark, the text, wrapped under the text. Read-only, the
    text selectable (and ⌘C); the window's keys still reach the window, since only
    editable fields keep them (`_typing`). The colours are the document's own: written again when the
    appearance changes."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)  # a path has no spaces to wrap at
        self.document().setDocumentMargin(8)
        self.entries: list[Entry] = []  # the lines shown, in the document's order
        self._appearance = ""
        self._marks()

    def show_entries(self, entries: list[Entry]) -> None:
        self.entries = []
        self.clear()
        self._marks()
        for entry in entries:
            self.append_entry(entry)

    def append_entry(self, entry: Entry) -> None:
        t = style.tokens()
        bar = self.verticalScrollBar()
        following = bar.value() >= bar.maximum() - 4  # at the end: stay there; scrolled up to read: stay put
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        block = QTextBlockFormat()
        block.setLeftMargin(TEXT_COLUMN)
        block.setTextIndent(-TEXT_COLUMN)  # the first line from the left edge, the rest under the text
        block.setTabPositions([QTextOption.Tab(TIME_COLUMN, QTextOption.TabType.LeftTab), QTextOption.Tab(TEXT_COLUMN, QTextOption.TabType.LeftTab)])
        block.setTopMargin(2)
        block.setBottomMargin(2)
        if entry.kind == "warning":
            block.setBackground(QColor(t.danger_ground))
        if self.entries:
            cursor.insertBlock(block)
        else:
            cursor.setBlockFormat(block)
        clock = QTextCharFormat()
        clock.setFontFamilies([style.mono_family()])
        clock.setForeground(QColor(t.muted))
        cursor.insertText(entry.clock() + "\t", clock)
        mark = QTextImageFormat()
        mark.setName(f"mark:{entry.kind}")
        mark.setWidth(MARK_SIZE)
        mark.setHeight(MARK_SIZE)
        mark.setVerticalAlignment(QTextCharFormat.VerticalAlignment.AlignMiddle)
        cursor.insertImage(mark)
        words = QTextCharFormat()
        words.setForeground(QColor(t.danger if entry.kind == "warning" else t.text))
        words.setFontWeight(QFont.Weight.DemiBold if entry.kind == "warning" else QFont.Weight.Normal)
        cursor.insertText("\t" + entry.text, words)
        self.entries.append(entry)
        if following:
            bar.setValue(bar.maximum())

    def drop_first(self) -> None:
        """The oldest line shown, gone from the log."""
        if not self.entries:
            return
        self.entries.pop(0)
        cursor = QTextCursor(self.document().firstBlock())
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor)  # with the paragraph break
        cursor.removeSelectedText()

    def rows(self) -> list[tuple[str, str, str]]:
        """What is shown, read back from the document: each line's mark, its ground ("" for none) and text."""
        shown, block = [], self.document().firstBlock()
        while block.isValid():
            mark, fragments = "", block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.charFormat().isImageFormat():
                    mark = fragment.charFormat().toImageFormat().name().removeprefix("mark:")
                fragments += 1
            ground = block.blockFormat().background()
            shown.append((mark, ground.color().name() if ground.style() != Qt.BrushStyle.NoBrush else "", block.text().split("\t")[-1]))
            block = block.next()
        return shown if self.entries else []

    def _marks(self) -> None:
        t = style.tokens()
        for kind, colour in (("file", t.ok), ("warning", t.danger), ("info", t.muted)):
            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(f"mark:{kind}"), mark_image(kind, colour))
        self._appearance = t.text

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and self._appearance and self._appearance != style.tokens().text:
            self.show_entries(list(self.entries))  # the appearance changed: the colours again


class LogPanel(QFrame):
    """Every line kept (`entries`, at most `LINES`): All or Warnings with their counts, since when, Copy
    and ×. Laid over the bottom of a widget (`lay_over`), not in a layout: the window's minimum stays."""

    def __init__(self) -> None:
        super().__init__()
        style.set_role(self, "logpanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.entries: deque[Entry] = deque(maxlen=LINES)
        self.opened = time.time()  # the window's start: the log is the window's, sessions are lines in it
        self.dropped = False  # older lines than the first kept were dropped
        self.filter = "all"
        self._over: Optional[QWidget] = None
        self.all_button, self.warnings_button = CountButton("All"), CountButton("Warnings")
        switch = QFrame()
        style.set_role(switch, "switch")
        pair = QHBoxLayout(switch)
        pair.setContentsMargins(3, 3, 3, 3)
        pair.setSpacing(2)
        group = QButtonGroup(self)
        for key, button in (("all", self.all_button), ("warning", self.warnings_button)):
            button.setCheckable(True)
            style.set_role(button, "segment")
            group.addButton(button)
            pair.addWidget(button)
            button.clicked.connect(lambda _checked=False, k=key: self.show_only(k))
        self.all_button.setChecked(True)
        self.since = style.label("", "muted", wrap=False)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("The lines shown, with their times, onto the clipboard")
        self.copy_button.clicked.connect(self.copy)
        self.close_button = QPushButton("×")
        self.close_button.setFlat(True)
        self.close_button.setAccessibleName("Close the log")
        self.close_button.setToolTip("Close the log")
        self.view = LogView()
        head = QHBoxLayout()
        head.setContentsMargins(20, 8, 10, 8)
        head.setSpacing(14)
        head.addWidget(style.label("Log", "heading", wrap=False))
        head.addWidget(switch)
        head.addWidget(self.since, 1)
        head.addWidget(self.copy_button)
        head.addWidget(self.close_button)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addLayout(head)
        divider = style.set_role(QFrame(), "divider")
        divider.setFixedHeight(1)
        column.addWidget(divider)
        column.addWidget(self.view, 1)
        self._show_counts()
        self.hide()

    def add(self, entry: Entry) -> None:
        if len(self.entries) == LINES:
            self.dropped = True
            if self._shown(self.entries[0]):
                self.view.drop_first()
        self.entries.append(entry)
        if self._shown(entry):
            self.view.append_entry(entry)
        self._show_counts()

    def show_only(self, kind: str) -> None:
        """all, or warning."""
        self.filter = kind
        (self.all_button if kind == "all" else self.warnings_button).setChecked(True)
        self.view.show_entries([entry for entry in self.entries if self._shown(entry)])

    def copy(self) -> None:
        QApplication.clipboard().setText("\n".join(entry.copied() for entry in self.entries if self._shown(entry)))

    def _shown(self, entry: Entry) -> bool:
        return self.filter == "all" or entry.kind == self.filter

    def _show_counts(self) -> None:
        warnings = sum(entry.kind == "warning" for entry in self.entries)
        self.all_button.set_count(len(self.entries))
        self.warnings_button.set_count(warnings, alert=warnings > 0)
        if self.dropped:
            self.since.setText(f"The last {LINES} lines, since {time.strftime('%H:%M', time.localtime(self.entries[0].when))}")
        else:
            self.since.setText(f"Since {time.strftime('%H:%M', time.localtime(self.opened))}, when the window opened")

    # ----- over the page -----

    def lay_over(self, over: QWidget) -> None:
        """Over the bottom of `over`, as wide as it, from its parent: in no layout."""
        self._over = over
        self.setParent(over.parentWidget())
        self.hide()
        over.installEventFilter(self)

    def open(self) -> None:
        self._place()
        self.show()
        self.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._over and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._place()
        return False

    def _place(self) -> None:
        if self._over is None:
            return
        page = self._over.geometry()
        height = min(PANEL_HEIGHT, page.height() // 2)
        self.setGeometry(page.x(), page.y() + page.height() - height, page.width(), height)


class LogBar(QFrame):
    """The last line with its time, and Log, which opens the panel (`panel`) and carries the count of
    warnings not seen yet. `text()`: every line kept, "HH:MM:SS  text", whatever the panel shows."""

    def __init__(self) -> None:
        super().__init__()
        style.set_role(self, "statusbar")
        self.time = style.label("", "value", wrap=False)
        self.status = style.label("", wrap=False)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # a long message is cut short, never widens the window
        self.log_button = CountButton("Log")
        self.log_button.setCheckable(True)
        self.log_button.setToolTip("Every message since the window was opened")
        self.log_button.clicked.connect(lambda _checked=False: self.toggle())
        self.unseen = 0  # warnings since the panel was last open
        self.panel = LogPanel()
        self.panel.close_button.clicked.connect(self.close_log)
        self._red = QTimer(self)
        self._red.setSingleShot(True)
        self._red.setInterval(WARNING_S * 1000)
        self._red.timeout.connect(lambda: self._set_red(False))
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 4, 10, 4)
        row.setSpacing(10)
        row.addWidget(self.time)
        row.addWidget(self.status, 1)
        row.addWidget(self.log_button)

    def add(self, text: str, kind: str = "info") -> None:
        entry = Entry(time.time(), kind, text)
        self.time.setText(entry.clock())
        self.status.setText(text)
        self.status.setToolTip(text)
        self._set_red(kind == "warning")
        if kind == "warning":
            self._red.start()
            if self.panel.isHidden():
                self.unseen += 1
        self.panel.add(entry)
        self._show_unseen()

    def text(self) -> str:
        return "\n".join(f"{entry.clock()}  {entry.text}" for entry in self.panel.entries)

    def toggle(self) -> None:
        if not self.panel.isHidden():
            self.close_log()
        else:
            self.open_log()

    def open_log(self) -> None:
        self.panel.open()
        self.unseen = 0
        self._show_unseen()

    def close_log(self) -> None:
        self.panel.hide()
        self._show_unseen()

    def _show_unseen(self) -> None:
        self.log_button.set_count(self.unseen or None, alert=True)
        self.log_button.setChecked(not self.panel.isHidden())

    def _set_red(self, red: bool) -> None:
        if not red:
            self._red.stop()
        for label in (self.time, self.status):
            style.set_tone(label, "danger" if red else "")


class LogPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _say(self, text: str, kind: str = "info") -> None:
        """A line for the operator: info, file (a file written) or warning."""
        self.log_bar.add(text, kind)

    def _on_written(self, text: str) -> None:
        """The render thread's `done`: every job of it that ends well has written files."""
        self._say(text, "file")

    def _on_failed(self, text: str) -> None:
        self._say(text, "warning")
