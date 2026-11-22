"""The window's look: colour tokens for a light and a dark appearance, and the application's palette
and stylesheet made from them. The window follows the system's appearance, also when it changes
while the window is open.

Warm grey chrome, white panels, one accent: the logo's amber, darkened to #B4531C so white text on
it can be read. Green and red say whether something is connected or went wrong. The picture's
surround is the same neutral grey in both appearances: a warm or a dark surround changes how the
colours of the picture judged on it look. Text is in Instrument Sans, numbers in IBM Plex Mono, both
shipped in fonts/ with their licences (SIL OFL 1.1).

The widgets ask for a look by a property, not by a colour: `role` (a panel, a title, a muted line,
the primary button) and `tone` (ok, danger, warning), so a change of appearance restyles them all.
Widgets that paint themselves read `tokens()` when they paint.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .scrolling import Column, Scroll

SURROUND = "#2b2b2b"  # around the picture, in both appearances: neutral, so it tints nothing judged on it
FONTS = Path(__file__).parent / "fonts"  # shipped with TetherInvert, their licences (SIL OFL 1.1) beside them
TEXT_FAMILY, NUMBER_FAMILY = "Instrument Sans", "IBM Plex Mono"  # text and numbers
PAGE_WIDTH = 820  # px: a full page's content at most, centred; lines longer than that are hard to read
SIDE_WIDTH = 350  # px, the column beside the picture on Scan, Develop and Presets: the same for all three, so the window's minimum stays put
COLUMN_MARGIN = 20  # px around a column's content


@dataclass(frozen=True)
class Tokens:
    chrome: str  # the window's ground
    rail: str  # the rail of steps
    rail_line: str
    panel: str  # panels, side columns, fields
    line: str  # a panel's border
    divider: str  # lines inside a panel; a pressed button
    control: str  # the border of buttons and fields
    text: str
    text2: str  # secondary text
    muted: str  # hints, disabled text
    accent: str  # the primary button, the current step
    accent_hover: str
    on_accent: str  # text on the accent
    accent_text: str  # the accent as text or a line on the ground
    badge: str  # a count on the rail that is not a call to act
    ok: str  # connected, done
    ok_ground: str  # under ok text: a chip that says all is well
    danger: str  # not connected, failed, deleting
    danger_ground: str
    danger_line: str
    alert: str  # under on_accent: a red count, warnings not seen yet
    warning: str  # a note to heed, not an error
    warning_ground: str


LIGHT = Tokens(
    chrome="#f3f3f0", rail="#e9e9e4", rail_line="#dadad4", panel="#ffffff", line="#deded8", divider="#ecece7", control="#cfcfc8",
    text="#17171a", text2="#3a3a40", muted="#5a5a61", accent="#b4531c", accent_hover="#9c4716", on_accent="#ffffff", accent_text="#a84d1a",
    badge="#c9c9c2", ok="#2e7d4a", ok_ground="#eef7f1", danger="#b42318", danger_ground="#fbe9e7", danger_line="#e8c4bf", alert="#b42318",
    warning="#8a3b00", warning_ground="#fbebdd",
)  # fmt: skip
DARK = Tokens(
    chrome="#161618", rail="#111113", rail_line="#2a2a2d", panel="#1c1c1f", line="#2e2e32", divider="#2a2a2e", control="#3e3e44",
    text="#ececee", text2="#b8b8c0", muted="#a0a0a8", accent="#b4531c", accent_hover="#c4622a", on_accent="#ffffff", accent_text="#ce8f6b",
    badge="#3a3a3f", ok="#4cc27a", ok_ground="#1d3326", danger="#f2786b", danger_ground="#3a201d", danger_line="#5c302c", alert="#c0392b",
    warning="#f0b27a", warning_ground="#3a2a1e",
)  # fmt: skip


def dark() -> bool:
    """Whether the system's appearance is dark. Offscreen (the tests) it is unknown: light."""
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def tokens() -> Tokens:
    return DARK if dark() else LIGHT


