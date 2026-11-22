"""A roll's contact sheet: an A4 PDF in the style of a darkroom contact print, and the same pages as PNG.

Every frame as its print will be (its base and corrections, developed from its preview: no
prints and no RAW files needed), on strips with a film rebate, under a header from the roll's
metadata, with the roll's notes at the foot of the last page. Order, numbers, metadata, colour
or black and white: everything is taken from the roll.

The rules: millimetres everywhere and one factor to device units; one drawing routine,
for the PDF and for the tests' pictures; the frame size depends only on the format (and on the
rebate and notes bands), never on how many frames there are, so a short roll leaves blank paper;
fonts sized from points in millimetres, so the PDF is the same at any resolution.

The layout (`layout`) is plain arithmetic; only the drawing uses Qt (QPdfWriter, part of PySide6).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPageSize, QPainter, QPdfWriter, QPen
from PySide6.QtWidgets import QApplication

from . import development, kinds, metadata, paths
from .window.viewer import to_qimage

# ----- formats -----


@dataclass(frozen=True)
class Format:
    name: str  # as in the roll's metadata (metadata.FORMATS)
    aspect: float  # width / height of a frame lying landscape
    per_row: int  # frames on one strip
    rows: int  # strips on a page
    perforated: bool = False  # 35 mm film: sprocket holes, and frame numbers printed "▸ 7 … 7A"; 120 has neither

    @property
    def per_page(self) -> int:
        return self.per_row * self.rows


# The names are the metadata's (metadata.FORMATS), in its order; here only what the sheet does with them.
FORMATS = {
    name: Format(name, *geometry)
    for name, geometry in zip(
        metadata.FORMATS,
        (
            (36 / 24, 6, 7, True),  # 135: 42 places, a 36-exposure roll and its extras on one page
            (56 / 42, 4, 4),  # 120 6×4.5: 16 places for its 15 or 16 frames
            (1.0, 4, 3),  # 120 6×6: 12 places, 12 frames
            (70 / 56, 4, 3),  # 120 6×7: 12 places for its 10 frames
            (84 / 56, 4, 2),  # 120 6×9: 8 places, 8 frames
        ),
        strict=True,
    )
}


def film_format(roll) -> Format:
    """The roll's format; a roll whose metadata names none is 135."""
    return FORMATS[roll.metadata.get("format") or metadata.FORMATS[0]]


# ----- the page, in millimetres -----

PAGE_W, PAGE_H = 210.0, 297.0  # A4 portrait
MARGIN = 10.0
HEADER_H = 18.0  # the header band at the top
FRAME_GAP_X = 2.0  # between frames on a strip
ROW_GAP = 4.5  # between strips without a rebate
ROW_GAP_REBATE = 3.0  # white paper between strips with one
CAPTION_H = 3.2  # the caption line under each frame (under the strip with a rebate)
REBATE_H = 4.2  # rebate band above and below the pictures: 135's 5.5 mm per side, scaled to the frame
NOTES_GAP = 3.0  # between the last strip and the notes' rule
NOTES_H = 24.0  # the notes block at the foot of the last page
CAPTION_PT, HEADER_TITLE_PT, HEADER_SUB_PT, REBATE_PT, NOTES_PT = 5.5, 11.0, 7.5, 5.0, 7.5
PT_MM = 0.3528  # one point in millimetres
TITLE_H, DETAILS_H = 7.0, 5.0  # mm: the header's two lines, the roll's name and its metadata
RULE_ABOVE = 2.5  # mm: the header's rule, this far above the header band's end
NOTES_INSET = 1.5  # mm: the notes' text under their rule
HOLE_H, HOLE_EDGE = 1.5, 0.45  # mm: a sprocket hole's height, and its distance from the film's edge
HOLES_PER_FRAME = 8  # 135 has eight perforations to a frame
FRAME_SPACING = 1 / 30  # a frame's pitch is its width plus this share of it on each side (135: 36 mm frames, 38 mm apart)
HOLE_SHARE, HOLE_ROUND = 0.55, 0.28  # a hole's width as a share of the pitch; its corner radius as a share of its height
NUMBER_INSET = 1.2  # mm: the frame numbers in from the frame's sides


