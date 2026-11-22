"""A column of widgets that scrolls on a screen too short for it: the columns beside the picture, the
Roll and Finish pages. Squeezed instead, their rows would lie on top of each other, fields a few pixels high.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QStyle, QWidget


class Column(QWidget):
    """The widgets of a Scroll. Its minimum height is the one at its width: the plain one counts
    wrapped labels at the width they would like, fewer lines than they get, and the scroll area would give
    the column that much less, squeezing its fields."""

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(hint.width(), max(hint.height(), self.layout().totalMinimumHeightForWidth(self.width())))


class Scroll(QScrollArea):
    """Scrolls a Column up and down, never sideways. It asks for the column's whole height, so a
    window that has the room shows it all; QScrollArea's own size hint is a few lines."""

    def __init__(self, column: Column) -> None:
        super().__init__()
        self.setWidget(column)
        self.setWidgetResizable(True)  # with room to spare the column takes it
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def bar_width(self) -> int:
        """Kept free beside the column, so a scroll bar does not narrow it."""
        return self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)

    def sizeHint(self) -> QSize:
        column = self.widget()
        width = column.sizeHint().width()
        return QSize(width + self.bar_width(), column.heightForWidth(width) if column.layout().hasHeightForWidth() else column.sizeHint().height())