def luminance(colour: str) -> float:
    """Relative luminance, as WCAG defines it."""
    channels = [QColor(colour).redF(), QColor(colour).greenF(), QColor(colour).blueF()]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(one: str, other: str) -> float:
    """WCAG contrast ratio of two colours, 1 to 21."""
    high, low = sorted((luminance(one), luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def load_fonts() -> set[str]:
    """The families of the fonts shipped with TetherInvert, made known to Qt. A file that will not load
    leaves the system's font in its place: the window works without them."""
    families: set[str] = set()
    for path in sorted(FONTS.glob("*.ttf")):
        loaded = QFontDatabase.addApplicationFont(str(path))
        if loaded >= 0:
            families.update(QFontDatabase.applicationFontFamilies(loaded))
    return families


def mono_family() -> str:
    """For numbers that are read and compared: frame numbers, exposure, times in the log."""
    if NUMBER_FAMILY in QFontDatabase.families():
        return NUMBER_FAMILY
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


def palette(t: Tokens) -> QPalette:
    """For what the stylesheet leaves to the style: check boxes, combo boxes, lists, menus, scroll bars."""
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.chrome, QPalette.ColorRole.WindowText: t.text, QPalette.ColorRole.Base: t.panel,
        QPalette.ColorRole.AlternateBase: t.divider, QPalette.ColorRole.Text: t.text, QPalette.ColorRole.Button: t.panel,
        QPalette.ColorRole.ButtonText: t.text, QPalette.ColorRole.Highlight: t.accent, QPalette.ColorRole.HighlightedText: t.on_accent,
        QPalette.ColorRole.PlaceholderText: t.muted, QPalette.ColorRole.ToolTipBase: t.panel, QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.Link: t.accent_text, QPalette.ColorRole.Mid: t.control, QPalette.ColorRole.Midlight: t.divider,
        QPalette.ColorRole.Dark: t.control, QPalette.ColorRole.Light: t.panel, QPalette.ColorRole.Shadow: t.line,
    }  # fmt: skip
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t.muted))
    return p


MARKS = {  # drawn on the accent in check boxes and radio buttons: white in both appearances
    "tick": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14"><path d="M3 7.2l2.6 2.6L11 4.4" fill="none" stroke="#fff" stroke-width="2"/></svg>',
    "dot": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14"><circle cx="7" cy="7" r="3" fill="#fff"/></svg>',
}


def mark(name: str) -> str:
    """The file of a mark, for the stylesheet: it takes pictures only from files. Written once."""
    path = Path(tempfile.gettempdir()) / f"tetherinvert-{name}.svg"
    if not path.exists() or path.read_text() != MARKS[name]:
        path.write_text(MARKS[name])
    return path.as_posix()