@dataclass(frozen=True)
class Place:
    index: int  # the frame's place in the roll (0 = its first frame), not its number
    row: int
    col: int
    x: float  # mm, the picture's left edge
    y: float  # mm, its top edge
    w: float
    h: float


@dataclass(frozen=True)
class Layout:
    frame_w: float
    frame_h: float
    pages: tuple[tuple[Place, ...], ...]  # at least one, empty for a roll without frames


def layout(fmt: Format, count: int, rebate: bool = False, notes: bool = False) -> Layout:
    """Where every frame goes. Frames fill the pages in order, `fmt.per_page` to a page. The
    frame size is fixed by the format: as wide as the page allows, smaller only if the format's
    rows would not fit (rebate bands, and the notes block at the foot when there are notes; it is
    kept free on every page, so all pages have the same frames)."""
    cols, rows = fmt.per_row, fmt.rows
    content_w = PAGE_W - 2 * MARGIN
    content_h = PAGE_H - 2 * MARGIN - HEADER_H - ((NOTES_H + NOTES_GAP) if notes else 0.0)
    band = REBATE_H if rebate else 0.0
    gap = ROW_GAP_REBATE if rebate else ROW_GAP
    fw = (content_w - (cols - 1) * FRAME_GAP_X) / cols
    fh = fw / fmt.aspect
    fixed = rows * (2 * band + CAPTION_H) + (rows - 1) * gap
    if rows * fh + fixed > content_h:
        fh = (content_h - fixed) / rows
        fw = fh * fmt.aspect
    x0 = MARGIN + (content_w - (cols * fw + (cols - 1) * FRAME_GAP_X)) / 2
    y0 = MARGIN + HEADER_H
    pages = []
    for start in range(0, max(count, 1), fmt.per_page):
        places = []
        for index in range(start, min(count, start + fmt.per_page)):
            row, col = divmod(index - start, cols)
            places.append(Place(index, row, col, x0 + col * (fw + FRAME_GAP_X), y0 + band + row * (fh + 2 * band + CAPTION_H + gap), fw, fh))
        pages.append(tuple(places))
    return Layout(fw, fh, tuple(pages))


# ----- the frames' pictures -----

SHEET_EDGE = 1000  # px: the preview is reduced to this before developing; a 30 mm frame at 600 dpi is 700 px


def centre_crop(image: np.ndarray, aspect: float) -> np.ndarray:
    height, width = image.shape[:2]
    if abs(width / height - aspect) < 1e-3:
        return image
    if width / height > aspect:  # too wide: the sides go
        keep = round(height * aspect)
        left = (width - keep) // 2
        return image[:, left : left + keep]
    keep = round(width / aspect)
    top = (height - keep) // 2
    return image[top : top + keep]


def picture(dev: Optional[development.Development], fmt: Format) -> Optional[np.ndarray]:
    """The frame as its print will be (8-bit RGB), lying landscape, cut to the format: the picture
    area of its preview, developed by its development, turned as its files. None for a frame
    without a picture (no preview, or not rendered yet)."""
    try:
        shown = development.picture(dev, SHEET_EDGE, cut=True).rgb if dev is not None else None
    except (development.Undeveloped, OSError):
        shown = None
    if shown is None:
        return None
    if shown.shape[0] > shown.shape[1]:  # portrait: on its side, clockwise, every one the same way, as on a real contact print
        shown = np.rot90(shown, -1)
    return np.ascontiguousarray(centre_crop(shown, fmt.aspect))


@dataclass
class Contents:
    """Everything a sheet shows, taken from the roll once."""

    title: str  # the roll's name
    details: str  # camera · film at EI · dates · developer
    notes: str
    stock: str  # for the edge print, uppercased
    colour: bool  # colour negative: orange edge print; black and white: white
    fmt: Format
    labels: list[str]  # the frames' numbers as on the film, in order
    pictures: list[Optional[np.ndarray]]
    rebate: bool
    sprockets: bool

    @property
    def missing(self) -> list[str]:
        """The frames without a picture (no preview)."""
        return [label for label, shown in zip(self.labels, self.pictures, strict=True) if shown is None]


