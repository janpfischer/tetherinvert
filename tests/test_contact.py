"""The contact sheet: its layout (plain arithmetic), its pictures, its drawing (pages painted into
images at a low resolution and looked at pixel by pixel), the PDF and the command."""

from __future__ import annotations

import os
import re
from dataclasses import replace

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from tetherinvert import contact, development, invert, metadata, paths, preview, sim  # noqa: E402
from tetherinvert.calibrate import Roi  # noqa: E402
from tetherinvert.cli import main as cli  # noqa: E402
from tetherinvert.session import Roll, Scanner, render  # noqa: E402

DPI = 100
MM = DPI / 25.4


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))


def rig() -> Scanner:
    light = sim.SimLight()
    return Scanner(light, sim.SimCamera(light), settle_s=0.0)


@pytest.fixture(scope="module")
def rolls(tmp_path_factory):
    """A colour roll of five frames (the third scanned turned to portrait, the fourth corrected,
    film stock and notes in its metadata) and a black-and-white roll of two, without metadata."""
    folder = tmp_path_factory.mktemp("sheets")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TETHERINVERT_HOME", str(folder / "home"))
        scanner = rig()
        colour = Roll.open(folder / "Roll15")
        colour.set_settings(rect=list(sim.PICTURE_RECT))
        colour.set_metadata(metadata.updated({}, {"film": "Kodak Gold 200", "camera": "Nikon FM2", "notes": "Weekend at the lake."}))
        preset = scanner.calibrate("Gold", Roi(*sim.BASE_ROI))
        for index in range(5):
            render(colour, scanner.scan(colour, preset, orientation={"turn": 90, "mirror": False} if index == 2 else None))
        Roll.open(colour.folder).update_frame(4, adjust={"d": -30, "c": 40})
        white = Roll.open(folder / "HP5")
        white.set_settings(rect=list(sim.PICTURE_RECT))
        preset = scanner.calibrate("HP5", Roi(*sim.BASE_ROI), kind="white")
        for _ in range(2):
            render(white, scanner.scan(white, preset))
    return Roll.open(colour.folder), Roll.open(white.folder)


def pixels(image) -> np.ndarray:
    """A QImage as (h, w, 3) uint8."""
    from PySide6.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_RGB888)
    rows = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return rows[:, : 3 * image.width()].reshape(image.height(), image.width(), 3).copy()


def area(page: np.ndarray, x: float, y: float, w: float, h: float) -> np.ndarray:
    """A rectangle of a page, given in millimetres."""
    return page[round(y * MM) : round((y + h) * MM), round(x * MM) : round((x + w) * MM)]


def fake(labels: list[str], **changes) -> contact.Contents:
    """A sheet's contents without a roll: grey pictures, for layout and PDF tests."""
    grey = np.full((40, 60, 3), 128, np.uint8)
    sheet = contact.Contents("Roll", "", "", "HP5 PLUS", False, contact.FORMATS["135"], labels, [grey] * len(labels), True, False)
    return replace(sheet, **changes)


# ----- layout -----


@pytest.mark.parametrize("notes", [False, True], ids=["plain", "notes"])
@pytest.mark.parametrize("rebate", [False, True], ids=["bare", "rebate"])
@pytest.mark.parametrize("fmt", contact.FORMATS.values(), ids=lambda f: f.name)
def test_a_full_page_fits_every_format(fmt, rebate, notes):
    lay = contact.layout(fmt, fmt.per_page, rebate, notes)
    band = contact.REBATE_H if rebate else 0.0
    bottom = contact.PAGE_H - contact.MARGIN - ((contact.NOTES_H + contact.NOTES_GAP) if notes else 0.0)
    assert len(lay.pages) == 1 and len(lay.pages[0]) == fmt.per_page
    for place in lay.pages[0]:
        assert place.x >= contact.MARGIN - 1e-6 and place.x + place.w <= contact.PAGE_W - contact.MARGIN + 1e-6
        assert place.y - band >= contact.MARGIN + contact.HEADER_H - 1e-6
        assert place.y + place.h + band + contact.CAPTION_H <= bottom + 1e-6
        assert place.w / place.h == pytest.approx(fmt.aspect)
    rows = sorted({place.y for place in lay.pages[0]})
    assert all(b - a >= lay.frame_h + 2 * band + contact.CAPTION_H for a, b in zip(rows, rows[1:], strict=False))  # strips do not overlap