def stylesheet(t: Tokens) -> str:
    return f"""
    QToolTip {{ background: {t.panel}; color: {t.text}; border: 1px solid {t.line}; padding: 4px; }}

    QFrame[role="panel"] {{ background: {t.panel}; border: 1px solid {t.line}; border-radius: 12px; }}
    QFrame[role="panel"][tone="accent"] {{ border: 2px solid {t.accent}; }}
    QFrame[role="panel"] QLabel {{ background: transparent; }}
    QWidget[role="column"] {{ background: {t.panel}; border-left: 1px solid {t.line}; }}
    QFrame[role="statusbar"] {{ background: {t.rail}; border-top: 1px solid {t.rail_line}; }}
    QFrame[role="statusbar"] QLabel {{ background: transparent; }}
    QFrame[role="statusbar"] QPushButton:checked {{ background: {t.panel}; border: 2px solid {t.accent}; }}
    QFrame[role="logpanel"] {{ background: {t.panel}; border-top: 1px solid {t.control}; }}
    QFrame[role="logpanel"] QTextEdit {{ background: {t.panel}; border: none; }}
    QPushButton QLabel {{ background: transparent; }}
    QWidget[role="header"] {{ background: {t.panel}; border-bottom: 1px solid {t.line}; }}
    QWidget[role="header"] QLabel {{ background: transparent; font-size: 16px; }}

    QLabel[role="title"] {{ font-size: 24px; font-weight: 700; }}
    QLabel[role="heading"] {{ font-weight: 700; }}
    QLabel[role="muted"] {{ color: {t.muted}; }}
    QLabel[role="number"] {{ font-family: "{mono_family()}"; font-size: 30px; font-weight: 600; }}
    QLabel[role="keys"] {{ font-family: "{mono_family()}"; font-size: 11px; color: {t.muted}; }}
    QLabel[role="value"] {{ font-family: "{mono_family()}"; color: {t.muted}; }}
    QLabel[role="value"][tone="accent"] {{ color: {t.accent_text}; font-weight: 700; }}
    QLabel[role="keys"]:disabled, QLabel[role="value"]:disabled {{ color: {t.control}; }}
    QFrame[role="divider"] {{ background: {t.divider}; border: none; }}
    QListWidget[role="strip"] {{ background: {SURROUND}; border: none; border-radius: 8px; }}
    QFrame[role="switch"] {{ background: {t.divider}; border: none; border-radius: 9px; }}
    QPushButton[role="segment"] {{ background: transparent; color: {t.text2}; border: none; border-radius: 7px; padding: 6px 0; }}
    QPushButton[role="segment"]:checked {{ background: {t.panel}; color: {t.text}; font-weight: 600; }}
    QPushButton[role="segment"]:disabled {{ color: {t.muted}; background: transparent; }}
    QLabel[tone="ok"] {{ color: {t.ok}; }}
    QLabel[tone="danger"] {{ color: {t.danger}; }}
    QLabel[tone="warning"] {{ color: {t.warning}; }}

    QPushButton, QToolButton {{
        background: {t.panel}; color: {t.text}; border: 1px solid {t.control}; border-radius: 8px; padding: 5px 12px;
    }}
    QToolButton {{ padding: 3px 7px; }}
    QPushButton:hover, QToolButton:hover {{ border-color: {t.muted}; }}
    QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked {{ background: {t.divider}; border-color: {t.text2}; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {t.muted}; background: {t.chrome}; border-color: {t.divider}; }}
    QPushButton:flat {{ border: none; background: transparent; }}
    QPushButton[role="primary"] {{ background: {t.accent}; color: {t.on_accent}; border: 1px solid {t.accent}; font-weight: 600; }}
    QPushButton[role="primary"]:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
    QPushButton[role="primary"]:disabled {{ background: {t.divider}; color: {t.muted}; border-color: {t.divider}; }}
    QPushButton[role="danger"] {{ color: {t.danger}; border-color: {t.danger_line}; }}
    QPushButton[role="danger"]:disabled {{ color: {t.muted}; border-color: {t.divider}; }}
    QPushButton[role="choice"] {{ background: transparent; border: 2px solid transparent; border-radius: 10px; padding: 0; text-align: left; }}
    QPushButton[role="choice"]:hover {{ background: {t.chrome}; border-color: transparent; }}
    QPushButton[role="choice"]:checked {{ background: {t.chrome}; border-color: {t.accent}; }}
    QPushButton[role="choice"]:checked:disabled {{ border-color: {t.control}; }}
    QPushButton[role="choice"] QLabel {{ background: transparent; }}
    QLabel[role="chip"] {{ background: {t.warning_ground}; color: {t.warning}; border-radius: 8px; padding: 1px 8px; font-size: 11px; font-weight: 600; }}
    QLabel[role="chip"][tone="ok"] {{ background: {t.ok_ground}; color: {t.ok}; }}
    QLabel[role="chip"][tone="danger"] {{ background: {t.danger_ground}; color: {t.danger}; }}
    QLabel[role="chip"][tone="off"] {{ background: {t.divider}; color: {t.muted}; }}
    QLabel[role="data"] {{ font-family: "{mono_family()}"; }}
    QLabel[role="count"], QWidget QLabel[role="count"] {{  /* the second as specific as "statusbar QLabel": the pill keeps its ground in a bar */
        background: {t.alert}; color: {t.on_accent}; border-radius: 8px; padding: 0 5px; font-family: "{mono_family()}"; font-size: 11px; font-weight: 600;
    }}

    QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {t.control}; background: {t.panel}; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t.muted}; }}
    QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url({mark("tick")}); }}
    QRadioButton::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url({mark("dot")}); }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background: {t.chrome}; border-color: {t.divider}; }}
    QCheckBox::indicator:checked:disabled, QRadioButton::indicator:checked:disabled {{ background: {t.divider}; }}

    QLineEdit, QPlainTextEdit {{
        background: {t.panel}; color: {t.text}; border: 1px solid {t.control}; border-radius: 6px; padding: 3px 6px;
        selection-background-color: {t.accent}; selection-color: {t.on_accent};
    }}
    QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {t.accent_text}; }}
    QLineEdit:disabled {{ background: {t.chrome}; color: {t.muted}; }}
    """