def contents(roll) -> Contents:
    fmt = film_format(roll)
    fields = roll.metadata or {}
    developments = development.of_roll(roll)
    frames = sorted(roll.frames, key=lambda frame: frame.number)
    lines = metadata.roll_summary(fields)
    preset = roll.scanned_with()
    made = {preset.kind} if preset is not None else {frame.kind for frame in frames}
    return Contents(
        title=roll.name, details=" · ".join(lines), notes=fields.get("notes", "").strip(), stock=fields.get("film", "").upper().strip(),
        colour=not made or any(kinds.colour(kind) for kind in made), fmt=fmt, labels=[paths.label(frame.number) for frame in frames],
        pictures=[picture(developments.get(frame.number), fmt) for frame in frames],
        rebate=roll.sheet_options()["rebate"], sprockets=roll.sheet_options()["sprockets"] and fmt.perforated,
    )  # fmt: skip


# ----- drawing -----

INK = QColor(40, 40, 40)  # near black, easy on toner
FAINT = QColor(150, 150, 150)  # hairlines and rules
PAPER = QColor(255, 255, 255)
EMPTY = QColor(235, 235, 235)  # a frame without a picture
REBATE = QColor(18, 18, 18)  # a positive print of the film's rebate: black, colour or black and white
EDGE_WHITE = QColor(225, 225, 225)  # black-and-white film's edge print, printed
EDGE_ORANGE = QColor(232, 140, 44)  # colour negative film's edge print, printed (C-41)


def _font(pt: float, mm: float, bold: bool = False) -> QFont:
    font = QFont("Helvetica")
    font.setBold(bold)
    font.setPixelSize(max(1, round(pt * PT_MM * mm)))
    return font


def paint_page(p: QPainter, sheet: Contents, lay: Layout, page: int, mm: float) -> None:
    """One page; `mm` is device units per millimetre."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.fillRect(QRectF(0, 0, PAGE_W * mm, PAGE_H * mm), PAPER)
    _paint_header(p, sheet, page, len(lay.pages), mm)
    places = lay.pages[page]
    if sheet.rebate:
        _paint_strips(p, sheet, places, mm)
    caption = _font(CAPTION_PT, mm)
    for place in places:
        rect = QRectF(place.x * mm, place.y * mm, place.w * mm, place.h * mm)
        shown = sheet.pictures[place.index]
        if shown is not None:
            p.drawImage(rect, to_qimage(shown))
        else:
            p.fillRect(rect, EMPTY)
            p.setPen(QPen(FAINT))
            p.setFont(caption)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{sheet.labels[place.index]}\nno preview")
        if not sheet.rebate:
            p.setPen(QPen(FAINT, 0.15 * mm))  # a hairline keeps the frame apart from the paper
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rect)
            p.setPen(QPen(INK))  # the number; with a rebate it is in the edge print
            p.setFont(caption)
            p.drawText(QRectF(place.x * mm, (place.y + place.h) * mm, place.w * mm, CAPTION_H * mm),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sheet.labels[place.index])  # fmt: skip
    if sheet.notes and page == len(lay.pages) - 1:
        _paint_notes(p, sheet.notes, mm)


def _paint_header(p: QPainter, sheet: Contents, page: int, pages: int, mm: float) -> None:
    x, w = MARGIN * mm, (PAGE_W - 2 * MARGIN) * mm
    title = QRectF(x, MARGIN * mm, w, TITLE_H * mm)
    p.setPen(QPen(INK))
    p.setFont(_font(HEADER_TITLE_PT, mm, bold=True))
    p.drawText(title, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sheet.title)
    p.setFont(_font(HEADER_SUB_PT, mm))
    right = sheet.fmt.name + (f" · page {page + 1} of {pages}" if pages > 1 else "")
    p.drawText(title, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, right)
    p.drawText(QRectF(x, (MARGIN + TITLE_H) * mm, w, DETAILS_H * mm), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sheet.details)
    p.setPen(QPen(FAINT, 0.2 * mm))
    rule = (MARGIN + HEADER_H - RULE_ABOVE) * mm
    p.drawLine(int(x), int(rule), int(x + w), int(rule))


def _paint_notes(p: QPainter, notes: str, mm: float) -> None:
    """The roll's notes, a block at the foot of the page (the layout kept it free)."""
    x, w = MARGIN * mm, (PAGE_W - 2 * MARGIN) * mm
    top = (PAGE_H - MARGIN - NOTES_H) * mm
    p.setPen(QPen(FAINT, 0.2 * mm))
    p.drawLine(int(x), int(top), int(x + w), int(top))
    p.setPen(QPen(INK))
    p.setFont(_font(NOTES_PT, mm))
    p.drawText(QRectF(x, top + NOTES_INSET * mm, w, (NOTES_H - NOTES_INSET) * mm), int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap), notes)