def test_the_frame_size_is_the_formats_whatever_the_count():
    assert set(contact.FORMATS) == set(metadata.FORMATS)  # the metadata's choices are the sheet's formats
    fmt = contact.FORMATS["135"]
    sizes = {(round(lay.frame_w, 6), round(lay.frame_h, 6)) for lay in (contact.layout(fmt, n) for n in (1, 9, 36, 80))}
    assert len(sizes) == 1  # a short roll leaves paper blank, it does not get bigger frames
    assert contact.layout(fmt, 9).pages[0][6].x == pytest.approx(contact.layout(fmt, 9).pages[0][0].x)  # columns line up
    assert contact.layout(fmt, 12, rebate=True).pages[0][0].y == pytest.approx(contact.layout(fmt, 12).pages[0][0].y + contact.REBATE_H)


def test_long_rolls_go_onto_more_pages_in_order():
    # 135 takes 42 to a page (6 x 7): a roll with 00, 0 and its extras fits one sheet.
    assert [len(page) for page in contact.layout(contact.FORMATS["135"], 38).pages] == [38]
    assert [len(page) for page in contact.layout(contact.FORMATS["135"], 43).pages] == [42, 1]
    assert [len(page) for page in contact.layout(contact.FORMATS["120 6×6"], 13).pages] == [12, 1]
    pages = contact.layout(contact.FORMATS["120 6×9"], 17).pages
    assert [place.index for page in pages for place in page] == list(range(17)) and len(pages) == 3
    assert pages[1][0].y == pytest.approx(pages[0][0].y) and pages[1][0].row == 0
    assert len(contact.layout(contact.FORMATS["135"], 0).pages) == 1  # an empty roll still has its page


def test_frames_come_in_number_order_and_a_deleted_one_leaves_no_gap(tmp_path):
    from tetherinvert.session import Frame

    roll = Roll(tmp_path / "R", frames=[Frame(n, [], "p", "rgb", [0.0] * 3, [0.1, 0.1, 0.1, 0.1]) for n in (3, -1, 1, 0)])
    sheet = contact.contents(roll)
    assert sheet.labels == ["00", "0", "1", "3"] and sheet.missing == ["00", "0", "1", "3"]  # no previews: shown empty
    assert [place.index for place in contact.sheet_layout(sheet).pages[0]] == [0, 1, 2, 3]
    assert sheet.fmt.name == "135" and sheet.rebate and not sheet.sprockets  # the defaults


# ----- pictures -----


def test_each_frame_is_shown_as_its_print_will_be(rolls):
    colour, _white = rolls
    fmt = contact.FORMATS["135"]
    developments = development.of_roll(colour)
    shown = contact.picture(developments[4], fmt)
    expected = development.picture(developments[4], cut=True).rgb  # the picture area, developed as the window shows it
    assert developments[4].adjust != invert.NEUTRAL  # with its corrections
    assert shown.dtype == np.uint8 and shown.shape[1] / shown.shape[0] == pytest.approx(fmt.aspect, abs=0.01)
    assert np.abs(shown.reshape(-1, 3).mean(0) - expected.reshape(-1, 3).mean(0)).max() < 3  # its corrections, its picture area
    plain = contact.picture(developments[5], fmt)
    assert np.abs(shown.reshape(-1, 3).mean(0) - plain.reshape(-1, 3).mean(0)).max() > 10  # and not some other frame's


def test_a_portrait_frame_lies_on_its_side_clockwise(rolls):
    colour, _white = rolls
    dev = development.of_roll(colour)[3]
    assert dev.orientation == {"turn": 90, "mirror": False}
    upright = development.picture(dev, contact.SHEET_EDGE, cut=True).rgb
    assert upright.shape[0] > upright.shape[1]  # stored portrait
    shown = contact.picture(dev, contact.FORMATS["135"])
    assert shown.shape[1] > shown.shape[0]
    assert np.array_equal(shown, contact.centre_crop(np.rot90(upright, -1), 1.5))