def apply(app: QApplication) -> None:
    """The look for the whole application, from now on following the system's appearance. Called
    by every window; the second time it only sets the look again."""
    app.setStyle("Fusion")  # draws by the palette and the stylesheet on every system; the macOS style ignores much of both
    if not app.property("tetherinvert_follows_appearance"):
        app.setProperty("tetherinvert_follows_appearance", True)
        if TEXT_FAMILY in load_fonts():
            font = QFont(app.font())  # the system's size, in Instrument Sans
            font.setFamily(TEXT_FAMILY)
            app.setFont(font)
        app.styleHints().colorSchemeChanged.connect(lambda _scheme: _restyle(app))  # the GUI thread's own signal: a lambda is fine
    _restyle(app)


def _restyle(app: QApplication) -> None:
    t = tokens()
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t))
    for widget in app.allWidgets():
        widget.update()  # the widgets that paint themselves read the tokens again


def set_role(widget: QWidget, role: str) -> QWidget:
    """A look by what the widget is (see `stylesheet`): panel, column, title, heading, muted, number, keys, value, data, chip, count, primary, danger,
    choice, switch, segment, divider, strip, statusbar, logpanel."""
    widget.setProperty("role", role)
    _repolish(widget)
    return widget


def set_tone(widget: QWidget, tone: str) -> None:
    """ok, danger, warning, accent (a changed value; a panel's border), off (a chip of something not in use), or "" for the text colour."""
    if widget.property("tone") != tone:
        widget.setProperty("tone", tone)
        _repolish(widget)


def _repolish(widget: QWidget) -> None:
    """A changed property is styled only once the widget is polished again."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)



# ----- building blocks of the pages -----


def label(text: str = "", role: str = "", wrap: bool = True) -> QLabel:
    """A label with a look (`set_role`); wrapped unless said otherwise."""
    made = QLabel(text)
    made.setWordWrap(wrap)
    if role:
        set_role(made, role)
    return made


def panel(heading: str = "") -> tuple[QFrame, QVBoxLayout]:
    """A white panel with a heading, and the column to put its content in."""
    frame = QFrame()
    set_role(frame, "panel")
    column = QVBoxLayout(frame)
    column.setContentsMargins(18, 14, 18, 16)
    column.setSpacing(8)
    if heading:
        column.addWidget(label(heading, "heading"))
    return frame, column


def full_page(subtitle: str, width: int = PAGE_WIDTH, tool: QWidget | None = None) -> tuple[Scroll, QVBoxLayout]:
    """A page of its own (Roll, Finish, Presets): what it is for (its title is the header's) with the
    page's own `tool` at its right, then panels in a column at most `width` wide, centred, which
    scrolls on a short screen."""
    content = QWidget()
    content.setMaximumWidth(width)
    column = QVBoxLayout(content)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(14)
    top = QHBoxLayout()
    top.addWidget(label(subtitle, "muted"), 1)
    if tool is not None:
        top.addWidget(tool, 0, Qt.AlignmentFlag.AlignBottom)
    column.addLayout(top)
    column.addSpacing(4)
    outer = Column()
    row = QHBoxLayout(outer)
    row.setContentsMargins(36, 28, 36, 36)
    row.addStretch(1)
    row.addWidget(content, 100)
    row.addStretch(1)
    return Scroll(outer), column