def _paint_strips(p: QPainter, sheet: Contents, places: tuple[Place, ...], mm: float) -> None:
    """One black strip per row, through the gaps between frames, with the edge print: the film
    stock along the top rebate over every other frame, small ticks over the others; the frame
    numbers along the bottom (135: "▸ 7" … "7A"; 120: "7"). Sprocket holes (`sheet.sprockets`, only
    ever set for perforated film) go through to the paper near both edges, and the lettering moves
    in towards the picture, as on real film."""
    edge = EDGE_ORANGE if sheet.colour else EDGE_WHITE
    holes = sheet.sprockets
    font = _font(REBATE_PT - (1.0 if holes else 0.0), mm)
    hole_h, hole_margin = HOLE_H * mm, HOLE_EDGE * mm
    band = REBATE_H * mm
    inner = band - (hole_h + hole_margin) if holes else band
    rows: dict[int, list[Place]] = {}
    for place in places:
        rows.setdefault(place.row, []).append(place)
    for row in rows.values():
        first, last = row[0], row[-1]
        x0, x1 = first.x * mm, (last.x + last.w) * mm
        top = (first.y - REBATE_H) * mm
        bottom = (first.y + first.h) * mm  # the bottom band's top
        p.fillRect(QRectF(x0, top, x1 - x0, 2 * band + first.h * mm), REBATE)
        if holes:
            _paint_sprockets(p, x0, x1, top + hole_margin, hole_h, first.w * mm)
            _paint_sprockets(p, x0, x1, bottom + band - hole_margin - hole_h, hole_h, first.w * mm)
        p.setPen(QPen(edge))
        p.setFont(font)
        for k, place in enumerate(row):
            slot = QRectF(place.x * mm, top + band - inner, place.w * mm, inner)
            if k % 2 == 0 and sheet.stock:
                p.drawText(slot, Qt.AlignmentFlag.AlignCenter, sheet.stock)
            elif k % 2 == 1:
                p.drawText(slot, Qt.AlignmentFlag.AlignCenter, "▪ ▪▪ ▪")
            label = sheet.labels[place.index]
            numbers = QRectF((place.x + NUMBER_INSET) * mm, bottom, (place.w - 2 * NUMBER_INSET) * mm, inner)
            if sheet.fmt.perforated:
                p.drawText(numbers, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"▸ {label}")
                p.drawText(numbers, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{label}A")
            else:
                p.drawText(numbers, Qt.AlignmentFlag.AlignCenter, label)


def _paint_sprockets(p: QPainter, x0: float, x1: float, y: float, hole_h: float, frame_w: float) -> None:
    """A row of perforations from x0 to x1 (device units), rounded, as 135 has."""
    pitch = frame_w * (1 + 2 * FRAME_SPACING) / HOLES_PER_FRAME
    hole_w, radius = pitch * HOLE_SHARE, hole_h * HOLE_ROUND
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(PAPER)
    x = x0 + (pitch - hole_w) / 2
    while x + hole_w <= x1 - 0.5:
        p.drawRoundedRect(QRectF(x, y, hole_w, hole_h), radius, radius)
        x += pitch
    p.setBrush(Qt.BrushStyle.NoBrush)


# ----- output -----

PDF_DPI = 600  # what QPdfWriter is told: the pictures and fonts are placed at this resolution; 600 is a print's
PNG_DPI = 300  # the PNG's pages: A4 at 2480 x 3508 px, enough to print; at 600 a page is 35 MP for the same pictures (SHEET_EDGE)
_application = None  # made here for the command line: fonts need one


def _ensure_application() -> None:
    """A QApplication, not just a QGuiApplication: a window opened later in the same process
    finds this one, and widgets on a QGuiApplication abort the process (as when the contact sheet's
    tests run before the window's)."""
    global _application
    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no window, no Dock icon
        _application = QApplication([])


def sheet_layout(sheet: Contents) -> Layout:
    return layout(sheet.fmt, len(sheet.labels), sheet.rebate, bool(sheet.notes))


def page_image(sheet: Contents, page: int, dpi: float = 100) -> QImage:
    """One page as a picture: what the PDF holds, at `dpi`."""
    _ensure_application()
    mm = dpi / 25.4
    image = QImage(round(PAGE_W * mm), round(PAGE_H * mm), QImage.Format.Format_RGB32)
    image.fill(PAPER)
    painter = QPainter(image)
    try:
        paint_page(painter, sheet, sheet_layout(sheet), page, mm)
    finally:
        painter.end()
    return image


def write_pdf(sheet: Contents, target: Path, dpi: int = PDF_DPI) -> Path:
    """The sheet as an A4 PDF, every page; written under another name and renamed, so a PDF
    that is there is whole."""
    _ensure_application()
    target = Path(target)
    partial = target.with_name(target.name + ".part")
    lay = sheet_layout(sheet)
    writer = QPdfWriter(str(partial))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setResolution(dpi)
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))  # the margins are the layout's
    writer.setTitle(f"{sheet.title} contact sheet")
    writer.setCreator("TetherInvert")
    painter = QPainter(writer)
    try:
        for page in range(len(lay.pages)):
            if page:
                writer.newPage()
            paint_page(painter, sheet, lay, page, dpi / 25.4)
    finally:
        painter.end()
    del writer
    partial.replace(target)
    return target