# ----- drawing -----


def strip_band(sheet: contact.Contents, page: np.ndarray) -> np.ndarray:
    """The first strip's top rebate band, the whole strip long."""
    row = [place for place in contact.sheet_layout(sheet).pages[0] if place.row == 0]
    return area(page, row[0].x, row[0].y - contact.REBATE_H, row[-1].x + row[-1].w - row[0].x, contact.REBATE_H)


def test_the_rebate_is_black_with_orange_lettering_on_colour_and_white_on_black_and_white(rolls):
    colour, white = rolls
    for roll, lettering in ((colour, "orange"), (white, "white")):
        sheet = contact.contents(roll)
        assert sheet.colour == (lettering == "orange")
        page = pixels(contact.page_image(sheet, 0, DPI))
        band = strip_band(sheet, page).reshape(-1, 3).astype(int)
        assert (np.median(band, axis=0) < 30).all()  # black film
        brightest = band[band.sum(1).argmax()]
        if lettering == "orange":
            assert brightest[0] > 180 and brightest[0] - brightest[2] > 90 and brightest[1] > brightest[2]
        else:
            assert brightest.min() > 150 and brightest.max() - brightest.min() < 20
    assert contact.contents(colour).stock == "KODAK GOLD 200" and contact.contents(white).stock == ""


def test_sprocket_holes_only_on_135(rolls, tmp_path):
    import shutil

    shutil.copytree(rolls[0].folder, tmp_path / "Roll15")
    roll = Roll.open(tmp_path / "Roll15")
    roll.set_sheet({"sprockets": True})
    sheet = contact.contents(Roll.open(roll.folder))
    page = pixels(contact.page_image(sheet, 0, 200))
    place = contact.sheet_layout(sheet).pages[0][0]
    holes = page[round((place.y - contact.REBATE_H + 0.45 + 0.75) * 200 / 25.4), round(place.x * 200 / 25.4) : round((place.x + place.w) * 200 / 25.4)]
    assert (holes.min(1) > 240).sum() > 20  # white paper through the film
    roll.set_metadata({**roll.metadata, "format": "120 6×6"})
    square = contact.contents(Roll.open(roll.folder))
    assert not square.sprockets and square.fmt.aspect == 1.0
    assert pixels(contact.page_image(square, 0, DPI)).tobytes() == pixels(contact.page_image(replace(square, sprockets=False), 0, DPI)).tobytes()


def test_a_frame_on_the_page_is_its_picture(rolls):
    colour, _white = rolls
    sheet = contact.contents(colour)
    page = pixels(contact.page_image(sheet, 0, 200))
    place = contact.sheet_layout(sheet).pages[0][3]  # frame 4, corrected
    scale = 200 / 25.4
    drawn = page[round((place.y + 1) * scale) : round((place.y + place.h - 1) * scale), round((place.x + 1) * scale) : round((place.x + place.w - 1) * scale)]
    assert np.abs(drawn.reshape(-1, 3).mean(0) - sheet.pictures[3].reshape(-1, 3).mean(0)).max() < 6


def test_the_notes_are_at_the_foot_of_the_last_page_only_when_there_are_some(rolls):
    colour, white = rolls
    rule = contact.PAGE_H - contact.MARGIN - contact.NOTES_H
    for roll, noted in ((colour, True), (white, False)):
        page = pixels(contact.page_image(contact.contents(roll), 0, DPI))
        line = area(page, contact.MARGIN, rule - 0.5, contact.PAGE_W - 2 * contact.MARGIN, 1.0)
        assert (line.min() < 235) == noted  # a 0.2 mm hairline: grey at this resolution
    two = fake([str(n) for n in range(1, 50)], notes="Notes")
    first, last = (pixels(contact.page_image(two, page, DPI)) for page in (0, 1))
    assert area(first, contact.MARGIN, rule - 0.5, 100, 1).min() > 250 and area(last, contact.MARGIN, rule - 0.5, 100, 1).min() < 235


