"""The Finish step: the roll's prints, written in a batch (Develop's corrections, development.py); the
contact sheet (contact.py), every page of it shown as it will be written, and written on the render
thread and opened when done; the roll's metadata, edited on Roll; rendering the roll again from its
RAW files, and deleting them. The camera is not needed.

The pages of the sheet are made on the preview thread (`Previewer.make_sheet`): developing every frame
takes about 1.5 s for 36. Only while Finish is shown, and again whenever roll.json changes (options,
corrections, metadata, a render or a print); the pages shown stay until the new ones arrive.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QImage, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from .. import contact, metadata, paths
from ..session import Roll, clean_raws, has_raws
from . import style
from .viewer import SheetRequest

if TYPE_CHECKING:
    from .mainwindow import Now

FINISH_PAGE_WIDTH = 1080  # px: the panels and the contact sheet beside them, at most
SHEET_WIDTH = 380  # px: the contact sheet's panel; an A4 page in it is about 500 px high
PAGE_GAP = 10  # px between the sheet's pages
# The roll's fields as Finish shows them, in the form's order: the two dates of the shoot as one.
# Keywords and notes take the whole width.
SHOWN_FIELDS = (
    ("camera", "Camera"), ("lens", "Lens"), ("shot", "Date shot"), ("film", "Film stock"), ("format", "Format"), ("box_speed", "Box speed"),
    ("ei", "Shot at EI"), ("developer", "Developer or lab"), ("developed", "Date developed"), ("author", "Author"), ("copyright", "Copyright"),
    ("keywords", "Keywords"), ("notes", "Notes"),
)  # fmt: skip
WIDE_FIELDS = {"keywords", "notes"}


def frames_text(numbers: Sequence[int]) -> str:
    """"Frame 3", "Frames 1–7, 9–12, 14 and 15": runs of three or more as a span, as on the film."""
    runs: list[list[int]] = []
    for number in sorted(numbers):
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    items = []
    for run in runs:
        items += [f"{paths.label(run[0])}–{paths.label(run[-1])}"] if len(run) >= 3 else [paths.label(n) for n in run]
    listed = ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else items[0]
    return ("Frames " if len(numbers) > 1 else "Frame ") + listed


def prints_line(changed: Sequence[int], unprinted: Sequence[int], waiting: Sequence[int]) -> str:
    """What the prints panel says of a roll with frames: the frames whose print is out of date, the
    frames without one, the frames that cannot be printed yet (not rendered)."""
    one = lambda numbers, single, plural: single if len(numbers) == 1 else plural  # noqa: E731
    said = []
    if changed:
        said.append(f"{frames_text(changed)} changed since {one(changed, 'it was', 'they were')} printed.")
    if unprinted:
        said.append(f"{frames_text(unprinted)} {one(unprinted, 'has', 'have')} no print yet.")
    if waiting:
        said.append(f"{frames_text(waiting)} {one(waiting, 'is', 'are')} not ready to print: not rendered yet.")
    if not changed and not unprinted and not waiting:
        said.append("Every print is up to date.")
    return " ".join(said + ["16-bit TIFF into prints/."])


def shown_path(folder: Optional[Path]) -> str:
    """The roll's folder, the home folder as ~, with a place to break after each /: a path has no
    spaces, and a label that cannot wrap widens the page."""
    if folder is None:
        return "—"
    try:
        text = "~/" + folder.relative_to(Path.home()).as_posix()
    except ValueError:
        text = folder.as_posix()
    return text.replace("/", "/​")


class Meter(QWidget):
    """How much of the roll is printed: a bar, the accent on a track."""

    def __init__(self) -> None:
        super().__init__()
        self.fraction = 0.0
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_fraction(self, fraction: float) -> None:
        if fraction != self.fraction:
            self.fraction = fraction
            self.update()

    def paintEvent(self, _event) -> None:
        t = style.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(t.divider))
        painter.drawRoundedRect(QRectF(self.rect()), 3, 3)
        if self.fraction > 0:
            painter.setBrush(QColor(t.accent))
            painter.drawRoundedRect(QRectF(0, 0, self.width() * self.fraction, self.height()), 3, 3)


class SheetPages(QWidget):
    """The contact sheet's pages, one under the other at the widget's width, each with a hairline
    round it; with none, the outline of an empty page and why it is empty."""

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[QImage] = []
        self.message = ""
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def set_pages(self, pages: list[QImage], message: str = "") -> None:
        count = max(1, len(self.pages))
        self.pages, self.message = pages, message
        if max(1, len(pages)) != count:
            self.updateGeometry()
        self.update()

    def page_height(self, width: int) -> int:
        return round(width * contact.PAGE_H / contact.PAGE_W)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        count = max(1, len(self.pages))
        return count * self.page_height(width) + (count - 1) * PAGE_GAP

    def sizeHint(self) -> QSize:
        width = SHEET_WIDTH - 36
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:
        return QSize(120, self.heightForWidth(120))

    def paintEvent(self, _event) -> None:
        t = style.tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        height = self.page_height(self.width())
        for index, page in enumerate(self.pages or [None]):
            place = QRectF(0.5, index * (height + PAGE_GAP) + 0.5, self.width() - 1, height - 1)
            if page is not None:
                painter.drawImage(place, page)
                painter.setPen(QPen(QColor(t.line), 1))
            else:
                painter.setPen(QPen(QColor(t.control), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(place)
            if page is None and self.message:
                painter.setPen(QColor(t.muted))
                painter.drawText(place.adjusted(24, 24, -24, -24), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)


class FinishPage(QWidget):
    """On the left the prints, the contact sheet's options, the roll's metadata and its files; on the
    right the contact sheet as it will be written."""

    def __init__(self) -> None:
        super().__init__()
        scroll, column = style.full_page("Prints and the contact sheet, all from the roll folder. The camera and RAW files are not needed.", FINISH_PAGE_WIDTH)

        prints, prints_column = style.panel()
        style.set_tone(prints, "accent")  # what is left to do on a roll
        self.prints_count = style.label("", "data", wrap=False)
        prints_column.addLayout(_heading("Prints", self.prints_count))
        self.prints_meter = Meter()
        prints_column.addWidget(self.prints_meter)
        self.prints_line = style.label("", "muted")
        prints_column.addWidget(self.prints_line)
        self.print_button = QPushButton()
        style.set_role(self.print_button, "primary")
        self.print_button.setMinimumHeight(40)
        self.print_button.setToolTip("Write the print of every frame (prints/) that is missing or out of date")
        prints_column.addWidget(self.print_button)

        sheet, sheet_column = style.panel("Contact sheet")
        sheet_column.addWidget(style.label("An A4 PDF and PNG of every frame of the roll as its print will be, on film strips; written into the roll folder.", "muted"))
        self.sheet_options: dict[str, QCheckBox] = {}
        options = QHBoxLayout()
        for key, text in (("rebate", "Film rebate"), ("sprockets", "Sprocket holes")):
            box = self.sheet_options[key] = QCheckBox(text)
            options.addWidget(box)
        options.addStretch(1)
        self.contact_button = QPushButton("Write contact sheet")
        self.contact_button.setToolTip("Written into the roll folder as PDF and PNG; the PDF is opened")
        options.addWidget(self.contact_button)
        sheet_column.addLayout(options)
        # Drawn on the rebate, and only 135 film has them: anywhere else the box changes nothing.
        sheet_column.addWidget(style.label("Sprocket holes are drawn on the rebate of 135 film.", "muted"))

        about, about_column = style.panel()
        self.edit_button = QPushButton("Edit…")
        self.edit_button.setToolTip("The roll's metadata on Roll, where it is saved into every TIFF of the roll")
        about_column.addLayout(_heading("Roll metadata", self.edit_button))
        self.metadata_note = style.label("", "muted")
        about_column.addWidget(self.metadata_note)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.metadata_values: dict[str, QLabel] = {}
        place = 0  # cells taken, two fields to a row
        for key, text in SHOWN_FIELDS:
            wide = key in WIDE_FIELDS
            if wide and place % 2:
                place += 1
            row, side = divmod(place, 2)
            value = self.metadata_values[key] = style.label("—")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(style.label(text, "muted", wrap=False), row, 2 * side, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value, row, 2 * side + 1, 1, 3 if wide else 1)
            place += 2 if wide else 1
        about_column.addLayout(grid)

        files, files_column = style.panel("The roll's files")
        self.path_label = style.label("—", "data")
        files_column.addWidget(self.path_label)
        self.roll_button = QPushButton("Render roll again")
        self.clean_button = QPushButton("Delete RAW files…")
        style.set_role(self.clean_button, "danger")
        for button, text in (
            (self.roll_button, "Every frame again from its RAW files, with the picture area, crop and file options as they are set now."),
            (self.clean_button, "Frees the space the RAW files take. The TIFFs stay, and prints are made from them."),
        ):
            row = QHBoxLayout()
            row.addWidget(style.label(text, "muted"), 1)
            row.addWidget(button)
            files_column.addLayout(row)

        shown, shown_column = style.panel()
        shown.setFixedWidth(SHEET_WIDTH)
        self.sheet_count = style.label("", "muted", wrap=False)
        shown_column.addLayout(_heading("The contact sheet", self.sheet_count))
        self.sheet_pages = SheetPages()
        shown_column.addWidget(self.sheet_pages)

        left = QVBoxLayout()
        left.setSpacing(14)
        for widget in (prints, sheet, about, files):
            left.addWidget(widget)
        left.addStretch(1)
        right = QVBoxLayout()
        right.addWidget(shown)
        right.addStretch(1)
        sides = QHBoxLayout()
        sides.setSpacing(20)
        sides.addLayout(left, 1)
        sides.addLayout(right)
        column.addLayout(sides)
        column.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)


def _heading(text: str, beside: QWidget) -> QHBoxLayout:
    """A panel's heading with a widget at its right."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(style.label(text, "heading", wrap=False))
    row.addStretch(1)
    row.addWidget(beside)
    return row


class FinishPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_finish(self) -> None:
        self._writing_sheet = False  # a contact sheet is being written
        self._printing: Optional[tuple[int, int]] = None  # prints written and due in the batch being written
        self._sheet_asked: Optional[tuple[str, int]] = None  # the roll and roll.json's time the pages shown (or coming) are of
        self._sheet_seq = 0

    def _render_roll(self) -> None:
        folder = self._roll_folder()
        if folder is None:
            return
        rect = self.view.rects["picture"]
        settings: dict = {"crop": self.roll_page.crop_box.isChecked() and rect is not None, "compare": self.roll_page.compare_box.isChecked()}
        if rect:
            settings["rect"] = list(rect)
        if self._session_folder() == str(folder):
            settings["flatfield"] = self._session_flatfield()
        self._count_pending(+1)
        roll = Roll.open(folder)
        self._rendering.update(frame.number for frame in roll.frames if has_raws(roll, frame))
        self._refresh_strip()
        self.ask_render_roll.emit(str(folder), self.roll_page.negative_box.isChecked(), settings)

    def _clean_raws(self) -> None:
        folder = self._roll_folder()
        if folder is None:
            return
        roll = Roll.open(folder)
        files, size, kept = clean_raws(roll, dry_run=True)
        unrendered = f" Frames {', '.join(map(paths.label, kept))} have no TIFF yet and keep theirs." if kept else ""
        if files == 0:
            self._say("Nothing to delete: no frame of this roll has both RAW files and a TIFF." + unrendered)
            return
        question = (
            f"Delete {files} RAW files ({size / 1e9:.2f} GB) of roll “{roll.name}”?\n\n"
            "The TIFFs stay, and prints are made from them, so corrections and printing go on. These frames can then "
            f"no longer be rendered again from the RAW files, so settle on the picture area and the flat field first.{unrendered}"
        )
        if self._confirm("Delete RAW files", question):
            files, size, _ = clean_raws(roll)
            self._say(f"Deleted {files} RAW files, {size / 1e9:.2f} GB freed.")
            self._update_buttons()
            self._refresh_strip()

    def _write_prints(self) -> None:
        self._save_edits()
        if self._due and self._strip_roll and self._printing is None:
            self._say(f"Printing {self._due} frame{'s' if self._due != 1 else ''} into prints/…")
            self._printing = (0, self._due)
            self._count_pending(+1)
            self.ask_prints.emit(self._strip_roll)

    def _on_print_step(self, folder: str, done: int, total: int) -> None:
        """A print of the batch is written: the count, the meter and the strip's captions follow it."""
        self._printing = (done, total)
        if folder == self._strip_roll:
            self._refresh_strip()

    def _write_contact_sheet(self) -> None:
        """Every frame of the roll in the Roll fields, onto the sheet."""
        folder = self._roll_folder()
        if folder is None or self._writing_sheet:
            return
        self._save_edits()  # the sheet shows the corrections as they are now
        self._writing_sheet = True
        self._say(f"Writing the contact sheet of roll {folder.name}…")
        self._count_pending(+1)  # disables the button too (_update_buttons)
        self.ask_contact.emit(str(folder))

    def _on_sheet_written(self, path: str) -> None:
        """The contact sheet's job is over: open the PDF, if there is one."""
        self._writing_sheet = False
        self._update_buttons()
        if path:
            self._open_file(Path(path))

    def _open_file(self, path: Path) -> None:
        """Show a file in the program the system has for it: Preview, for the contact sheet."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _set_sheet_option(self, key: str, on: bool) -> None:
        folder = self._roll_folder()
        if Roll.state(folder) == "existing":
            Roll.open(folder).set_sheet({key: on})
        self._update_buttons()  # Sprocket holes follow the rebate; the pages are made again

    def _edit_roll_metadata(self) -> None:
        """Edit… on Finish: the roll's form on Roll, the one place it is edited and saved: a second
        form in a dialog would be a second set of unsaved edits of one roll."""
        self._go("roll")
        QTimer.singleShot(0, self._show_roll_form)  # once the page is laid out on screen

    def _show_roll_form(self) -> None:
        page = self.roll_page
        page.scroll.ensureWidgetVisible(page.metadata_panel)
        page.metadata_form.edits["camera"].setFocus()

    # ----- the contact sheet's pages -----

    def _ask_sheet(self) -> None:
        """The pages of the sheet as it would be written now, while Finish is shown: asked again when
        roll.json changes, which anything on the sheet does. The pages of another roll go at once; this
        roll's stay until the new ones arrive."""
        if self._step != "finish" or self._printing is not None:
            return  # a print changes roll.json but not the sheet: asked for once the batch is over
        folder = self._roll_folder()
        pages = self.finish_page.sheet_pages
        try:
            changed = (folder / "roll.json").stat().st_mtime_ns if folder is not None else None
        except OSError:
            changed = None
        if changed is None or not Roll.open(folder).frames:
            self._sheet_asked = None
            self._sheet_seq += 1  # a page still being made is not shown
            pages.set_pages([], "The contact sheet appears here once the roll has frames.")
            self.finish_page.sheet_count.setText("")
            return
        asked = (str(folder), changed)
        if asked == self._sheet_asked:
            return
        if self._sheet_asked is None or self._sheet_asked[0] != asked[0]:
            pages.set_pages([], "Making the contact sheet…")
            self.finish_page.sheet_count.setText("")
        self._sheet_asked = asked
        self._sheet_seq += 1
        self._previewer.newest_sheet = self._sheet_seq
        self.ask_sheet.emit(SheetRequest(str(folder), self._sheet_seq))

    def _on_sheet(self, request: SheetRequest, pages: list) -> None:
        if request.seq != self._sheet_seq:
            return  # asked for again since
        self.finish_page.sheet_pages.set_pages(pages, "The contact sheet could not be made: see the log.")
        self.finish_page.sheet_count.setText(f"{len(pages)} page{'s' if len(pages) != 1 else ''}" if pages else "")

    # ----- the page -----

    def _show_prints(self) -> None:
        """The prints panel, from the strip's roll (its developments, the prints due) once the strip
        shows the roll in the Roll fields; called by `_refresh_strip` and `_update_finish_page`."""
        page = self.finish_page
        folder = self._roll_folder()
        here = folder is not None and str(folder) == self._strip_roll
        frames = list(self._developments) if here else []
        if frames:
            place = paths.Place(folder, self._strip_name)
            due = self._developments.due
            printed = {n for n in frames if paths.tiff(place, n, "print").exists()}
            current = printed - due
            waiting = set(frames) - current - due
            page.prints_count.setText(f"{len(current)} / {len(frames)}")
            page.prints_meter.set_fraction(len(current) / len(frames))
            page.prints_line.setText(prints_line(sorted(due & printed), sorted(due - printed), sorted(waiting)))
        else:
            state = Roll.state(folder)
            name = Roll.open(folder).name if state == "existing" else folder.name if folder is not None else ""
            page.prints_count.setText("0 / 0")
            page.prints_meter.set_fraction(0.0)
            page.prints_line.setText(
                "No roll chosen: name one on Roll." if state == "none"
                else f"{name} is a folder, not a roll." if state == "taken"
                else f"{name} has no frames yet." + (" Its session starts on Roll." if state == "new" else "")
            )  # fmt: skip
        due = self._due if here else 0
        if self._printing is not None:
            done, total = self._printing
            page.print_button.setText(f"Printing {min(done + 1, total)} of {total}…")
        else:
            page.print_button.setText(f"Print {due} frame{'s' if due != 1 else ''}" if due else "Prints up to date" if frames else "Print")
        page.print_button.setEnabled(due > 0 and self._printing is None)

    def _show_finish_metadata(self, now: "Now") -> None:
        page = self.finish_page
        fields = now.roll.metadata if now.roll is not None else {}
        page.metadata_note.setText(
            "Written into every TIFF of the roll." if now.roll is not None
            else "Written when its session starts; the fields are on Roll." if now.state == "new"
            else "No roll chosen."
        )  # fmt: skip
        for key, label in page.metadata_values.items():
            if key == "shot":
                text = metadata.dates(fields.get("shot_from", ""), fields.get("shot_to", ""))
            elif key == "keywords":
                text = ", ".join(fields.get("keywords", []))
            else:
                text = str(fields.get(key, "")).strip()
            label.setText(text or "—")
            if (label.property("role") or "") != ("" if text else "muted"):  # restyled only when it changes: every update comes here
                style.set_role(label, "" if text else "muted")

    def _update_finish_page(self, now: "Now") -> None:
        page = self.finish_page
        self._show_prints()
        self._show_finish_metadata(now)
        page.path_label.setText(shown_path(now.folder))
        page.path_label.setToolTip(str(now.folder) if now.folder is not None else "")
        page.roll_button.setEnabled(now.has_frames and now.has_raws)
        page.roll_button.setText("Render roll again" + (f"  ({self._pending} rendering)" if self._pending else ""))
        page.clean_button.setEnabled(now.idle and self._pending == 0 and now.has_raws)
        page.contact_button.setEnabled(now.has_frames and not self._writing_sheet)
        rebate = page.sheet_options["rebate"]
        rebate.setEnabled(now.roll is not None)
        # Drawn on the rebate, on 135 only: elsewhere the box would change nothing on the sheet.
        page.sheet_options["sprockets"].setEnabled(now.roll is not None and rebate.isChecked() and contact.film_format(now.roll).perforated)
        self._ask_sheet()