def write_png(sheet: Contents, target: Path, dpi: int = PNG_DPI) -> list[Path]:
    """The sheet's pages as PNG pictures at `dpi` (stored in the file, so it prints at A4):
    `target` for the first page, `paths.contact_page` names the others. Each written under another
    name and renamed; pages a longer sheet left there before are removed."""
    target = Path(target)
    written = []
    for page in range(len(sheet_layout(sheet).pages)):
        image = page_image(sheet, page, dpi)
        image.setDotsPerMeterX(round(dpi / 0.0254))
        image.setDotsPerMeterY(round(dpi / 0.0254))
        path = paths.contact_page(target, page)
        partial = path.with_name(path.name + ".part")
        if not image.save(str(partial), "PNG"):
            raise OSError(f"{path.name} could not be written")
        partial.replace(path)
        written.append(path)
    page = len(written)
    while (stale := paths.contact_page(target, page)).exists():
        stale.unlink()
        page += 1
    return written


def write_sheet(roll, target: Optional[Path] = None) -> tuple[Path, list[Path], list[str]]:
    """The roll's contact sheet: the PDF (`<roll>/<Roll>_contact.pdf` unless `target` says
    otherwise), the PNG pages beside it under the same name, and the numbers of the frames shown
    empty for want of a preview."""
    sheet = contents(roll)
    pdf = write_pdf(sheet, Path(target) if target else paths.contact_sheet(roll))
    return pdf, write_png(sheet, pdf.with_suffix(".png")), sheet.missing