def test_without_a_rebate_the_number_is_under_the_frame(rolls):
    colour, _white = rolls
    sheet = replace(contact.contents(colour), rebate=False)
    page = pixels(contact.page_image(sheet, 0, 200))
    place = contact.sheet_layout(sheet).pages[0][0]
    scale = 200 / 25.4
    caption = page[round((place.y + place.h) * scale) : round((place.y + place.h + contact.CAPTION_H) * scale), round(place.x * scale) : round((place.x + 5) * scale)]
    assert caption.min() < 120  # ink: the frame number
    assert page[round((place.y - 2) * scale), round((place.x + place.w / 2) * scale)].min() > 240  # and no strip above the frame


# ----- the PDF and the command -----


def test_the_pdf_has_a_page_per_sheet_and_is_written_whole(tmp_path):
    target = tmp_path / "sheet.pdf"
    contact.write_pdf(fake([str(n) for n in range(1, 44)]), target, dpi=150)
    data = target.read_bytes()
    assert data.startswith(b"%PDF") and len(re.findall(rb"/Type\s*/Page\b(?!s)", data)) == 2
    assert not list(tmp_path.glob("*.part"))
    try:
        from PySide6.QtPdf import QPdfDocument
    except ImportError:
        return
    document = QPdfDocument()
    document.load(str(target))
    assert document.pageCount() == 2


def test_the_png_has_a_file_per_page_at_its_resolution_and_drops_pages_no_longer_there(tmp_path):
    from PySide6.QtGui import QImage

    target = tmp_path / "sheet.png"
    two = contact.write_png(fake([str(n) for n in range(1, 44)]), target, dpi=DPI)
    assert two == [target, tmp_path / "sheet_2.png"] and not list(tmp_path.glob("*.part"))
    image = QImage(str(target))
    assert (image.width(), image.height()) == (round(contact.PAGE_W * MM), round(contact.PAGE_H * MM))
    assert round(image.dotsPerMeterX() * 0.0254) == DPI  # prints at A4
    assert pixels(image).tobytes() == pixels(contact.page_image(fake([str(n) for n in range(1, 44)]), 0, DPI)).tobytes()
    assert contact.write_png(fake(["1", "2"]), target, dpi=DPI) == [target]
    assert sorted(tmp_path.iterdir()) == [target]  # the second page of before is gone


def test_the_contact_sheet_on_the_command_line(rolls, tmp_path, capsys):
    import shutil

    shutil.copytree(rolls[1].folder, tmp_path / "HP5")
    folder = tmp_path / "HP5"
    assert cli(["contactsheet", str(folder)]) == 0
    assert paths.contact_sheet(Roll.open(folder)) == folder / "HP5_contact.pdf" and (folder / "HP5_contact.pdf").read_bytes().startswith(b"%PDF")
    assert (folder / "HP5_contact.png").read_bytes().startswith(b"\x89PNG")
    out = capsys.readouterr().out
    assert f"contact sheet: {folder / 'HP5_contact.pdf'}" in out and f"contact sheet: {folder / 'HP5_contact.png'}" in out
    preview.path(Roll.open(folder), 2).unlink()
    assert cli(["contactsheet", str(folder), "--sprockets", "--no-rebate", "--out", str(tmp_path / "other.pdf")]) == 0
    assert "shown empty, no preview (render them again): frame 2" in capsys.readouterr().err and (tmp_path / "other.pdf").exists() and (tmp_path / "other.png").exists()
    assert Roll.open(folder).sheet == {"sprockets": True, "rebate": False}  # kept for the roll
    assert cli(["contactsheet", str(tmp_path)]) == 1 and "not a roll folder" in capsys.readouterr().err
    assert cli(["metadata", str(folder), "format=6x7"]) == 0 and Roll.open(folder).metadata["format"] == "120 6×7"
    assert cli(["metadata", str(folder), "format=8x10"]) == 1 and "not a film format" in capsys.readouterr().err
