"""End-to-end tests on the simulated rig, plus the device drivers against fakes."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
import tifffile

from tetherinvert import development, invert, paths, preview, sim
from tetherinvert.calibrate import FULL, TARGET, CalibrationError, Roi, shutter_seconds, true_seconds, usable_ladder
from tetherinvert.camera import CameraError, GphotoCamera
from tetherinvert.cli import main as cli
from tetherinvert.scanlight import Scanlight, packet
from tetherinvert.session import Frame, Preset, PresetStore, Roll, Scanner, render, render_roll, write_prints

BASE = Roi(*sim.BASE_ROI)


def rig(shake=(0.0, 0.0)):
    light = sim.SimLight()
    return Scanner(light, sim.SimCamera(light, shake=shake), settle_s=0.0)


def patch(image: np.ndarray, box, margin: int = 8) -> np.ndarray:
    """Mean of a fractional box in a 0..1 image."""
    height, width = image.shape[:2]
    x, y, w, h = box
    area = image[int(y * height) + margin : int((y + h) * height) - margin, int(x * width) + margin : int((x + w) * width) - margin]
    return area.reshape(-1, image.shape[2] if image.ndim == 3 else 1).mean(axis=0)


def read(path: Path) -> np.ndarray:
    return tifffile.imread(path).astype(np.float32) / FULL


def due(roll: Roll, number: int) -> bool:
    """Whether the frame's print has to be written."""
    return number in development.of_roll(roll).due


def plain(roll: Roll, number: int, base: str = "flat") -> development.Development:
    """The frame's development with this base and no corrections."""
    return replace(development.of_roll(roll)[number], base=base).corrected(None)


@pytest.fixture(scope="module")
def colour_roll(tmp_path_factory):
    """One calibrated colour preset and a three-frame roll, shared by the read-only tests."""
    scanner = rig(shake=(1.4, -0.6))
    preset = scanner.calibrate("Portra", BASE)
    roll = Roll.open(tmp_path_factory.mktemp("rolls") / "Roll.01")
    roll.set_settings(rect=list(sim.PICTURE_RECT))
    for _ in range(3):
        render(roll, scanner.scan(roll, preset), negative=True)
    return scanner, preset, roll


# ----- Scanlight -----


class FakeSerial:
    def __init__(self, replies: bytes = b"") -> None:
        self.written, self._replies, self.closed, self.events = [], replies, False, []

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))
        self.events.append("write")

    def read(self, count: int) -> bytes:
        count = min(count, 5)  # packets arrive in pieces
        chunk, self._replies = self._replies[:count], self._replies[count:]
        return chunk

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.events.append("close")


def test_closing_the_scanlight_waits_for_it_to_answer_after_off(monkeypatch):
    """The light handles packets in order: its answer to a question sent after "off" means it took
    "off". Closing at once loses "off" in about half the tries on the real rig."""
    version = packet(3, ((1 << 16) | 0).to_bytes(4, "big"))
    port = FakeSerial(version)
    light = Scanlight(serial_obj=port)
    light.set(0, 255, 0)
    port.events.clear()
    light.close()
    assert port.written[-2:] == [bytes([0xFE, 0, 6, 0, 0, 0, 0, 0, 0]), bytes([0xFE, 2, 0])]  # off, then the question
    assert port.events == ["write", "write", "close"] and port._replies == b""  # the answer was read before closing

    monkeypatch.setattr("tetherinvert.scanlight.CLOSE_CONFIRM_S", 0.05)
    silent = FakeSerial()
    Scanlight(serial_obj=silent).close()
    assert silent.closed  # a light that does not answer is closed anyway


def test_scanlight_packets(monkeypatch):
    monkeypatch.setattr("tetherinvert.scanlight.CLOSE_CONFIRM_S", 0.05)  # no light here to confirm "off"
    port = FakeSerial()
    light = Scanlight(serial_obj=port)
    light.set(200, 90, 70)
    assert port.written[-1] == bytes([0xFE, 0, 6, 200, 90, 70, 0, 0, 0])
    light.set(w=255)
    assert port.written[-1] == bytes([0xFE, 0, 6, 0, 0, 0, 255, 0, 0])
    with pytest.raises(ValueError):
        light.set(10, 0, 0, 10)
    light.close()
    assert port.written[-2:] == [bytes([0xFE, 0, 6, 0, 0, 0, 0, 0, 0]), bytes([0xFE, 2, 0])] and port.closed  # off, then confirmed


def test_scanlight_reads_version_between_telemetry():
    temperature = packet(1, (36500).to_bytes(4, "big", signed=True))
    version = packet(3, ((3 << 16) | 7).to_bytes(4, "big"))
    light = Scanlight(serial_obj=FakeSerial(b"\x00junk" + temperature + version))
    assert light.temperature() is None  # nothing complete has arrived yet
    assert light.info() == (7, 3)
    assert light.temperature() == 36.5
    assert Scanlight(serial_obj=FakeSerial(version)).describe() == "Scanlight v4b (fw 7)"


def test_the_scanlight_tells_its_stored_colour_default_between_telemetry():
    """GET_DEFAULT_RGB (1, no payload) is answered by DEFAULT_RGB (4: r, g, b). Temperature
    telemetry arrives in between, and is kept."""
    temperature = packet(1, (31250).to_bytes(4, "big", signed=True))
    port = FakeSerial(temperature + b"\x07" + packet(4, bytes([200, 170, 230])) + temperature)
    light = Scanlight(serial_obj=port)
    assert light.default_rgb() == (200, 170, 230)
    assert port.written == [bytes([0xFE, 1, 0])] and light.temperature() == 31.25
    with pytest.raises(Exception, match="did not answer"):
        Scanlight(serial_obj=FakeSerial(temperature)).default_rgb(timeout=0.2)


# ----- camera wrapper against a fake libgphoto2 -----


ARW = b"II*\x00" + bytes(200_000)  # starts like the TIFF container an ARW is
JPEG = b"\xff\xd8\xff\xe1" + bytes(200_000)


class FakeGphoto:
    GP_WIDGET_RADIO, GP_WIDGET_MENU, GP_WIDGET_TEXT = 5, 6, 2
    GP_CAPTURE_IMAGE, GP_FILE_TYPE_NORMAL, GP_EVENT_TIMEOUT, GP_EVENT_UNKNOWN = 0, 1, 1, 0
    GP_OPERATION_CAPTURE_PREVIEW = 8

    class GPhoto2Error(Exception):
        def __init__(self, code: int = -1) -> None:
            super().__init__(f"[{code}]")
            self.code = code

    def __init__(self, still_name: str = "capt_DSC00001.ARW", preview: bool = True, still: bytes = ARW) -> None:
        self.still_name, self.still, self.operations = still_name, still, 8 if preview else 0
        self.values = {"iso": "Auto ISO", "shutterspeed": "1/60", "f-number": None, "capturetarget": "card"}
        self.options = {
            "iso": ["Auto ISO", "100", "200"],
            "shutterspeed": ["1/60", "1/3", "0.4"],
            "f-number": [],  # a lens without electronic aperture
            "capturetarget": ["card", "card+sdram", "sdram"],
        }
        self.reads_before_settled = 0
        self.events = self.deleted = self.unsafe_reads = 0

    def Camera(self):
        return FakeCamera(self)


class FakeWidget:
    def __init__(self, gp: FakeGphoto, name: str) -> None:
        self.gp, self.name, self.pending = gp, name, None

    def get_type(self):
        return self.gp.GP_WIDGET_RADIO

    def count_choices(self):
        return len(self.gp.options[self.name])

    def get_choice(self, index):
        return self.gp.options[self.name][index]

    def get_value(self):
        if not self.gp.options[self.name]:
            self.gp.unsafe_reads += 1  # the real library would crash here
        if self.gp.reads_before_settled > 0:
            self.gp.reads_before_settled -= 1
            return "…"
        return self.gp.values[self.name]

    def set_value(self, value):
        self.pending = value


class FakeFile:
    """Like the real binding's file object: its buffer is only good while the object lives."""

    def __init__(self, data: bytes) -> None:
        self._buffer = bytearray(data)

    def get_data_and_size(self):
        return memoryview(self._buffer)

    def __del__(self) -> None:
        self._buffer[:32] = b"\x01" + bytes(31)[: max(0, min(31, len(self._buffer) - 1))]


class FakeCamera:
    def __init__(self, gp: FakeGphoto) -> None:
        self.gp = gp

    def init(self):
        pass

    def exit(self):
        pass

    def get_summary(self):
        return "Manufacturer: Sony\nModel: ILCE-6700\n"

    def get_abilities(self):
        return type("Abilities", (), {"operations": self.gp.operations})()

    def get_single_config(self, name):
        if name not in self.gp.values:
            raise self.gp.GPhoto2Error(-2)
        return FakeWidget(self.gp, name)

    def set_single_config(self, name, widget):
        self.gp.values[name] = widget.pending
        self.gp.reads_before_settled = 2  # the body reports a new value only after a moment

    def capture(self, _kind):
        self.gp.events = 2
        return type("Taken", (), {"folder": "/", "name": self.gp.still_name})()

    def file_get(self, _folder, _name, _kind):
        return FakeFile(self.gp.still)

    def file_delete(self, _folder, _name):
        self.gp.deleted += 1

    def wait_for_event(self, _ms):
        if self.gp.events:
            self.gp.events -= 1
            return self.gp.GP_EVENT_UNKNOWN, None
        return self.gp.GP_EVENT_TIMEOUT, None

    def capture_preview(self):
        return type("File", (), {"get_data_and_size": lambda _self: b"\xff\xd8jpeg"})()


def test_camera_settings_and_capture(tmp_path):
    gp = FakeGphoto()
    camera = GphotoCamera(gp)
    camera.open()
    assert camera.model == "ILCE-6700"
    assert gp.values["capturetarget"] == "sdram"  # stills go to the computer only, not also to the card
    assert camera.choices("iso") == ["100", "200"]  # Auto ISO is not offered
    assert camera.choices("aperture") == [] and camera.get("aperture") is None
    assert gp.unsafe_reads == 0  # the empty aperture widget was never read
    assert camera.set("shutter", "1/3") and gp.values["shutterspeed"] == "1/3"  # waited for the read-back
    assert not camera.set("shutter", "1/8000")  # not offered by this body
    path = camera.capture(tmp_path / "Roll.01_001_R")
    assert path.name == "Roll.01_001_R.ARW" and path.read_bytes() == ARW and camera.last_name == "capt_DSC00001.ARW"
    assert gp.events == 0 and gp.deleted == 1  # events drained, file removed from the body
    assert camera.preview() == b"\xff\xd8jpeg"


def test_the_simulator_shows_real_pictures_in_turn(tmp_path, monkeypatch):
    folder = tmp_path / "demo"
    folder.mkdir()
    landscape = np.full((40, 60, 3), 64, np.uint8)
    landscape[:, :30] = 255  # bright on the left
    portrait = np.full((60, 40, 3), 255, np.uint8)
    portrait[:30] = 0  # dark at the top: on the left once it lies turned, as on film
    cv2.imwrite(str(folder / "a.jpg"), landscape)
    cv2.imwrite(str(folder / "b.png"), portrait)
    (folder / "notes.txt").write_text("not a picture")
    assert sim.Bench().camera().pictures == []  # without the variable: the synthetic scene and its wedge
    monkeypatch.setenv("TETHERINVERT_SIM_PICTURES", str(folder))
    camera = sim.Bench().camera()
    assert [p.name for p in camera.pictures] == ["a.jpg", "b.png"]

    def left_and_right(image: np.ndarray) -> tuple[float, float]:
        return float(np.median(image[80:120, 45:105])), float(np.median(image[80:120, 195:255]))

    a, b = (sim.scene(200, 300, 1, p) for p in camera.pictures)
    assert left_and_right(a) == pytest.approx((1.0, 0.051), abs=0.01)  # sRGB 64 is 0.051 linear
    assert left_and_right(b) == pytest.approx((sim.SCENE_FLOOR, 1.0), abs=0.01)
    synthetic = sim.scene(200, 300, 1)
    assert np.array_equal(a < 0, synthetic < 0) and np.array_equal(a == 0, synthetic == 0)  # the same holder and rebate

    def bright_left() -> bool:
        left, right = left_and_right(camera._film(200, 300).mean(axis=2))
        return left < right  # a bright scene is dense on the negative

    assert bright_left()  # the first picture before any still
    for name, first in (("Roll_002", False), ("Roll_003", True), ("Roll_002", False), ("Roll_00", True), ("Roll_000", False)):  # 3 starts over; a rescan of 2 shows 2 again; 0 the last, 00 the one before
        camera.capture(tmp_path / "Roll" / f"{name}_R")
        assert bright_left() == first

    (folder / "a.jpg").unlink()
    (folder / "b.png").unlink()
    with pytest.raises(ValueError, match="no JPEG, TIFF or PNG"):
        sim.Bench().camera()


def test_a_body_that_keeps_its_card_copy_is_left_alone_and_reported(capsys):
    """Some bodies take the save destination only from their own menu."""

    class Stubborn(FakeCamera):
        def set_single_config(self, name, widget):
            self.gp.writes.append((name, widget.pending))
            if (name, widget.pending) != ("capturetarget", "sdram"):  # that one it ignores
                super().set_single_config(name, widget)

    def rig(start: str) -> tuple[FakeGphoto, GphotoCamera]:
        gp = FakeGphoto()
        gp.values["capturetarget"], gp.writes, gp.Camera = start, [], lambda: Stubborn(gp)
        camera = GphotoCamera(gp)
        camera.open()
        return gp, camera

    import tetherinvert.camera as module

    settle, module.WRITE_SETTLE_S = module.WRITE_SETTLE_S, 0.3
    try:
        first = FakeGphoto()
        camera = GphotoCamera(first)
        camera.open()
        assert camera.keeps_card_copy() is False  # the usual case: computer only

        # Already reaching the computer: asked once for computer only, then left as it is.
        gp, camera = rig("card+sdram")
        assert gp.writes == [("capturetarget", "sdram")] and gp.values["capturetarget"] == "card+sdram"
        assert camera.keeps_card_copy() is True

        # Card only would lose the download, so computer and card is the fallback.
        gp, camera = rig("card")
        assert gp.writes == [("capturetarget", "sdram"), ("capturetarget", "card+sdram")] and camera.keeps_card_copy() is True
    finally:
        module.WRITE_SETTLE_S = settle

    from tetherinvert.cli import stills_go_to

    assert "computer AND memory card" in stills_go_to(camera) and "Still Img. Save Dest." in stills_go_to(camera)
    del gp.values["capturetarget"]
    assert camera.keeps_card_copy() is None and "does not say" in stills_go_to(camera)
    assert cli(["--sim", "status"]) == 0 and "stills:  computer only" in capsys.readouterr().out


def test_camera_rejects_jpeg_and_skips_missing_live_view(tmp_path):
    camera = GphotoCamera(FakeGphoto(still_name="capt0001.JPG", preview=False))
    camera.open()
    with pytest.raises(CameraError, match="RAW only"):
        camera.capture(tmp_path / "x")
    assert not camera.has_live_view and camera.preview() is None


def test_download_survives_the_real_binding(tmp_path):
    """The real python-gphoto2 frees a file's buffer with the file object. Copied one moment too
    late, the first 32 bytes of every RAW are garbage."""
    gphoto2 = pytest.importorskip("gphoto2")
    source = tmp_path / "source.bin"
    source.write_bytes(b"II*\x00" + bytes(range(256)) * 600)

    class Camera(FakeCamera):
        def file_get(self, _folder, _name, _kind):
            real = gphoto2.CameraFile()
            real.open(str(source))
            return real

    fake = FakeGphoto()
    fake.Camera = lambda: Camera(fake)
    camera = GphotoCamera(fake)
    camera.open()
    assert camera.capture(tmp_path / "shot").read_bytes() == source.read_bytes()


def test_camera_checks_what_it_was_sent(tmp_path):
    for still, message in ((JPEG, "sent a JPEG picture named capt_DSC00001.ARW"), (b"II*\x00", "sent only 4 bytes")):
        camera = GphotoCamera(FakeGphoto(still=still))
        camera.open()
        with pytest.raises(CameraError, match=message):
            camera.capture(tmp_path / "x")
        assert not list(tmp_path.iterdir())  # nothing unusable is left behind
        assert camera.capture(tmp_path / "kept", check=False).read_bytes() == still  # unless asked, for diagnosis
        (tmp_path / "kept.ARW").unlink()


def test_undecodable_files_explain_themselves(tmp_path, monkeypatch):
    from tetherinvert.raw import RawError, decode_linear, sniff

    assert [sniff(h) for h in (ARW, JPEG, b"\0\0\0\x18ftypheic", b"\0\0\0\x18ftypcrx ", b"junk")] == ["TIFF", "JPEG", "HEIF", "CR3", "unknown"]
    cases = {
        "a.ARW": (JPEG, "holds a JPEG picture, not a RAW"),
        "b.ARW": (b"\0\0\0\x18ftypheic" + bytes(200_000), "holds a HEIF picture"),
        "c.ARW": (b"II*\x00", "cut short"),
        "d.ARW": (b"II*\x00" + bytes(range(256)) * 800, "looks like a RAW file, but the decoder cannot read it"),
        "e.ARW": (bytes(range(7, 200)) * 2000, "not a file type the decoder knows"),
    }
    for name, (data, message) in cases.items():
        (tmp_path / name).write_bytes(data)
        with pytest.raises(RawError, match=message):
            decode_linear(tmp_path / name)

    # In calibration the shot lives in a scratch folder; the file that failed must survive it.
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    scanner = rig()
    real_capture = scanner.camera.capture

    def jpeg_instead(stem, check=True):
        path = real_capture(stem, check)
        path.write_bytes(JPEG)
        return path

    scanner.camera.capture = jpeg_instead
    with pytest.raises(CalibrationError, match="holds a JPEG picture.*The file was kept at"):
        scanner.calibrate("film", BASE)
    kept = list((tmp_path / "home" / "failed").iterdir())
    assert len(kept) == 1 and kept[0].read_bytes() == JPEG


# ----- calibration -----


def test_shutter_labels():
    assert shutter_seconds("1/100") == 0.01 and shutter_seconds("4/10") == 0.4 and shutter_seconds("0.4000s") == 0.4
    assert usable_ladder(["Bulb", "30", "2", "1/3", "1/250", "1/4000", "1/0", ""]) == ["1/250", "1/3", "2"]
    assert true_seconds("1/3") == pytest.approx(0.315, abs=0.001)  # a third-stop rung, not 0.333
    assert true_seconds("1/250") == pytest.approx(1 / 256, rel=0.001)


@pytest.mark.parametrize("iso, aperture", [("100", "f/8"), ("400", "f/5.6"), ("100", "f/11")])
def test_rgb_calibration_lands_every_channel_on_target(iso, aperture):
    scanner = rig()
    scanner.camera.set("iso", iso)
    scanner.camera.set("aperture", aperture)
    preset = scanner.calibrate("film", BASE)
    assert preset.kind == "rgb" and preset.levels[3] == 0 and (preset.iso, preset.aperture) == (iso, aperture)
    assert 1 / 250 <= shutter_seconds(preset.shutter) <= 2.0
    assert all(40 <= level <= 255 for level in preset.levels[:3])
    assert max(preset.levels[:3]) >= 150  # the weakest channel runs its LED high, so the shutter is short
    # The base ends up equally bright in all three channels: that is the white balance.
    assert np.allclose(np.array(preset.base) / FULL, TARGET, atol=0.06)
    assert scanner.light.color == (0, 0, 0, 0)


def test_white_calibration():
    preset = rig().calibrate("HP5", BASE, "white")
    assert preset.kind == "white" and preset.levels[:3] == (0, 0, 0) and 40 <= preset.levels[3] <= 255
    assert max(preset.base) / FULL == pytest.approx(TARGET, abs=0.06)  # the brightest channel sets the limit


def test_rgb_together_calibrates_the_shutter_only_at_the_lights_colour_default(tmp_path):
    """The levels are the light's stored default; the shutter puts the brightest channel's film base
    0 to 1/3 stop under the target, the others where the default and the mask put them."""
    from tetherinvert import wizard
    from tetherinvert.calibrate import CLIP_BUDGET, TOLERANCE
    from tetherinvert.raw import decode_linear

    scanner = rig()
    steps = []
    preset = scanner.calibrate("gold200-together", BASE, "rgb-together", progress=steps.append)
    assert preset.kind == "rgb-together" and preset.levels == (*sim.COLOUR_DEFAULT, 0)
    brightest = max(preset.base) / FULL
    assert TARGET * 2 ** (-1 / 3) * (1 - TOLERANCE) <= brightest <= TARGET * (1 + 2 * TOLERANCE)
    assert min(preset.base) / FULL < 0.8 * TARGET  # the default does not balance the mask: the others sit lower
    stages = [step.stage for step in steps]
    assert "trim" not in stages and stages[-1] == "shutter" and steps[-1].shutter == preset.shutter
    checks = stages.index("check")  # the probes, then the shots at the shutter found
    assert set(stages[:checks]) == {"probe"} and set(stages[checks:-1]) == {"check"} and steps[-2].shutter == preset.shutter
    shots = [step for step in steps if step.histogram is not None]
    assert all(shot.colour == "RGB" and shot.level == 0 and sorted(shot.histogram.counts) == [0, 1, 2] for shot in shots)
    assert shots[-1].histogram.clipped <= CLIP_BUDGET and scanner.light.color == (0, 0, 0, 0)
    shown = wizard.Wizard.new_preset()
    shown.kind, shown.state, shown.shots = "rgb-together", wizard.RUNNING, steps
    assert shown.histogram()[1] == f"Last shot: colour default check, {preset.shutter} s" and len(shown.stages()) == 3
    # Shot as a scan would be, nothing in the rebate clips.
    scanner.light.set(*preset.levels)
    scanner.camera.set("shutter", preset.shutter)
    image = decode_linear(scanner.camera.capture(tmp_path / "check"), half=True)
    x0, y0, x1, y1 = BASE.pixels(image.shape[1], image.shape[0])
    assert image[y0:y1, x0:x1].max() < 0.998 * FULL


def test_rgb_together_measures_again_when_the_probe_misjudged_the_light(monkeypatch):
    """A probe that thought the light three times brighter than it is puts the first shot far under
    target with nothing clipped: that is not a rectangle half on the holder, so it measures again."""
    from tetherinvert.calibrate import TOLERANCE, Calibrator

    probe = Calibrator._probe
    monkeypatch.setattr(Calibrator, "_probe", lambda self, *args: 3 * probe(self, *args))
    preset = rig().calibrate("film", BASE, "rgb-together")
    assert TARGET * 2 ** (-1 / 3) * (1 - TOLERANCE) <= max(preset.base) / FULL <= TARGET * (1 + 2 * TOLERANCE)


def test_rgb_together_at_the_factory_default_calibrates_and_says_so():
    from tetherinvert import wizard

    scanner = rig()
    scanner.light.default = (255, 255, 255)
    preset = scanner.calibrate("film", BASE, "rgb-together")
    rows = dict(wizard.result(preset))
    assert preset.levels == (255, 255, 255, 0) and rows["LEDs"] == "R 255 · G 255 · B 255 from the light"
    assert "factory default" in rows["Note"]
    assert "Note" not in dict(wizard.result(rig().calibrate("film", BASE, "rgb-together")))
    scanner.light.default = (0, 0, 0)
    with pytest.raises(CalibrationError, match="every LED off"):
        scanner.calibrate("film", BASE, "rgb-together")


def test_a_sloppy_base_rectangle_still_finds_the_base():
    """The rebate is narrow. A rectangle that also covers holder and picture must give the
    same preset as one drawn exactly on the base."""
    exact = rig().calibrate("film", BASE)
    x, y, w, h = sim.BASE_ROI
    sloppy = rig().calibrate("film", Roi(x - 0.02, y, w + 0.06, h))  # from the holder into the picture
    assert sloppy.shutter == exact.shutter
    assert np.allclose(sloppy.levels, exact.levels, atol=6)
    assert np.allclose(sloppy.base, exact.base, rtol=0.03)


def test_calibration_takes_the_camera_off_auto_iso():
    scanner = rig()
    scanner.camera._settings["iso"] = "Auto ISO"
    assert scanner.calibrate("film", BASE).iso == "100"


def test_calibration_says_which_way_to_go(monkeypatch):
    monkeypatch.setattr(sim, "RESPONSE", sim.RESPONSE * 2000)
    with pytest.raises(CalibrationError, match="close the aperture"):
        rig().calibrate("film", BASE)
    monkeypatch.setattr(sim, "RESPONSE", sim.RESPONSE / 2000 / 500)
    with pytest.raises(CalibrationError, match="open the aperture"):
        rig().calibrate("film", BASE)


def test_calibration_notices_a_rectangle_off_the_film():
    with pytest.raises(CalibrationError, match="clear film base"):
        rig().calibrate("film", Roi(0.0, 0.0, 0.02, 0.02))  # on the holder


def test_every_calibration_shot_is_reported_with_its_histogram():
    """The window's W4 draws these: the probes, the shutter, then the trims, each shot with the
    histogram of its own colour's channel and where the film base landed in it."""
    from tetherinvert.calibrate import TOLERANCE

    steps = []
    preset = rig().calibrate("film", BASE, progress=steps.append)
    solved = [i for i, step in enumerate(steps) if step.stage == "shutter"]
    assert len(solved) == 1 and steps[solved[0]].shutter == preset.shutter and steps[solved[0]].histogram is None
    assert {step.stage for step in steps[: solved[0]]} == {"probe"} and {step.stage for step in steps[solved[0] + 1 :]} == {"trim"}
    shots = [step for step in steps if step.histogram is not None]
    assert all(list(shot.histogram.counts) == ["RGB".index(shot.colour)] for shot in shots)
    last = {shot.colour: shot for shot in shots}  # each colour's last shot: the one kept
    assert sorted(last) == ["B", "G", "R"]
    for colour, shot in last.items():
        channel = "RGB".index(colour)
        assert shot.level == preset.levels[channel] and shot.shutter == preset.shutter
        assert 2 ** shot.histogram.base[channel] == pytest.approx(preset.base[channel] / FULL, rel=0.01) and shot.histogram.clipped == 0
        # The film base is the brightest thing in the frame: hardly a pixel above the target's tolerance.
        counts, edges = shot.histogram.counts[channel], np.linspace(-10, 0, len(shot.histogram.counts[channel]) + 1)
        assert counts[edges[1:] > np.log2(TARGET * (1 + 2 * TOLERANCE))].sum() <= 0.001 * counts.sum()
        assert counts[edges[:-1] > np.log2(0.5)].sum() > 0.01 * counts.sum()  # and the rebate is there, just under the ceiling
    white = []
    rig().calibrate("HP5", BASE, "white", progress=white.append)
    assert all(sorted(step.histogram.counts) == [0, 1, 2] for step in white if step.histogram is not None)  # under white light, every channel


def test_a_stopped_calibration_ends_at_its_next_shot():
    steps = []
    scanner = rig()
    with pytest.raises(CalibrationError, match="Calibration cancelled"):
        scanner.calibrate("film", BASE, cancelled=lambda: len(steps) >= 2, progress=steps.append)
    assert len(steps) == 2 and scanner.light.color == (0, 0, 0, 0)


def test_preset_names_are_kept_as_typed_and_compared_without_case(tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    assert store.newest() is None and store.find("gold200") is None
    gold = Preset("gold200", "rgb", (200, 100, 90, 0), "1/4", roi=sim.BASE_ROI, created="2026-10-01 10:00")
    store.save(gold)
    assert store.get("GOLD200") == gold and store.find(" Gold200 ") == "gold200"
    store.save(replace(gold, name="Gold200", created="2026-10-02 10:00"))  # the new spelling replaces the old
    store.save(Preset("hp5", "white", (0, 0, 0, 200), "1/30", roi=sim.BASE_ROI, created="2026-09-01 09:00"))
    assert store.names() == ["Gold200", "hp5"] and store.get("gold200").created == "2026-10-02 10:00"
    assert store.newest().name == "Gold200"
    store.delete("GOLD200")
    assert store.names() == ["hp5"]
    with pytest.raises(KeyError, match="known: hp5"):
        store.get("gold200")


def test_a_presets_film_base_picture_goes_when_it_is_calibrated_again_or_deleted(tmp_path):
    """The window keeps the live view around the film base with a preset; a new calibration under that
    name (the store's save) and a delete take it away. Any name is a file name, compared without case."""
    store = PresetStore(tmp_path / "presets.json")
    gold = Preset("Gold 200/120", "rgb", (200, 100, 90, 0), "1/4", roi=sim.BASE_ROI)
    store.save(gold)
    picture = store.picture("gold 200/120")
    assert picture == store.picture(" GOLD 200/120 ") and picture.parent == tmp_path / "presets" and "/" not in picture.name
    picture.parent.mkdir()
    picture.write_bytes(b"jpeg")
    store.save(replace(gold, shutter="1/2"))
    assert not picture.exists()  # it showed where the calibration replaced was metered
    picture.write_bytes(b"jpeg")
    store.delete("GOLD 200/120")
    assert not picture.exists() and store.names() == []


def test_every_kind_is_one_entry_of_one_table_and_nothing_compares_kind_strings():
    """kinds.py says what each kind means: its exposures, its film, whether it needs the
    Scanlight and how it is calibrated. The code asks the table; no kind string is compared."""
    import re

    import tetherinvert
    from tetherinvert import kinds, wizard

    assert not hasattr(wizard, "KINDS")
    table = {name: (k.exposures, k.colour, k.scanlight, k.from_light, k.prompted) for name, k in kinds.KINDS.items()}
    assert table == {
        "rgb": (("R", "G", "B"), True, True, False, False),
        "rgb-together": (("RGB",), True, True, True, False),
        "white": (("W",), False, True, False, False),
        "own-rgb": (("R", "G", "B"), True, False, False, True),  # the operator switches the colour for every shot
        "own-colour": (("O",), True, False, False, False),
        "own-bw": (("O",), False, False, False, False),
    }
    assert kinds.EXPOSURES["O"].light((1, 2, 3, 4)) == (0, 0, 0, 0) and kinds.EXPOSURES["O"].sensors == (0, 1, 2)  # TetherInvert lights nothing
    lit = [kinds.EXPOSURES[letter].light((200, 170, 230, 0)) for letter in ("R", "G", "B", "RGB")]
    assert lit == [(200, 0, 0, 0), (0, 170, 0, 0), (0, 0, 230, 0), (200, 170, 230, 0)]
    assert kinds.EXPOSURES["W"].light((0, 0, 0, 90)) == (0, 0, 0, 90) and kinds.EXPOSURES["W"].sensors == (0, 1, 2)
    source = {path.name: path.read_text() for path in Path(tetherinvert.__file__).parent.glob("*.py")}
    comparison = r"\.kind\s*[!=]=\s*['\"]|_kinds\.get\([^)]*\)\s*[!=]=|\bkinds\s*[!=]=\s*\{"
    compared = [(name, found.group()) for name, text in source.items() for found in re.finditer(comparison, text)]
    assert compared == []
    named = {found.group(1) for text in source.values() for found in re.finditer(r"\bkind\s*(?::\s*str)?\s*=\s*['\"]([\w-]+)['\"]", text)}
    assert named and named <= set(kinds.KINDS)


def test_the_calibration_wizards_rules():
    """wizard.py without a window: the ways in, what each step needs before Next, Back keeping
    what was entered, W4's stages, histogram and result, and steps inserted and appended."""
    from tetherinvert import wizard as w
    from tetherinvert.calibrate import Histogram, Step as Shot

    last = Preset("gold200", "rgb", (200, 100, 90, 0), "1/4", iso="100", aperture="f/8", base=(58982.0, 57000.0, 59000.0),
                  created="2026-10-01 10:00", roi=sim.BASE_ROI, by_hand=("aperture",))  # fmt: skip
    new = w.Wizard.new_preset(last, rect=None)
    assert [step.key for step in new.steps] == [w.FILM, w.CAMERA, w.FILM_BASE, w.CALIBRATE] and new.step.key == w.FILM and not new.fixed
    assert new.values["aperture"] == "f/8" and new.position() == "Step 1 of 4 · Film" and new.heading() == "New preset"
    # W1: a name. W2: a camera, and what it does not report. W3: the film base rectangle.
    assert new.missing() == "Name the film." and not new.next() and new.at == 0
    new.name, new.kind = "Portra", "white"
    assert new.next() and new.step.key == w.CAMERA and new.heading() == "New preset · Portra · black and white"
    assert "not connected" in new.missing() and not new.next()
    new.camera, new.unknown = True, ("aperture",)
    new.values["aperture"] = " "
    assert new.missing() == "Fill in the f-stop." and not new.next()
    new.values["aperture"] = "f/5.6"
    assert new.next() and new.step.base_tool and new.by_hand() == {"aperture": "f/5.6"}  # the ISO is reported, so not stated
    assert new.next_label() == "Calibrate" and "rectangle" in new.missing() and not new.next()
    # Back keeps what was entered.
    assert new.back() and new.back() and new.step.key == w.FILM and not new.back()
    assert (new.name, new.kind, new.values["aperture"]) == ("Portra", "white", "f/5.6")
    assert new.next() and new.next()
    new.rect = sim.BASE_ROI
    assert new.next() and new.state == w.RUNNING and new.step.key == w.CALIBRATE
    assert not new.can_back() and not new.can_cancel() and new.next_label() == ""
    new.failed("Calibration cancelled.")
    assert new.state == w.FAILED and new.can_back() and new.can_cancel() and new.stages()[0][1] == "todo"
    assert new.back() and new.step.key == w.FILM_BASE and new.state == w.EDITING and new.rect == sim.BASE_ROI and new.problem == ""

    # W4 on a colour preset: the stages as the shots come in, each channel from its own colour's last shot.
    run = w.Wizard.new_preset(last, rect=sim.BASE_ROI)
    run.name, run.camera = "Portra", True
    assert run.next() and run.next() and run.next() and run.state == w.RUNNING
    shot = lambda stage, colour, clipped=0.0: Shot(stage, colour, "1/4", 120, Histogram({"RGB".index(colour): np.arange(100)}, {"RGB".index(colour): -0.2}, clipped))  # noqa: E731
    assert [state for _, state in run.stages()] == ["todo"] * 4 and run.histogram() == ({}, "")
    run.record(shot("probe", "R"))
    assert [state for _, state in run.stages()] == ["now", "todo", "todo", "todo"]
    run.record(Shot("shutter", shutter="1/4"))
    run.record(shot("trim", "R"))
    run.record(shot("trim", "G", clipped=0.0005))
    assert run.stages()[1] == ("One shutter for all colours: 1/4 s", "done") and run.stages()[2][1] == "now"
    histograms, caption = run.histogram()
    assert sorted(histograms) == [0, 1] and caption == "Last shot: green trim, level 120, 1/4 s"
    assert run.clipping() == "0.05% of the film base clipped in green."
    run.record(shot("trim", "G"))
    assert run.clipping() == "Nothing clipped."  # each colour's last shot counts: that one was kept
    run.done("Portra")
    assert [state for _, state in run.stages()] == ["done"] * 4 and run.stages()[3][0] == "Saved as Portra"
    assert run.next_label() == "Finish" and not run.can_cancel() and not run.can_back()
    rows = dict(w.result(last))
    assert rows == {"Exposure": "1/4 s · ISO 100 · f/8 by hand", "LEDs": "R 200 · G 100 · B 90", "Film base": "R 90% · G 87% · B 90%"}

    # Recalibrate…: from W2, name and film type fixed, the preset's own rectangle.
    again = w.Wizard.recalibrate(last)
    assert [step.key for step in again.steps] == [w.CAMERA, w.FILM_BASE, w.CALIBRATE] and again.fixed and again.rect == sim.BASE_ROI
    assert not again.can_back() and again.heading() == "Recalibrate · gold200 · colour" and again.position() == "Step 1 of 3 · Camera"
    # Steps inserted and appended.
    again.insert(w.CALIBRATE, w.Step("red only", "Red only"))
    again.append(w.Step("card", "Card"))
    assert [step.key for step in again.steps] == [w.CAMERA, w.FILM_BASE, "red only", w.CALIBRATE, "card"]
    # W1's note on a taken name.
    assert w.replace_note("GOLD200", "rgb", last) == "A preset named “gold200” exists. Calibrating replaces it. Rolls scanned with it keep their own copy."
    assert "It is for colour film; calibrating replaces it with a black and white preset." in w.replace_note("GOLD200", "white", last)
    assert w.replace_note("Portra", "rgb", None) == ""


# ----- scanning and rendering -----


def test_roll_layout(colour_roll):
    _, preset, roll = colour_roll
    names = sorted(p.name for p in roll.folder.iterdir())
    assert names == [".roll.lock", "negative", "preview", "raw", "roll.json", "scans"]
    assert sorted(p.name for p in (roll.folder / "scans").iterdir()) == ["Roll.01_001.tif", "Roll.01_002.tif", "Roll.01_003.tif"]
    assert sorted(p.name for p in (roll.folder / "negative").iterdir()) == ["Roll.01_001.tif", "Roll.01_002.tif", "Roll.01_003.tif"]
    assert sorted(p.name for p in (roll.folder / "preview").iterdir()) == ["Roll.01_001.npz", "Roll.01_002.npz", "Roll.01_003.npz"]
    assert sorted(p.name for p in (roll.folder / "raw").iterdir())[:3] == ["Roll.01_001_B.dng", "Roll.01_001_G.dng", "Roll.01_001_R.dng"]
    saved = json.loads((roll.folder / "roll.json").read_text())
    assert [f["number"] for f in saved["frames"]] == [1, 2, 3] and all(f["bounds"] for f in saved["frames"])
    assert saved["frames"][0]["base"] == list(Scanner.session_exposure(roll, preset)[1])  # without a flat field, a quarter stop lower


def test_flat_positive_is_inverted_neutral_and_flat(colour_roll):
    _, _, roll = colour_roll
    image = read(roll.folder / "scans" / "Roll.01_001.tif")
    assert image.shape == (2000, 3000, 3)
    steps = np.array([patch(image, box) for box in sim.wedge_boxes()])
    assert np.all(np.diff(steps[:, 1]) > 0.02)  # brighter in the scene = brighter in the file
    assert np.max(steps.max(axis=1) - steps.min(axis=1)) < 0.01  # grey stays grey: the orange mask is gone
    assert np.allclose(patch(image, sim.BASE_ROI), invert.LIFT, atol=0.01)  # film base = scene black
    assert steps[-1].max() < invert.LIFT + invert.GAIN + 0.02  # highlights keep headroom
    # Equal steps of scene exposure give equal steps in the file: no contrast curve was applied.
    slope = np.diff(steps[:, 1]) / np.diff(np.log10(sim.WEDGE))
    assert slope.max() / slope.min() < 1.25


def test_linear_negative_is_untouched(colour_roll):
    _, preset, roll = colour_roll
    image = read(roll.folder / "negative" / "Roll.01_001.tif")
    assert np.allclose(patch(image, sim.BASE_ROI), np.array(roll.frame(1).base) / FULL, atol=0.02)  # the film base where the frame says
    steps = np.array([patch(image, box) for box in sim.wedge_boxes()])
    assert np.all(np.diff(steps, axis=0) < 0)  # still a negative
    assert steps[-1, 2] < steps[-1, 0]  # and still orange: blue is the densest layer


def test_files_state_their_tone_curve(colour_roll):
    _, _, roll = colour_roll
    for name, gamma, word in (("scans/Roll.01_001.tif", b"gamma 2.2", "flat positive"), ("negative/Roll.01_001.tif", b"gamma 1", "linear negative")):
        with tifffile.TiffFile(roll.folder / name) as tif:
            page = tif.pages[0]
            assert page.dtype == np.uint16 and gamma in page.tags["InterColorProfile"].value
            assert word in page.description and "registered by" in page.description


def test_registration_recovers_the_drift(colour_roll):
    _, _, roll = colour_roll
    with tifffile.TiffFile(roll.folder / "scans" / "Roll.01_001.tif") as tif:
        text = tif.pages[0].description
    found = [float(v) for pair in text.split("registered by ")[1].split(" px")[0].split(", ") for v in pair.split("/")]
    assert np.allclose(found, [1.4, -0.6, -1.4, 0.6], atol=0.3)  # green drifted one way, blue the other


def test_registration_removes_colour_fringes(tmp_path):
    from tetherinvert.merge import merge_triplet
    from tetherinvert.raw import decode_linear

    edge = (sim.FILM[1] - 0.01, 0.3, 0.02, 0.3)  # across the holder's edge: dark to clear base
    fringe = {}
    for drift in (0.0, 3.0):
        scanner = rig((drift, 0.0))
        scanner.camera.set("shutter", "0.8")
        files = []
        for letter, colour in (("R", (190, 0, 0, 0)), ("G", (0, 96, 0, 0)), ("B", (0, 0, 76, 0))):
            scanner.light.set(*colour)
            files.append(scanner.camera.capture(tmp_path / f"{drift}_{letter}"))
        exposures = [decode_linear(f) for f in files]
        for align in (True, False):
            merged, _ = merge_triplet(*exposures, align=align)
            profile = patch_profile(merged, edge)
            fringe[drift, align] = float(np.abs(profile[:, 0] - profile[:, 2]).max())
    assert fringe[3.0, False] > 0.5  # left alone, the drift puts red and blue edges in different places
    assert fringe[3.0, True] < 0.2 and fringe[0.0, True] < 0.2  # registered, they coincide


def patch_profile(image: np.ndarray, box) -> np.ndarray:
    """Each channel's brightness across a vertical edge, scaled 0..1."""
    height, width = image.shape[:2]
    x, y, w, h = box
    strip = image[int(y * height) : int((y + h) * height), int(x * width) : int((x + w) * width)].astype(np.float32)
    profile = strip.mean(axis=0)
    return (profile - profile.min(axis=0)) / (profile.max(axis=0) - profile.min(axis=0))


def test_black_and_white(tmp_path):
    scanner = rig()
    preset = scanner.calibrate("HP5", BASE, "white")
    roll = Roll.open(tmp_path / "BW")
    roll.set_settings(rect=list(sim.PICTURE_RECT))
    frame = scanner.scan(roll, preset)
    assert frame.files == ["raw/BW_001_W.dng"]
    flat_path, negative_path = render(roll, frame, negative=True)
    flat, negative = read(flat_path), read(negative_path)
    assert flat.ndim == 2 and negative.ndim == 2 and negative_path.parent.name == "negative"  # single channel
    steps = np.array([patch(flat, box) for box in sim.wedge_boxes()])[:, 0]
    assert np.all(np.diff(steps) > 0.02) and patch(flat, sim.BASE_ROI)[0] == pytest.approx(invert.LIFT, abs=0.01)
    with tifffile.TiffFile(roll.folder / "scans" / "BW_001.tif") as tif:
        assert tif.pages[0].photometric.name == "MINISBLACK" and b"GRAY" in tif.pages[0].tags["InterColorProfile"].value[:20]
    found = preview.load(roll, 1)
    assert found.image.ndim == 2 and development.picture(plain(Roll.open(roll.folder), 1, "converted")).rgb.shape == found.image.shape + (3,)  # grey, shown as RGB
    roll.update_frame(1, develop="converted")
    (printed,) = write_prints(Roll.open(roll.folder))
    dark, light = picture_ends(read(printed))
    assert read(printed).ndim == 2 and dark[0] < 0.002 and light[0] == pytest.approx(invert.WHITE_FLAT, abs=0.002)


def test_retake_replaces_the_last_frame(tmp_path):
    scanner = rig()
    preset = scanner.calibrate("film", BASE)
    roll = Roll.open(tmp_path / "R")
    scanner.scan(roll, preset)
    first = roll.folder / "raw" / "R_002_R.dng"
    scanner.scan(roll, preset)
    before = first.read_bytes()
    scanner.scan(roll, preset, retake=True)
    assert [f.number for f in Roll.open(roll.folder).frames] == [1, 2]
    assert first.read_bytes() != before and not list((roll.folder / "raw").glob(".scan-*"))
    assert scanner.light.color == preset.framing_light()  # back to the dim framing light


def test_frame_numbers_are_spelled_as_on_the_film():
    assert [paths.stem("R", n) for n in (-1, 0, 1, 12, 100)] == ["R_00", "R_000", "R_001", "R_012", "R_100"]
    assert sorted(f"{paths.stem('R', n)}.tif" for n in (2, 0, 1, -1)) == ["R_00.tif", "R_000.tif", "R_001.tif", "R_002.tif"]  # film order by name
    assert [paths.label(n) for n in (-1, 0, 7)] == ["00", "0", "7"]
    for number in (-1, 0, 1, 37):
        assert paths.parse(paths.label(number)) == number
    for bad in ("x", "-2", ""):
        with pytest.raises(ValueError):
            paths.parse(bad)


def test_a_roll_started_at_00_counts_on_and_a_frame_left_out_leaves_a_gap(tmp_path):
    scanner = rig()
    preset = scanner.calibrate("film", BASE)
    roll = Roll.open(tmp_path / "R15")
    roll.set_next(-1)  # the first-frame question, answered 00
    for _ in range(3):
        render(roll, scanner.scan(Roll.open(roll.folder), preset))
    roll = Roll.open(roll.folder)
    assert [f.number for f in roll.frames] == [-1, 0, 1] and roll.next is None and roll.next_number() == 2
    assert sorted(p.name for p in (roll.folder / "scans").iterdir()) == ["R15_00.tif", "R15_000.tif", "R15_001.tif"]
    assert sorted(p.name for p in (roll.folder / "preview").iterdir()) == ["R15_00.npz", "R15_000.npz", "R15_001.npz"]
    assert {p.name.rsplit("_", 1)[0] for p in (roll.folder / "raw").iterdir()} == {"R15_00", "R15_000", "R15_001"}
    roll.set_next(3)  # frame 2 was left out on the film
    assert scanner.scan(Roll.open(roll.folder), preset).number == 3
    assert Roll.open(roll.folder).next_number() == 4
    assert scanner.scan(Roll.open(tmp_path / "Fresh"), preset).number == 1  # no answer (skipped, or the CLI): from 1


class CameraGone(RuntimeError):
    pass


def test_rescanning_a_frame_in_the_middle_keeps_what_was_made_of_it(tmp_path):
    scanner = rig()
    preset = scanner.calibrate("film", BASE)
    folder = tmp_path / "R"
    for _ in range(4):
        render(Roll.open(folder), scanner.scan(Roll.open(folder), preset))
    Roll.open(folder).update_frame(2, adjust={"m": 2}, develop="converted")
    write_prints(Roll.open(folder))
    old = (folder / "raw" / "R_002_R.dng").read_bytes()

    frame = scanner.scan(Roll.open(folder), preset, orientation={"turn": 90, "mirror": False}, number=2)  # Replace frame 2
    render(Roll.open(folder), frame)
    roll = Roll.open(folder)
    again = roll.frame(2)
    assert frame.number == 2 and [f.number for f in roll.frames] == [1, 2, 3, 4]
    assert again.adjust == {"m": 2} and again.develop == "converted"  # its corrections and base stay
    assert again.orientation == {"turn": 90, "mirror": False}  # the way up is the new scan's
    assert (folder / "raw" / "R_002_R.dng").read_bytes() != old
    assert due(roll, 2) and not due(roll, 3)  # its print is due again, only its
    assert roll.next is None and roll.next_number() == 5  # the counter is back after the last frame

    # A rescan that fails half-way leaves the frame as it was.
    kept = {name: (folder / name).read_bytes() for name in roll.frame(3).files}
    capture = scanner.camera.capture

    def failing(stem, check=True):
        if str(stem).endswith("_G"):
            raise CameraGone("the camera went away")
        return capture(stem, check)

    scanner.camera.capture = failing
    with pytest.raises(CameraGone):
        scanner.scan(Roll.open(folder), preset, number=3)
    assert {name: (folder / name).read_bytes() for name in roll.frame(3).files} == kept
    assert not list((folder / "raw").glob(".scan-*")) and Roll.open(folder).next_number() == 5


def test_the_counter_steps_only_onto_free_numbers():
    from tetherinvert.session import free_number

    taken = {-1, 0, 1, 3, 4}
    assert free_number(taken, 5, -1) == 2  # a gap left by a deleted frame
    assert free_number(taken, 2, -1) is None  # nothing free down to 00
    assert free_number(taken, 2, +1) == 5
    assert free_number(set(), 0, -1) == -1 and free_number(set(), -1, -1) is None  # the film starts at 00


def test_deleting_the_last_frame_gives_its_number_back_and_one_in_the_middle_does_not(tmp_path):
    from tetherinvert.session import delete_frame

    scanner = rig()
    preset = scanner.calibrate("film", BASE)
    folder = tmp_path / "R"
    for _ in range(4):
        scanner.scan(Roll.open(folder), preset)
    delete_frame(Roll.open(folder), 2)
    assert Roll.open(folder).next_number() == 5
    Roll.open(folder).set_next(9)
    delete_frame(Roll.open(folder), 4)
    assert Roll.open(folder).next_number() == 4


def test_the_command_line_takes_frames_as_on_the_film(tmp_path, capsys):
    base = ",".join(str(v) for v in sim.BASE_ROI)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    assert cli(["--sim", "scan", str(tmp_path / "R"), "--preset", "Gold", "--number", "00"]) == 0
    assert cli(["--sim", "scan", str(tmp_path / "R"), "--preset", "Gold"]) == 0
    assert [f.number for f in Roll.open(tmp_path / "R").frames] == [-1, 0]
    assert "frame 0 done" in capsys.readouterr().out
    assert cli(["delete", str(tmp_path / "R"), "00", "--yes"]) == 0
    assert "of frame 00" in capsys.readouterr().out and [f.number for f in Roll.open(tmp_path / "R").frames] == [0]
    assert cli(["render", str(tmp_path / "R"), "--frame", "0"]) == 0  # frame 0 is a frame, not "every frame"
    assert capsys.readouterr().out.strip().endswith("R_000.tif")
    with pytest.raises(SystemExit):  # argparse refuses it
        cli(["delete", str(tmp_path / "R"), "x", "--yes"])
    assert "not a frame number" in capsys.readouterr().err


def own_copy(colour_roll, tmp_path) -> Roll:
    """The shared roll, copied: for tests that write into it."""
    import shutil

    shutil.copytree(colour_roll[2].folder, tmp_path / "Roll.01")
    return Roll.open(tmp_path / "Roll.01")


def test_a_roll_moved_under_another_name_keeps_finding_and_naming_its_files_by_its_own_name(colour_roll, tmp_path):
    """A roll moved into another folder (Giro2 into Giro/2) keeps its name: its files are found and
    named as Giro2_001, not 2_001."""
    from tetherinvert.session import delete_frame

    copy = own_copy(colour_roll, tmp_path)
    assert json.loads((copy.folder / "roll.json").read_text())["name"] == "Roll.01"  # given when roll.json was first written
    moved = tmp_path / "Giro" / "2"
    moved.parent.mkdir()
    copy.folder.rename(moved)
    roll = Roll.open(moved)
    assert roll.name == "Roll.01" and preview.load(roll, 1) is not None
    assert paths.tiff(roll, 1, "flat") == moved / "scans" / "Roll.01_001.tif" and paths.contact_sheet(roll) == moved / "Roll.01_contact.pdf"
    write_prints(roll)
    assert sorted(p.name for p in (moved / "prints").iterdir()) == ["Roll.01_001.tif", "Roll.01_002.tif", "Roll.01_003.tif"]
    render(roll, rig().scan(roll, colour_roll[1]))  # a frame scanned into the moved roll is named like the others
    assert Roll.open(moved).frames[-1].files[0].startswith("raw/Roll.01_004_") and paths.tiff(roll, 4, "flat").exists() and preview.load(roll, 4)
    assert {p.name for p in delete_frame(Roll.open(moved), 3)} >= {"Roll.01_003.tif", "Roll.01_003.npz", "Roll.01_003_R.dng"}
    assert not list(moved.rglob("2_*")) and Roll.open(moved).name == "Roll.01"  # nothing named after the folder, and the name kept through every change
    assert Roll(tmp_path / "New").name == "New"  # a new roll: its folder's name


def test_a_matched_roll_is_printed_with_one_stretch(colour_roll, tmp_path):
    roll = own_copy(colour_roll, tmp_path)
    assert len({tuple(f.bounds["dense"]) for f in roll.frames}) == 3  # each frame measured its own highlights
    write_prints(roll)
    assert len({dev.points.dense for dev in development.of_roll(roll).values()}) == 3  # each frame developed with its own points
    assert [p.name for p in sorted((roll.folder / "prints").iterdir())] == ["Roll.01_001.tif", "Roll.01_002.tif", "Roll.01_003.tif"]
    assert not development.of_roll(Roll.open(roll.folder)).due
    roll = Roll.open(roll.folder)
    roll.set_settings(matched=True)
    assert development.of_roll(roll).due == {1, 2, 3}  # matching changes every print
    written = write_prints(Roll.open(roll.folder))
    with tifffile.TiffFile(written[1]) as tif:
        assert "points shared across the roll" in tif.pages[0].description
    # One set of points for all, and a patch of known density comes out the same in every frame.
    assert len({dev.points for dev in development.of_roll(Roll.open(roll.folder)).values()}) == 1
    shared = [patch(read(p), sim.wedge_boxes()[5]) for p in written]
    assert np.ptp(shared, axis=0).max() < 0.004


def test_the_window_shows_each_frame_as_its_print_will_be(colour_roll):
    """The window's pictures are developed from the preview as the prints are from the flat TIFF."""
    _, _, roll = colour_roll
    found = preview.load(roll, 1)
    assert found is not None and found.image.shape == (1067, 1600, 3) and found.image.dtype == np.uint16
    dev = plain(roll, 1)
    flat = development.picture(dev)
    full = development.picture(dev, None)  # from the flat TIFF, as the print
    assert flat.rgb.shape == (1067, 1600, 3) and flat.rgb.dtype == np.uint8 and full.values.shape == (2000, 3000, 3)
    assert np.abs(flat.values - preview.reduce(full.values)).mean() < 1 / 255  # differs only by reducing before rather than after developing
    assert np.allclose(patch(flat.values, sim.BASE_ROI), invert.LIFT, atol=0.01)  # film base where the flat TIFF has it
    converted = development.picture(replace(dev, base="converted")).values
    assert picture_ends(converted)[0].max() < 0.01  # the picture's first tone at 0
    assert development.picture(dev, 240).rgb.shape == (160, 240, 3)
    # The flat TIFF is an exact copy of the log density: developed back to flat, it is itself.
    tiff = tifffile.imread(roll.folder / "scans" / "Roll.01_001.tif")
    assert np.abs(full.values * FULL - tiff).max() <= 1.0
    with pytest.raises(ValueError, match="no base called"):
        development.picture(replace(dev, base="print"))


def test_a_development_is_what_the_print_was_made_with_and_unsaved_corrections_count(colour_roll, tmp_path):
    roll = own_copy(colour_roll, tmp_path)
    write_prints(roll)
    roll = Roll.open(roll.folder)
    frame, dev = roll.frame(2), development.of_roll(roll)[2]
    points = [[round(v, 5) for v in frame.bounds[key]] for key in ("base", "dense", "thin")]
    record = {"d": 0, "c": 0, "m": 0, "y": 0, "grad": 0, "sat": 0, "tone": "standard", "develop": "flat", "points": points, "rendered": frame.rendered}
    assert dev.made_with == record == frame.printed and list(dev.made_with) == list(record)  # as roll.json keeps it, so no print goes stale
    # The window's corrections, not saved yet: the frame is due, and nothing is written or changed.
    stored = (roll.folder / "roll.json").read_text()
    window = development.of_roll(roll, {2: {"d": 4}})
    assert window.due == {2} and window[2].adjust["d"] == 4 and roll.frame(2).adjust is None
    assert (roll.folder / "roll.json").read_text() == stored
    # C held: the same frame without them.
    assert np.array_equal(development.picture(window[2].corrected(None)).values, development.picture(dev).values)
    assert hash(window[2]) != hash(dev) and window[2].corrected(None) == dev  # a value: it crosses to the preview thread as it is


def test_a_turned_frame_is_shown_turned_from_every_source(colour_roll, tmp_path):
    from tetherinvert import orient
    from tetherinvert.session import reorient_frame

    roll = own_copy(colour_roll, tmp_path)
    turn = {"turn": 90, "mirror": False}
    reorient_frame(roll, 1, turn)
    dev = development.of_roll(Roll.open(roll.folder))[1]
    assert dev.orientation == turn
    shown, small, full = development.picture(dev), development.picture(dev, 240), development.picture(dev, None)
    aspect = shown.values.shape[0] / shown.values.shape[1]
    assert aspect > 1 and small.values.shape == (240, 160, 3)  # portrait, as the operator has it
    assert abs(full.values.shape[0] / full.values.shape[1] - aspect) < 0.01  # the TIFF is written turned and not turned again
    assert np.abs(shown.values - preview.reduce(full.values)).mean() < 1 / 255
    assert shown.outline == pytest.approx(orient.rect(sim.PICTURE_RECT, turn))  # the picture area outlined where it is seen
    cut, cut_full = development.picture(dev, 400, cut=True), development.picture(dev, None, cut=True)
    x, y, w, h = orient.rect(sim.PICTURE_RECT, turn)
    assert cut.outline is None and abs(cut.values.shape[0] / cut.values.shape[1] - h * 3000 / (w * 2000)) < 0.02
    assert abs(cut_full.values.shape[0] / cut_full.values.shape[1] - cut.values.shape[0] / cut.values.shape[1]) < 0.02


def test_a_preview_written_again_is_seen_although_its_base_is_kept(colour_roll, tmp_path):
    roll = own_copy(colour_roll, tmp_path)
    dev = plain(roll, 1)
    before = development.picture(dev).values.mean()
    assert development.picture(dev).values.mean() == before  # kept: the same base
    found = preview.load(roll, 1)
    preview.write(roll, 1, found.image // 2, found.bounds, rect=found.rect)  # as a render would: the same file name, new pixels
    assert development.picture(dev).values.mean() > before + 0.01  # half the light: a thinner negative, a lighter positive


def test_a_frame_without_points_or_files_has_no_picture_and_no_print_due(colour_roll, tmp_path):
    roll = own_copy(colour_roll, tmp_path)
    roll.update_frame(1, bounds=None)  # scanned, not rendered yet
    preview.path(roll, 2).unlink()
    (roll.folder / "scans" / "Roll.01_003.tif").unlink()
    developments = development.of_roll(Roll.open(roll.folder))
    assert developments.due == set() and developments[1].points is None and developments[1].base == "flat"
    with pytest.raises(development.Undeveloped, match="frame 1 is not rendered yet"):
        development.picture(developments[1])
    with pytest.raises(development.Undeveloped, match=r"frame 2 has no preview \(Roll.01_002.npz\)"):
        development.picture(developments[2])
    with pytest.raises(development.Undeveloped, match=r"frame 3 has no flat TIFF \(Roll.01_003.tif\)"):
        development.picture(developments[3], None)
    assert development.picture(developments[3]).values.shape == (1067, 1600, 3)  # its preview is still there


def picture_ends(image: np.ndarray) -> np.ndarray:
    """Darkest and lightest tone inside the picture, per channel, metered the way the program meters."""
    cells = invert._grid(image, sim.PICTURE_RECT)
    return np.percentile(cells.reshape(-1, cells.shape[-1]), [invert.CLIP_PERCENT, 100 - invert.CLIP_PERCENT], axis=0)


def test_the_converted_base_starts_at_the_first_picture_tone(colour_roll, tmp_path):
    """What is otherwise done by hand with a tone curve per channel: black at the first data."""
    roll = own_copy(colour_roll, tmp_path)
    roll.update_frame(1, develop="converted")
    printed = dict(zip((1, 2, 3), write_prints(Roll.open(roll.folder)), strict=True))
    flat, converted = read(roll.folder / "scans" / "Roll.01_001.tif"), read(printed[1])

    # The flat file starts at the film base (10 %); the picture's own first tone lies well above it.
    assert np.all(picture_ends(flat)[0] > invert.LIFT + 0.05)
    # The converted print starts at zero in every channel and leaves white where the flat file has it.
    dark, light = picture_ends(converted)
    assert np.all(dark < 0.002) and np.allclose(light, invert.WHITE_FLAT, atol=0.002)
    steps = np.array([patch(converted, box) for box in sim.wedge_boxes()])
    assert np.all(np.diff(steps[:, 1]) > 0.02) and np.max(steps.max(axis=1) - steps.min(axis=1)) < 0.01  # in order, and grey stays grey
    slope = np.diff(steps[:, 1]) / np.diff(np.log10(sim.WEDGE))
    assert slope.max() / slope.min() < 1.25  # still no contrast curve
    with tifffile.TiffFile(printed[1]) as tif:
        assert "Roll.01_001.tif as converted (its own points)" in tif.pages[0].description and tif.pages[0].bitspersample == 16
    assert np.abs(read(printed[2]) - read(roll.folder / "scans" / "Roll.01_002.tif")).max() <= 1 / FULL  # flat base, no corrections: the scan itself

    # The white point too: the same picture, taken up to 98 %.
    roll.update_frame(1, develop="converted white")
    write_prints(Roll.open(roll.folder))
    stretched = read(printed[1])
    assert np.allclose(picture_ends(stretched)[1], invert.WHITE_FULL, atol=0.002)
    assert np.abs(stretched - np.clip(converted * invert.WHITE_FULL / invert.WHITE_FLAT, 0, 1)).max() < 3 / FULL

    # A matched roll shares the black point as well: a known tone comes out the same in every frame.
    for number in (1, 2, 3):
        roll.update_frame(number, develop="converted")
    roll = Roll.open(roll.folder)
    roll.set_settings(matched=True)
    values = [patch(read(p), sim.wedge_boxes()[5]) for p in write_prints(Roll.open(roll.folder))]
    assert np.ptp(values, axis=0).max() < 0.004
    assert len({tuple(f.bounds["thin"]) for f in Roll.open(roll.folder).frames}) == 3  # though each frame has its own first tone

    # Without a picture area the brightest thing in the frame is the holder's window, so the film base stands in.
    frame = Roll.open(roll.folder).frame(1)
    log_image = invert.to_log(tifffile.imread(roll.folder / "negative" / "Roll.01_001.tif"))
    loose = invert.measure(log_image, None, frame.base)
    marked = invert.measure(log_image, sim.PICTURE_RECT, frame.base)
    assert loose.thin == loose.base and all(t < b - 0.1 for t, b in zip(marked.thin, marked.base, strict=True))
    alike = invert.measure(log_image, sim.PICTURE_RECT, frame.base, per_channel=False)
    assert np.ptp(np.subtract(alike.base, alike.thin)) < 1e-5  # one density range for all channels: one offset for the black point


def test_crop_and_common_range(colour_roll, tmp_path):
    _, _, source = colour_roll
    import shutil

    shutil.copytree(source.folder, tmp_path / "Roll.01")
    roll = Roll.open(tmp_path / "Roll.01")
    roll.set_settings(crop=True, per_channel=False)
    render(roll, roll.frame(1))
    image = read(roll.folder / "scans" / "Roll.01_001.tif")
    x, y, w, h = sim.PICTURE_RECT
    assert image.shape[:2] == (int((y + h) * 2000) - int(y * 2000), int((x + w) * 3000) - int(x * 3000))
    steps = np.array([patch(image, ((i + 0.2) / 8, 0.875, 0.075, 0.0875)) for i in range(8)])  # the wedge, in cropped terms
    assert np.all(np.diff(steps[:, 1]) > 0.02)
    # One density range for all channels: the dye layers' different contrast shows as a cast in the highlights.
    spread = steps.max(axis=1) - steps.min(axis=1)
    assert spread[-1] > 0.05 and spread[0] < spread[-1]


def test_the_preview_shows_what_a_cropped_file_holds(colour_roll, tmp_path):
    """The preview covers what the file covers: only the picture area when the file is cropped."""
    roll = own_copy(colour_roll, tmp_path)
    assert preview.load(roll, 1).cropped is False
    roll.set_settings(crop=True)
    render(roll, roll.frame(1))
    found, tiff = preview.load(roll, 1), read(roll.folder / "scans" / "Roll.01_001.tif")
    assert found.cropped and found.rect == tuple(roll.rect)
    assert abs(found.image.shape[1] / found.image.shape[0] - tiff.shape[1] / tiff.shape[0]) < 0.01
    shown = development.picture(plain(roll, 1))
    assert abs(shown.values.mean() - tiff.mean()) < 0.01 and shown.outline is None  # the same picture, not more of it
    assert development.picture(plain(roll, 1), None).values.shape == tiff.shape


# ----- flat field -----


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch):
    """Nothing a test learns or saves reaches the real ~/.tetherinvert."""
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))


FALLOFF = (0.20, 0.15, 0.25)
REBATES = {"left": sim.BASE_ROI, "right": (0.94, 0.30, 0.02, 0.40), "top": (0.2, 0.055, 0.6, 0.03), "bottom": (0.2, 0.915, 0.6, 0.03)}


def uneven_rig():
    light = sim.SimLight()
    return Scanner(light, sim.SimCamera(light, falloff=FALLOFF), settle_s=0.0)


def base_levels(path: Path) -> np.ndarray:
    """Median film base at the four rebates of a rendered file, one row per rebate."""
    image = read(path)
    height, width = image.shape[:2]
    rows = []
    for x, y, w, h in REBATES.values():
        area = image[int(y * height) : int((y + h) * height), int(x * width) : int((x + w) * width)]
        rows.append(np.median(area.reshape(-1, image.shape[2] if image.ndim == 3 else 1), axis=0))
    return np.array(rows)


@pytest.mark.parametrize("kind", ["rgb", "white"])
def test_flat_field_evens_out_the_frame(tmp_path, kind):
    from tetherinvert.flatfield import FlatField, FlatFieldError
    from tetherinvert.raw import decode_linear

    scanner = uneven_rig()
    preset = scanner.calibrate("film", BASE, kind)
    assert preset.roi == sim.BASE_ROI
    roll = Roll.open(tmp_path / "R")
    roll.set_settings(rect=list(sim.PICTURE_RECT))

    with pytest.raises(FlatFieldError, match="Take the film out"):  # the operator forgot
        scanner.capture_flatfield(roll, preset)
    assert FlatField.load(roll.folder) is None
    assert scanner.camera.get("shutter") == preset.shutter
    shots, capture = [], scanner.camera.capture
    scanner.camera.capture = lambda *args, **kwargs: shots.append(1) or capture(*args, **kwargs)
    with sim.film_removed(scanner.camera):
        flat = scanner.capture_flatfield(roll, preset)
        # Any clean exposure of the bare light will do, so it is not trimmed onto a target:
        # at most one metering shot per colour the first time, and none once it is known.
        colours, first = (1 if kind == "white" else 3), len(shots)
        assert colours <= first <= 2 * colours
        again = scanner.capture_flatfield(Roll.open(tmp_path / "Again"), preset)
        assert len(shots) - first == colours and np.allclose(again.maps, flat.maps, atol=0.01)
    scanner.camera.capture = capture
    # Back to framing: the bare light was shot far faster than film, and the live view shows the
    # set exposure, so a shutter left there would make framing too dark until the first scan.
    assert scanner.light.color == preset.framing_light()
    assert scanner.camera.get("shutter") == preset.shutter

    # The stored map is the light's real unevenness, inside the holder's window.
    height, width = flat.maps.shape[:2]
    truth = sim.illumination(height, width, FALLOFF)
    top, left, bottom, right = sim.FILM
    gate = (slice(int(top * height) + 6, int(bottom * height) - 6), slice(int(left * width) + 6, int(right * width) - 6))
    for channel in range(3):
        expected = truth[..., channel][gate] / truth[..., channel][gate].max()
        assert np.abs(flat.maps[..., channel][gate] / expected - 1).max() < 0.01
    assert 0.15 < flat.falloff() < 0.30 and FlatField.load(roll.folder).kind == kind

    # The session exposes a little lower than the preset: the base was metered at the dim edge,
    # and the brightest part of the frame is what must stay under the ceiling.
    levels, base = scanner.session_exposure(roll, preset)
    lit = [3] if kind == "white" else [0, 1, 2]
    assert all(levels[i] < preset.levels[i] for i in lit) and all(b < p for b, p in zip(base, preset.base, strict=True))
    frame = scanner.scan(roll, preset)
    assert frame.base == list(base) and frame.roi == list(preset.roi)
    brightest = [decode_linear(roll.folder / name, half=True) for name in frame.files]
    x, y, w, h = REBATES["right"]
    peak = max(float(np.percentile(r[int(y * r.shape[0]) : int((y + h) * r.shape[0]), int(x * r.shape[1]) : int((x + w) * r.shape[1])], 99.9)) for r in brightest)
    assert 0.75 * FULL < peak < 0.95 * FULL  # exposed to the right, nothing clipped

    flat_positive, negative = render(roll, frame, negative=True)
    even = base_levels(negative)
    assert np.ptp(even, axis=0).max() / even.mean() < 0.015  # the base is the same at all four rebates
    assert np.allclose(even[0], np.array(base[: even.shape[1]] if kind == "rgb" else [np.dot(invert.LUMA, base)]) / FULL, rtol=0.03)
    assert np.allclose(base_levels(flat_positive), invert.LIFT, atol=0.006)
    with tifffile.TiffFile(flat_positive) as tif:
        assert "flat-field corrected" in tif.pages[0].description

    # On request every file is written a second time without the correction, to compare.
    roll.set_settings(compare=True)
    written = render(roll, frame, negative=True)
    assert [str(path.relative_to(roll.folder)) for path in written] == ["scans/R_001.tif", "negative/R_001.tif", "scans/R_001_noflat.tif", "negative/R_001_noflat.tif"]
    assert Roll.open(roll.folder).compare
    assert np.array_equal(base_levels(written[1]), even)  # the corrected files are what they were
    as_shot = base_levels(written[3])
    assert np.ptp(as_shot, axis=0).max() / as_shot.mean() > 0.04  # the light's unevenness, as the camera saw it
    assert np.allclose(as_shot[0], even[0], rtol=0.02)  # the pair agrees where the preset was calibrated...
    assert np.ptp(base_levels(written[2]), axis=0).max() > 3 * np.ptp(base_levels(written[0]), axis=0).max()  # ...and not away from it
    described = []
    for path in written:
        with tifffile.TiffFile(path) as tif:
            described.append(tif.pages[0].description)
    assert all("NOT flat-field corrected (comparison copy)" in text for text in described[2:])
    assert not any("NOT" in text for text in described[:2])
    assert described[2].split("NOT")[0] == described[0].split("flat-field")[0]  # registered identically

    roll.set_settings(flatfield=False)  # the same exposures without the correction: the unevenness is back
    alone = render(roll, frame, negative=True)
    uneven = base_levels(alone[1])
    assert np.ptp(uneven, axis=0).max() / uneven.mean() > 0.04
    # Nothing to compare with now: no copies, and those left from before, which no longer have a partner, are gone.
    assert alone == written[:2] and not written[2].exists() and not written[3].exists()


@pytest.mark.parametrize("kind", ["rgb", "rgb-together"])
def test_without_a_flat_field_a_brighter_middle_does_not_clip(tmp_path, kind):
    """A light a quarter stop brighter in the middle of the frame than at the film base rectangle on
    its edge, as on the tested rig: a roll without a flat field is lit a quarter stop lower, the film
    base scaled with it, so no frame clips in green."""
    from tetherinvert.session import NO_FLAT_MARGIN, clipping

    light = sim.SimLight()
    scanner = Scanner(light, sim.SimCamera(light, falloff=(0.5, 0.5, 0.5)), settle_s=0.0)
    corner = Roi(0.035, 0.05, 0.025, 0.10)  # the left rebate near the top: about 1.25 times dimmer than the brightest rebate
    preset = scanner.calibrate("film", corner, kind)
    roll = Roll.open(tmp_path / "R")  # no picture area: the rebate all round is checked, the film base at its brightest
    levels, base = scanner.session_exposure(roll, preset)
    assert NO_FLAT_MARGIN == pytest.approx(2 ** -0.25)
    assert all(abs(new - old * NO_FLAT_MARGIN) <= 0.5 for new, old in zip(levels, preset.levels, strict=True))
    assert np.allclose(np.array(base) / np.array(preset.base), NO_FLAT_MARGIN, rtol=0.01)
    frame = scanner.scan(roll, preset)
    assert frame.base == list(base) and clipping(roll, frame) is None


# ----- rgb together -----


def test_a_together_preset_keeps_its_levels_when_the_lights_default_changes(tmp_path):
    """Scanning uses the levels read at calibration, never the light's default as it is now: a
    default changed later in the web app must not change a roll half-way."""
    scanner = rig()
    preset = scanner.calibrate("film", BASE, "rgb-together")
    scanner.light.default = (90, 255, 40)
    lit, original = [], scanner.light.set
    scanner.light.set = lambda *levels: lit.append(tuple(levels)) or original(*levels)
    roll = Roll.open(tmp_path / "R")
    frame = scanner.scan(roll, preset)
    assert frame.files == ["raw/R_001_RGB.dng"] and frame.kind == "rgb-together"
    levels, _base = scanner.session_exposure(roll, preset)  # the preset's levels, a quarter stop lower without a flat field
    assert lit[0] == levels and lit[-1] == preset.framing_light() and len(lit) == 2  # one exposure, then back to framing
    assert levels[1] < levels[0] < levels[2]  # in the preset's proportions, not the new default's (G 255 the brightest)


class SerialRig(FakeSerial):
    """A Scanlight over a fake port that answers as the firmware does and lights the simulated
    film: every packet the program sends is kept."""

    def __init__(self, light: sim.SimLight) -> None:
        super().__init__()
        self.light = light

    def write(self, data: bytes) -> None:
        super().write(data)
        header = data[1]
        if header == 0:
            self.light.set(*data[3:7])
        elif header == 1:
            self._replies += packet(4, bytes(self.light.default))
        elif header == 2:
            self._replies += packet(3, ((1 << 16) | 3).to_bytes(4, "big"))


def test_the_lights_colour_default_is_never_overwritten(tmp_path):
    """Only a SET_COLOR with save not 0 changes the stored default. Calibrating, the flat field and
    scanning all send save = 0."""
    simulated = sim.SimLight()
    port = SerialRig(simulated)
    scanner = Scanner(Scanlight(serial_obj=port), sim.SimCamera(simulated), settle_s=0.0)
    preset = scanner.calibrate("film", BASE, "rgb-together")
    roll = Roll.open(tmp_path / "R")
    with sim.film_removed(scanner.camera):
        scanner.capture_flatfield(roll, preset)
    scanner.scan(roll, preset)
    colours = [data for data in port.written if data[1] == 0]
    assert len(colours) > 5 and all(data[2] == 6 and data[8] == 0 for data in colours)
    assert bytes([0xFE, 1, 0]) in port.written and preset.levels == (*sim.COLOUR_DEFAULT, 0)


def test_a_together_roll_is_flat_fielded_by_one_factor_and_renders_without_a_merge(tmp_path):
    from tetherinvert.flatfield import FlatField
    from tetherinvert.session import clipping

    scanner = uneven_rig()
    preset = scanner.calibrate("film", BASE, "rgb-together")
    roll = Roll.open(tmp_path / "R")
    roll.set_settings(rect=list(sim.PICTURE_RECT))
    shots, capture = [], scanner.camera.capture
    scanner.camera.capture = lambda *args, **kwargs: shots.append(1) or capture(*args, **kwargs)
    with sim.film_removed(scanner.camera):
        flat = scanner.capture_flatfield(roll, preset)
    scanner.camera.capture = capture
    assert flat.kind == "rgb-together" and FlatField.load(roll.folder).maps.shape[2] == 3 and 1 <= len(shots) <= 2  # one exposure lights all three
    assert 0.15 < flat.falloff() < 0.30 and scanner.light.color == preset.framing_light()

    # One factor for all three LEDs, the smallest of the channels': the default's balance stays.
    levels, base = scanner.session_exposure(roll, preset)
    factor = min(flat.at(preset.roi))
    assert levels[3] == 0 and all(abs(new - old * factor) <= 0.5 for new, old in zip(levels[:3], preset.levels[:3], strict=True))
    assert all(new < old for new, old in zip(levels[:3], preset.levels[:3], strict=True))
    assert np.allclose(np.array(base) / np.array(preset.base), base[2] / preset.base[2], rtol=1e-9)
    frame = scanner.scan(roll, preset)
    assert frame.base == list(base) and frame.files == ["raw/R_001_RGB.dng"] and clipping(roll, frame) is None

    flat_positive, negative = render(roll, frame, negative=True)
    with tifffile.TiffFile(flat_positive) as tif:
        described = tif.pages[0].description
    assert "red, green and blue lit together" in described and "registered" not in described and "flat-field corrected" in described
    even = base_levels(negative)
    assert np.ptp(even, axis=0).max() / even.mean() < 0.02  # the base the same at all four rebates
    assert np.allclose(base_levels(flat_positive), invert.LIFT, atol=0.01)  # inverted with the base where the preset says
    assert "colour default R 200 G 170 B 230 together" in xmp_of(flat_positive)["scan:Light"]


def test_a_together_frame_with_more_light_than_at_calibration_is_reported_clipped(tmp_path):
    from tetherinvert.session import clipping

    scanner = rig()
    preset = scanner.calibrate("film", BASE, "rgb-together")
    roll = Roll.open(tmp_path / "R")
    brighter = replace(preset, shutter=sim.SHUTTERS[sim.SHUTTERS.index(preset.shutter) - 3])  # a stop more
    warning = clipping(roll, scanner.scan(roll, brighter))
    assert warning is not None and warning.startswith("Frame 1 is clipped (blue ")


# ----- own light -----

NOTE = "Kaiser slimlite plano, full"


def own_rig(spectrum: str = "colour", brightness: float = 1.0, light=None, falloff=(0.0, 0.0, 0.0)):
    """The simulated rig with the operator's own light: TetherInvert gets NoLight (or `light`, a
    Scanlight that must be left alone), the film is lit by the bench's lamp, set as `spectrum`."""
    from tetherinvert.session import NoLight

    lamp = sim.SimOwnLight(brightness)
    lamp.show(spectrum)
    return Scanner(light if light is not None else NoLight(), sim.SimCamera(lamp, falloff=falloff), settle_s=0.0)


@pytest.mark.parametrize("kind, spectrum", [("own-colour", "colour"), ("own-colour", "white"), ("own-bw", "white")])
def test_an_own_light_is_calibrated_by_its_shutter_alone(kind, spectrum):
    """The light's setting is the operator's: only the shutter is found, the brightest channel's
    film base 0 to 1/3 stop under the target, nothing clipped; the light is a by-hand setting."""
    from tetherinvert.calibrate import CLIP_BUDGET, TOLERANCE

    scanner, steps = own_rig(spectrum), []
    preset = scanner.calibrate("film", BASE, kind, light_note=NOTE, progress=steps.append)
    assert preset.kind == kind and preset.levels == (0, 0, 0, 0) and preset.light_note == NOTE and "light" in preset.by_hand
    assert TARGET * 2 ** (-1 / 3) * (1 - TOLERANCE) <= max(preset.base) / FULL <= TARGET * (1 + 2 * TOLERANCE)
    shots = [step for step in steps if step.histogram is not None]
    assert shots[-1].histogram.clipped <= CLIP_BUDGET and all(shot.colour == "O" and shot.level == 0 for shot in shots)
    assert ("Light", NOTE, True) in preset.settings()  # reminded of like the f-stop
    assert preset.framing_light() is None  # nothing to light while framing


def test_an_own_light_preset_sends_nothing_to_a_connected_scanlight(tmp_path):
    """The Scanlight may be plugged in while an own light lights the film: it is left alone, not even switched off."""
    scanlight = sim.SimLight()
    sent, original = [], scanlight.set
    scanlight.set = lambda *levels: sent.append(levels) or original(*levels)
    scanlight.off = lambda: sent.append("off")
    scanner = own_rig(light=scanlight)
    preset = scanner.calibrate("film", BASE, "own-colour", light_note=NOTE)
    roll = Roll.open(tmp_path / "R")
    scanner.scan(roll, preset)
    assert sent == []


def own_rgb_preset(scanner, roi=BASE):
    """An own-rgb preset as the operator makes it: each colour shot with the light set to it alone."""
    lamp = scanner.camera._light
    shots = {}
    for letter in "RGB":
        lamp.show(letter)
        shots[letter] = scanner.calibrate_colour(roi, letter)
    lamp.show("colour")
    return scanner.assemble("film", roi, "own-rgb", shots, light_note=NOTE)


def test_own_rgb_gives_each_colour_its_own_shutter_with_the_light_at_full():
    from tetherinvert.calibrate import TOLERANCE

    scanner = own_rig()
    preset = own_rgb_preset(scanner)
    assert preset.kind == "own-rgb" and sorted(preset.shutters) == ["B", "G", "R"] and preset.shutter == preset.shutters["G"]
    assert len(set(preset.shutters.values())) > 1  # the colours differ through the orange mask: red needs the shortest
    for channel in range(3):
        assert TARGET * 2 ** (-1 / 3) * (1 - TOLERANCE) <= preset.base[channel] / FULL <= TARGET * (1 + 2 * TOLERANCE), channel
    assert ("Shutter", f"R {preset.shutters['R']} · G {preset.shutters['G']} · B {preset.shutters['B']} s", False) in preset.settings()


def test_a_wrong_colour_at_a_calibration_prompt_is_refused():
    from tetherinvert.calibrate import WrongColour

    scanner = own_rig()
    scanner.camera._light.show("G")
    with pytest.raises(WrongColour, match="That did not look like red only"):
        scanner.calibrate_colour(BASE, "R")


def test_the_same_negative_scanned_as_rgb_and_as_own_rgb_agrees(tmp_path):
    """Same LEDs (the simulated Scanlight's, at full for own-rgb), two code paths."""
    rgb = rig()
    rgb_roll = Roll.open(tmp_path / "Scanlight")
    rgb_roll.set_settings(rect=list(sim.PICTURE_RECT))
    (rgb_tiff,) = render(rgb_roll, rgb.scan(rgb_roll, rgb.calibrate("film", BASE)))

    own = own_rig()
    preset = own_rgb_preset(own)
    own_roll = Roll.open(tmp_path / "Own")
    own_roll.set_settings(rect=list(sim.PICTURE_RECT))
    frame = own.scan(own_roll, preset, prompt=lambda letter, problem: own.camera._light.show(letter))
    assert [Path(name).name for name in frame.files] == ["Own_001_R.dng", "Own_001_G.dng", "Own_001_B.dng"]
    (own_tiff,) = render(own_roll, frame)
    a, b = read(rgb_tiff), read(own_tiff)
    x, y, w, h = sim.PICTURE_RECT
    inside = (slice(int(y * 2000) + 20, int((y + h) * 2000) - 20), slice(int(x * 3000) + 20, int((x + w) * 3000) - 20))
    assert np.abs(a[inside] - b[inside]).mean() < 0.01 and np.percentile(np.abs(a[inside] - b[inside]), 99) < 0.04


def test_a_wrong_colour_at_a_scan_prompt_is_asked_for_again_and_only_a_complete_frame_is_kept(tmp_path):
    scanner = own_rig()
    preset = own_rgb_preset(scanner)
    roll = Roll.open(tmp_path / "R")
    asked = []

    def operator(letter: str, problem: str) -> None:
        asked.append((letter, problem))
        scanner.camera._light.show("R" if letter == "G" and len(asked) == 2 else letter)  # red again where green was asked, once

    frame = scanner.scan(roll, preset, prompt=operator)
    assert asked == [("R", ""), ("G", ""), ("G", "That did not look like green only. Set your light to green only, then shoot again."), ("B", "")]
    assert len(frame.files) == 3 and sorted(path.name for path in (roll.folder / "raw").iterdir()) == ["R_001_B.dng", "R_001_G.dng", "R_001_R.dng"]


def test_a_frame_cancelled_after_one_shot_leaves_no_frame_and_no_files(tmp_path):
    scanner = own_rig()
    preset = own_rgb_preset(scanner)
    roll = Roll.open(tmp_path / "R")
    shots = scanner.begin_scan(roll, preset)
    scanner.camera._light.show("R")
    shots.shoot()
    assert shots.next_letter == "G" and not shots.complete
    shots.cancel()
    assert Roll.open(roll.folder).frames == [] and list((roll.folder / "raw").iterdir()) == []
    assert scanner.camera.get("shutter") == preset.shutter  # the live view as calibrated, not on red's shutter


def shutters_used(camera) -> list[float]:
    """Every shutter a camera is set to from here on, in seconds."""
    used, original = [], camera.set
    camera.set = lambda key, value: (used.append(sim.seconds(value)) if key == "shutter" else None) or original(key, value)
    return used


def test_a_bright_own_lights_bare_light_is_shot_between_1_250_and_1_1000_s(tmp_path):
    from tetherinvert.calibrate import FLAT_FASTEST_S

    scanner = own_rig(brightness=12.0)
    preset = scanner.calibrate("film", BASE, "own-colour", light_note=NOTE)
    used = shutters_used(scanner.camera)
    with sim.film_removed(scanner.camera):
        flat = scanner.capture_flatfield(Roll.open(tmp_path / "R"), preset)
    fastest = min(used)
    assert FLAT_FASTEST_S * 0.99 <= fastest < 1 / 250 and flat.kind == "own-colour"


def test_a_scanlight_kind_still_stops_at_1_250_s(tmp_path, monkeypatch):
    monkeypatch.setattr(sim, "RESPONSE", sim.RESPONSE * 12)
    scanner = rig()
    preset = scanner.calibrate("film", BASE, "rgb-together")  # the light stays at its default: only the shutter could go faster
    used = shutters_used(scanner.camera)
    with sim.film_removed(scanner.camera), pytest.raises(CalibrationError, match="Close the aperture or lower the ISO"):
        scanner.capture_flatfield(Roll.open(tmp_path / "R"), preset)
    assert min(used) >= 1 / 250 * 0.99


def test_an_own_light_too_bright_even_at_1_1000_s_is_refused_with_advice(tmp_path):
    scanner = own_rig(brightness=12.0)
    preset = scanner.calibrate("film", BASE, "own-colour", light_note=NOTE)
    scanner.camera._light.brightness = 80.0  # turned up after calibrating
    with sim.film_removed(scanner.camera), pytest.raises(CalibrationError, match="Close the aperture or dim the light, then calibrate again"):
        scanner.capture_flatfield(Roll.open(tmp_path / "R"), preset)


def test_a_flickering_own_light_is_refused_and_the_message_names_flicker(tmp_path):
    from tetherinvert.flatfield import FlatFieldError

    scanner = own_rig(brightness=12.0)
    preset = scanner.calibrate("film", BASE, "own-colour", light_note=NOTE)
    scanner.camera._light.flicker = True  # dimmed by pulse width: bands at the bare light's short shutter
    with sim.film_removed(scanner.camera), pytest.raises(FlatFieldError, match="the light flickers at this shutter speed: run it at full brightness"):
        scanner.capture_flatfield(Roll.open(tmp_path / "R"), preset)


def test_an_own_rgb_flat_field_is_three_prompted_shots(tmp_path):
    scanner = own_rig()
    preset = own_rgb_preset(scanner)
    asked = []
    lamp = scanner.camera._light
    with sim.film_removed(scanner.camera):
        flat = scanner.capture_flatfield(Roll.open(tmp_path / "R"), preset, prompt=lambda letter, problem: asked.append(letter) or lamp.show(letter))
    assert asked == ["R", "G", "B"] and flat.kind == "own-rgb" and flat.maps.shape[2] == 3


def test_an_own_light_session_shortens_each_colours_shutter_where_the_flat_field_calls_for_it(tmp_path):
    """No LEDs to lower: where the flat field shows the frame brighter than the film base
    rectangle by more than calibration left room for, each colour's shutter is shortened by whole
    thirds of a stop, and the film base scaled by the true ratio of the times."""
    from tetherinvert.calibrate import true_seconds
    from tetherinvert.session import clipping

    scanner = own_rig(falloff=(0.5, 0.5, 0.5))
    corner = Roi(0.035, 0.05, 0.025, 0.10)  # the dim corner of the left rebate, as in the brighter-middle test
    preset = own_rgb_preset(scanner, corner)
    roll = Roll.open(tmp_path / "R")
    lamp = scanner.camera._light
    with sim.film_removed(scanner.camera):
        scanner.capture_flatfield(roll, preset, prompt=lambda letter, problem: lamp.show(letter))
    shutters = Scanner.session_shutters(roll, preset, scanner.camera.choices("shutter"))
    _levels, base = Scanner.session_exposure(roll, preset)
    for channel, letter in enumerate("RGB"):
        ratio = true_seconds(shutters[letter]) / true_seconds(preset.shutters[letter])
        assert ratio < 0.9 and abs(np.log2(ratio) * 3 - round(np.log2(ratio) * 3)) < 0.05, letter  # whole thirds of a stop shorter
        assert base[channel] == pytest.approx(preset.base[channel] * ratio)
    used = shutters_used(scanner.camera)
    frame = scanner.scan(roll, preset, prompt=lambda letter, problem: lamp.show(letter))
    assert {sim.seconds(shutters[letter]) for letter in "RGB"} <= set(used) and frame.base == list(base)
    assert clipping(roll, frame) is None
    assert frame.light == NOTE  # the files name the light that was used, not NoLight
    (tiff,) = render(roll, frame)
    assert xmp_of(tiff)["scan:Light"] == f"{NOTE}, red, green and blue one by one" and xmp_of(tiff)["scan:Preset"] == "film (colour)"


def test_w4_warns_when_an_own_light_is_calibrated_faster_than_about_1_250_s():
    """Its flat field may then find no shutter short enough; calibrating again costs nothing yet."""
    from tetherinvert import wizard
    from tetherinvert.calibrate import BRIGHT_NOTE

    bright = Preset("film", "own-rgb", (0, 0, 0, 0), "1/200", roi=sim.BASE_ROI, shutters={"R": "1/250", "G": "1/200", "B": "1/160"}, light_note=NOTE, by_hand=("light",))
    assert ("Note", BRIGHT_NOTE) in wizard.result(bright)
    assert "Note" not in dict(wizard.result(replace(bright, shutters={"R": "1/200", "G": "1/200", "B": "1/160"})))
    assert ("Note", BRIGHT_NOTE) in wizard.result(Preset("film", "own-colour", (0, 0, 0, 0), "1/250", roi=sim.BASE_ROI, light_note=NOTE))
    assert dict(wizard.result(bright))["Light"] == NOTE


def test_an_own_light_on_the_command_line_without_the_scanlight(tmp_path, capsys):
    """--no-light: calibrate, flat field and scan an own-rgb roll with its prompts; a Scanlight
    preset is refused, saying the switch is on."""
    base = ",".join(str(v) for v in sim.BASE_ROI)
    roll = tmp_path / "Own"
    assert cli(["--sim", "--no-light", "calibrate", "Gold own", "--base", base, "--kind", "own-rgb", "--light", NOTE]) == 0
    assert cli(["--sim", "--no-light", "flatfield", str(roll), "--preset", "Gold own", "--yes"]) == 0
    assert cli(["--sim", "--no-light", "scan", str(roll), "--preset", "Gold own"]) == 0
    out = capsys.readouterr().out
    assert f"Light {NOTE} (SET BY HAND)" in out and "frame 1 done" in out
    assert sorted(path.name for path in (roll / "raw").iterdir()) == ["Own_001_B.dng", "Own_001_G.dng", "Own_001_R.dng"]
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    assert cli(["--sim", "--no-light", "scan", str(tmp_path / "Other"), "--preset", "Gold"]) == 1
    assert "started with --no-light" in capsys.readouterr().err
    assert cli(["--sim", "calibrate", "Bare", "--base", base, "--kind", "own-colour"]) == 1  # no note: the light is a setting by hand
    assert "pass --light" in capsys.readouterr().err


def test_the_calibration_wizard_for_an_own_light():
    """W1 offers the Scanlight's kinds only while one is connected; W2 asks for the light; own-rgb
    has a prompted step per colour between the film base and Calibrate, each shot refused or kept."""
    from tetherinvert import wizard as w

    new = w.Wizard.new_preset(None)
    new.name, new.scanlight = "Gold own", False
    assert new.missing() == "No Scanlight is connected: connect its data port (the left USB-C), or choose Own light."
    new.kind = "own-rgb"
    assert new.next() and [step.key for step in new.steps] == [w.FILM, w.CAMERA, w.FILM_BASE, "R", "G", "B", w.CALIBRATE]
    new.camera = True
    assert new.missing() == "Note which light it is and how it is set." and not new.next()
    new.values["light"] = f"  {NOTE} "
    assert new.next() and new.light_note == NOTE and new.by_hand() == {}
    new.rect = sim.BASE_ROI
    assert new.next() and new.step.key == "R" and new.position() == "Step 4 of 7 · Red only"
    assert new.next_label() == "Shoot" and not new.next()  # a colour step shoots, it does not just move on
    assert new.shoot() == "R" and new.shoot() is None and not new.can_back() and not new.can_cancel()
    assert not new.shot("R", None, "That did not look like red only.") and new.step.key == "R" and new.shot_problem.startswith("That did not")
    shutters = {"R": "1/2", "G": "1/4", "B": "1/5"}
    for letter in "RGB":
        assert new.shoot() == letter and new.shot(letter, {"letter": letter, "shutter": shutters[letter], "base": 58000.0}, "")
    assert new.step.key == w.CALIBRATE and new.state == w.RUNNING and new.shot_problem == "" and sorted(new.colour_shots) == ["B", "G", "R"]
    assert new.stages() == [("Shoot red, green and blue, each by hand", "done"), ("A shutter per colour: R 1/2 · G 1/4 · B 1/5 s", "done"),
                            ("Save the preset", "todo")]  # fmt: skip
    # A kind of one shot has no colour steps; changed at W1, they go.
    again = w.Wizard.new_preset(None)
    again.name, again.kind = "Gold own", "own-rgb"
    assert again.next() and again.back() and again.step.key == w.FILM
    again.kind = "own-colour"
    assert again.next() and [step.key for step in again.steps] == [w.FILM, w.CAMERA, w.FILM_BASE, w.CALIBRATE]
    assert w.filled_in(Preset("x", "own-colour", (0, 0, 0, 0), "1/4", roi=sim.BASE_ROI, light_note=NOTE))["light"] == NOTE
    recal = w.Wizard.recalibrate(Preset("x", "own-rgb", (0, 0, 0, 0), "1/4", light_note=NOTE, roi=sim.BASE_ROI))
    assert [step.key for step in recal.steps] == [w.CAMERA, w.FILM_BASE, "R", "G", "B", w.CALIBRATE]


# ----- settings the camera cannot set -----


def test_settings_set_by_hand_are_asked_for_stored_and_left_alone(tmp_path):
    from tetherinvert.session import tidy_aperture

    assert [tidy_aperture(t) for t in ("8", "f8", "F/5.6", "5,6", "wide open")] == ["f/8", "f/8", "f/5.6", "f/5.6", "wide open"]
    messages = []
    light = sim.SimLight()
    camera = sim.SimCamera(light)
    camera.manual_lens = True  # no contacts: the camera knows no aperture
    scanner = Scanner(light, camera, log=messages.append, settle_s=0.0)
    assert scanner.unknown_settings() == ["aperture"]

    preset = scanner.calibrate("film", BASE, by_hand={"aperture": "8"})
    assert preset.aperture == "f/8" and preset.by_hand == ("aperture",) and preset.iso == "100"
    assert preset.settings()[2] == ("Aperture", "f/8", True) and preset.settings()[0] == ("ISO", "100", False)
    assert rig().calibrate("film", BASE).by_hand == ()  # a lens the camera can read needs nothing

    store = PresetStore(tmp_path / "presets.json")
    store.save(preset)
    assert store.get("film") == preset  # the rectangle and the hand-set list survive the file
    scanner.scan(Roll.open(tmp_path / "R"), preset)
    assert not [m for m in messages if "could not set" in m]  # it never tried to set the aperture

    forgotten = scanner.calibrate("film", BASE)  # nothing stated: still flagged, so the reminder appears
    assert forgotten.settings()[2] == ("Aperture", "not recorded", True)


# ----- cleaning up -----


def test_clean_up_deletes_only_what_has_a_tiff(tmp_path):
    from tetherinvert.session import ScanError, clean_raws

    scanner = rig()
    preset = scanner.calibrate("film", BASE)
    roll = Roll.open(tmp_path / "R")
    first, _second = scanner.scan(roll, preset), scanner.scan(roll, preset)
    render(roll, first)  # the second frame is not rendered yet
    size = sum((roll.folder / name).stat().st_size for name in first.files)
    assert clean_raws(roll, dry_run=True) == (3, size, [2]) and len(list((roll.folder / "raw").iterdir())) == 6
    assert clean_raws(roll) == (3, size, [2])
    assert sorted(p.name for p in (roll.folder / "raw").iterdir()) == ["R_002_B.dng", "R_002_G.dng", "R_002_R.dng"]
    assert (roll.folder / "scans" / "R_001.tif").exists() and preview.load(roll, 1) is not None  # the window can still show it

    roll = Roll.open(roll.folder)
    with pytest.raises(ScanError, match="RAW files of frame 1 are gone"):
        render(roll, roll.frame(1))
    assert [p.name for p in render_roll(roll)] == ["R_002.tif"]  # only what still can be
    assert clean_raws(Roll.open(roll.folder))[0] == 3 and not (roll.folder / "raw").exists()
    with pytest.raises(ScanError, match="no RAW files left"):
        render_roll(Roll.open(roll.folder))
    assert scanner.scan(Roll.open(roll.folder), preset).number == 3  # numbering carries on


# ----- command line -----


def test_a_scan_with_more_light_than_at_calibration_is_reported_clipped(tmp_path, monkeypatch, capsys):
    """A third of a stop more light than at calibration clips the film base, and the prints come out
    dark: the scan says so."""
    import json

    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    base = ",".join(str(v) for v in sim.BASE_ROI)
    picture = ",".join(f"{v:.3f}" for v in sim.PICTURE_RECT)
    assert cli(["--sim", "calibrate", "Gold", "--base", base]) == 0
    capsys.readouterr()
    assert cli(["--sim", "scan", str(tmp_path / "R"), "--preset", "Gold", "--picture", picture]) == 0
    assert cli(["--sim", "scan", str(tmp_path / "R1"), "--preset", "Gold"]) == 0  # no picture area: the whole frame
    assert "WARNING" not in capsys.readouterr().out
    store = tmp_path / "home" / "presets.json"
    presets = json.loads(store.read_text())
    # As if the f-stop ring had moved. Green and blue: red is near the top of its range already. Checked on the whole
    # frame: the simulated picture holds no tone as thin as its film base, the rebate around it is that base.
    r, g, b, w = presets["Gold"]["levels"]
    presets["Gold"]["levels"] = [r, min(255, g * 3), min(255, b * 3), w]
    store.write_text(json.dumps(presets))
    assert cli(["--sim", "scan", str(tmp_path / "R2"), "--preset", "Gold"]) == 0
    out = capsys.readouterr().out
    assert "WARNING: Frame 1 is clipped (green " in out and "of the picture area at the sensor ceiling" in out and "Check the f-stop" in out
    assert "Capture a flat field for this roll" in out  # the roll has none: the likelier cause comes first


def test_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TETHERINVERT_HOME", str(tmp_path / "home"))
    base = ",".join(str(v) for v in sim.BASE_ROI)
    picture = ",".join(f"{v:.3f}" for v in sim.PICTURE_RECT)
    assert cli(["--sim", "calibrate", "Gold 200", "--base", base]) == 0
    assert PresetStore().names() == ["Gold 200"]
    assert cli(["--sim", "scan", str(tmp_path / "Roll7"), "--preset", "Gold 200", "--picture", picture]) == 0
    assert cli(["--sim", "scan", str(tmp_path / "Roll7"), "--preset", "Gold 200", "--negative"]) == 0
    assert (tmp_path / "Roll7" / "scans" / "Roll7_002.tif").exists() and (tmp_path / "Roll7" / "negative" / "Roll7_002.tif").exists()
    assert not (tmp_path / "Roll7" / "negative" / "Roll7_001.tif").exists()
    assert cli(["render", "--negative", str(tmp_path / "Roll7")]) == 0
    assert (tmp_path / "Roll7" / "negative" / "Roll7_001.tif").exists()
    # Every frame gets a print; a frame developed from another base, and the roll matched.
    assert cli(["develop", str(tmp_path / "Roll7"), "1", "converted white"]) == 0 and Roll.open(tmp_path / "Roll7").frame(1).develop == "converted white"
    capsys.readouterr()
    assert cli(["prints", str(tmp_path / "Roll7"), "--match"]) == 0 and Roll.open(tmp_path / "Roll7").matched
    assert "prints/Roll7_001.tif" in capsys.readouterr().out and (tmp_path / "Roll7" / "prints" / "Roll7_002.tif").exists()
    with tifffile.TiffFile(tmp_path / "Roll7" / "prints" / "Roll7_001.tif") as tif:
        assert "as converted white (points shared across the roll)" in tif.pages[0].description
    assert cli(["prints", str(tmp_path / "Roll7")]) == 0 and "every print is up to date" in capsys.readouterr().out
    assert cli(["presets"]) == 0 and "Gold 200: rgb, ISO 100, Shutter" in capsys.readouterr().out

    # a session from the command line: flat field first, then scans that use it
    assert cli(["--sim", "flatfield", str(tmp_path / "Roll8"), "--preset", "Gold 200", "--yes"]) == 0
    assert "falls off by up to" in capsys.readouterr().out and (tmp_path / "Roll8" / "flatfield.npz").exists()
    assert cli(["--sim", "scan", str(tmp_path / "Roll8"), "--preset", "Gold 200", "--picture", picture, "--compare"]) == 0
    printed = capsys.readouterr().out
    assert "lowered from the preset" in printed and "Roll8_001_noflat.tif" in printed
    with tifffile.TiffFile(tmp_path / "Roll8" / "scans" / "Roll8_001.tif") as tif:
        assert "flat-field corrected" in tif.pages[0].description
    with tifffile.TiffFile(tmp_path / "Roll8" / "scans" / "Roll8_001_noflat.tif") as tif:
        assert "NOT flat-field corrected" in tif.pages[0].description
    assert cli(["render", str(tmp_path / "Roll8"), "--no-flatfield"]) == 0
    assert not (tmp_path / "Roll8" / "scans" / "Roll8_001_noflat.tif").exists()  # nothing left to compare with
    with tifffile.TiffFile(tmp_path / "Roll8" / "scans" / "Roll8_001.tif") as tif:
        assert "flat-field corrected" not in tif.pages[0].description
    assert cli(["clean", str(tmp_path / "Roll8"), "--yes"]) == 0
    assert "deleted 3 RAW files" in capsys.readouterr().out and not (tmp_path / "Roll8" / "raw").exists()

    # a lens the camera cannot read: the f-stop has to be given, and is remembered
    monkeypatch.setenv("TETHERINVERT_SIM_MANUAL_LENS", "1")
    assert cli(["--sim", "calibrate", "HP5", "--base", base, "--white"]) == 1
    assert "pass --aperture" in capsys.readouterr().err
    assert cli(["--sim", "calibrate", "HP5", "--base", base, "--white", "--aperture", "5.6"]) == 0
    assert "Aperture f/5.6 (SET BY HAND)" in capsys.readouterr().out and PresetStore().get("HP5").by_hand == ("aperture",)
    monkeypatch.delenv("TETHERINVERT_SIM_MANUAL_LENS")
    assert cli(["--sim", "scan", str(tmp_path / "Roll7"), "--preset", "Nope"]) == 1
    assert "no preset named 'Nope'" in capsys.readouterr().err
    assert cli(["--sim", "status"]) == 0 and "Simulated Camera" in capsys.readouterr().out

    # Lightroom Classic from the command line (it is not here, so its folder is a scratch one and starting it is noted)
    from tetherinvert import lightroom

    launched = []
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("TETHERINVERT_LIGHTROOM_WAIT", "0")
    monkeypatch.setattr(lightroom, "modules_folder", lambda: tmp_path / "Modules")
    monkeypatch.setattr(lightroom, "launch", lambda: launched.append(1))
    assert cli(["lightroom", str(tmp_path / "Roll7" / "scans"), str(tmp_path / "Roll7" / "prints")]) == 1  # nobody answers
    printed = capsys.readouterr()
    assert launched == [1] and "plug-in was installed" in printed.out and "Roll7/scans and Roll7/prints" in printed.out and "Plug-in Manager" in printed.err
    asked = (tmp_path / "userhome" / ".tetherinvert" / "lightroom" / "request.txt").read_text().split("\n")[1:]
    assert asked == [str((tmp_path / "Roll7" / "scans").resolve()), str((tmp_path / "Roll7" / "prints").resolve()), ""]
    assert cli(["lightroom", "--stop"]) == 0 and len((tmp_path / "userhome" / ".tetherinvert" / "lightroom" / "request.txt").read_text().split("\n")) == 2
    assert cli(["lightroom"]) == 1 and "name the folders to watch" in capsys.readouterr().err
    assert cli(["--sim", "doctor"]) == 0
    report = capsys.readouterr().out
    assert "decodes: yes" in report and "pattern RGGB" in report and "LibRaw" in report and "RESULT: this camera's files decode" in report
    assert (tmp_path / "home" / "doctor" / "test-shot.dng").exists()


def test_print_corrections_move_the_way_the_keys_say():
    """SP-3000 keys: + D denser (darker), + C/M/Y more of that colour (less red/green/blue)."""
    values = np.full((2, 2, 3), 0.4, np.float32)
    values[0, 0] = (0.2, 0.3, 0.6)
    k, pivot = np.array([1.0, 2.0, 1.0], np.float32), 0.375
    assert invert.edit(values, k, pivot, None) is values and invert.edit(values, k, pivot, {"tone": "standard"}) is values  # nothing to do
    darker = invert.edit(values, k, pivot, {"d": 1})
    assert np.allclose(values - darker, k * invert.STEP_DENSITY)  # each channel by its own density step
    cyan = invert.edit(values, k, pivot, {"c": 2}) - values
    assert np.allclose(cyan[..., 0], -2 * invert.STEP_COLOUR) and np.allclose(cyan[..., 1:], 0)
    assert np.allclose(invert.edit(values, k, pivot, {"y": -1})[..., 2] - values[..., 2], invert.STEP_COLOUR)
    vivid = invert.edit(values, k, pivot, {"sat": 5})
    assert np.ptp(vivid[0, 0]) > np.ptp(values[0, 0]) and np.allclose(vivid[1, 1], values[1, 1])  # grey stays grey
    harder = invert.edit(values, k, pivot, {"grad": 2})  # contrast
    assert np.all(np.abs(harder - pivot) > np.abs(values - pivot))
    soft_shadows = invert.edit(values, k, pivot, {"tone": "shadow soft"})
    below = values < pivot
    assert np.all(soft_shadows[below] > values[below]) and np.array_equal(soft_shadows[~below], values[~below])
    assert invert.edit(values, k, pivot, {"d": -30}).max() <= 1.0  # clipped, not wrapped
    assert invert.adjustment({"d": 99, "grad": -30, "sat": 30, "tone": "sideways", "from": "x"}) == {**invert.NEUTRAL, "d": 80, "grad": -18, "sat": 24}


def test_the_gauges_ends_are_where_the_keys_move_the_picture():
    """Each gauge's + end is the way invert.edit moves a grey at +1, its − end the other way: for
    C, M and Y the channel that drops is the + end's weakest and the − end's strongest."""
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QColor

    from tetherinvert.window.viewer import GAUGE_GREY, GAUGES, gauge_position

    grey = np.full((1, 1, 3), 0.5, np.float32)
    k = np.ones(3, np.float32)
    rgb = {key: [np.array(QColor(c).getRgbF()[:3]) for c in GAUGES[key]] for key in ("d", "c", "m", "y")}
    for key, (minus, plus) in rgb.items():
        moved = invert.edit(grey, k, 0.5, {key: 1})[0, 0] - grey[0, 0]
        if key == "d":
            assert moved.mean() < 0 and plus.mean() < minus.mean()  # darker, and the + end is the dark one
            continue
        drops = int(np.argmin(moved))
        assert moved[drops] < 0 and np.argmin(plus) == drops and np.argmax(minus) == drops, key
        assert min(minus.max() - minus.min(), plus.max() - plus.min()) < 0.6  # muted: about half saturation, not full
    assert np.ptp(QColor(GAUGE_GREY).getRgbF()[:3]) < 0.02  # 0 is neutral grey
    assert [gauge_position("c", v) for v in (-80, 0, 80)] == [0.0, 0.5, 1.0] and abs(gauge_position("c", 20) - 0.75) < 1e-9  # square root
    assert gauge_position("grad", -18) == 0.0 and gauge_position("sat", 6) == 0.75  # each key's own range


def test_hold_gives_its_corrections_to_frames_without_their_own():
    from tetherinvert.corrections import Corrections

    c = Corrections()
    c.load(1, None), c.load(2, {"m": 2}), c.load(3, None)
    c.step(1, "d", 1)
    c.step(1, "d", 1)
    assert c.of(1) == {**invert.NEUTRAL, "d": 2} and c.hold is None
    assert not c.shown(3) and c.of(3) is None  # Hold off: nothing is carried
    c.set_hold(1)
    assert c.shown(3) and c.of(3) == {**invert.NEUTRAL, "d": 2}  # no corrections of its own: takes the held ones
    assert not c.shown(2) and c.hold == {**invert.NEUTRAL, "m": 2}  # its own: they become the held ones
    c.step(2, "y", -1)  # a correction made while holding is held too
    assert c.hold == {**invert.NEUTRAL, "m": 2, "y": -1}
    assert c.scanned(4) and c.of(4) == c.hold
    assert not c.scanned(1) and c.of(1) == {**invert.NEUTRAL, "d": 2}  # a retake keeps its corrections
    c.set_hold(None)
    assert not c.scanned(5) and c.of(5) is None


def test_corrections_step_within_their_range_and_are_saved_once():
    from tetherinvert.corrections import Corrections

    c = Corrections()
    assert c.load(1, {"c": 3}) == {"c": 3} and not c.has_unsaved()  # as saved in roll.json
    for _ in range(200):
        c.step(1, "d", 1)
    assert c.of(1)["d"] == invert.KEYS["d"][1]
    c.step(1, "tone", -1)
    assert c.of(1)["tone"] == list(invert.TONES)[-1]  # wraps round
    c.set(2, invert.NEUTRAL)
    assert c.of(2) is None  # neutral is none
    assert c.load(1, None)["d"] == invert.KEYS["d"][1]  # made here counts over what roll.json says
    assert c.take_unsaved() == [(1, c.of(1)), (2, None)] and not c.has_unsaved()
    c.to_all(1, [1, 2, 3])
    assert c.of(3) == c.of(1) and [n for n, _ in c.take_unsaved()] == [1, 2, 3]
    c.set_hold(1)
    c.clear()
    assert c.of(1) is None and c.hold is None and not c.has_unsaved()


def test_corrections_are_seen_on_the_preview_and_printed_from_the_flat_file(tmp_path):
    from tetherinvert.session import clean_raws, print_path

    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    roll = Roll.open(tmp_path / "P")
    roll.set_settings(rect=list(sim.PICTURE_RECT))
    render(roll, scanner.scan(roll, preset))
    roll = Roll.open(roll.folder)
    assert due(roll, 1)  # every frame gets a print, corrected or not
    (written,) = write_prints(roll)
    assert written == roll.folder / "prints" / "P_001.tif" and not due(Roll.open(roll.folder), 1)
    assert np.abs(tifffile.imread(written).astype(int) - tifffile.imread(roll.folder / "scans" / "P_001.tif")).max() <= 1  # flat, nothing set: the scan

    adjust = {"d": 3, "c": -2, "y": 1, "sat": 4, "grad": 1, "tone": "shadow soft"}
    roll.update_frame(1, adjust=adjust, develop="converted")
    roll = Roll.open(roll.folder)
    assert roll.frame(1).adjust == adjust and due(roll, 1)
    assert clean_raws(roll)[0] == 3  # the print needs no RAW file
    assert write_prints(roll) == [written] and not due(Roll.open(roll.folder), 1)
    # What was seen is what was printed: the corrected preview against the print, reduced the same way.
    dev = development.of_roll(roll)[1]
    shown = development.picture(dev).values
    assert np.abs(shown - preview.reduce(read(written))).mean() < 1 / 255
    assert shown.mean() < development.picture(dev.corrected(None)).values.mean()  # + D: denser
    with tifffile.TiffFile(written) as tif:
        assert "P_001.tif as converted (its own points), D+3 C-2 M+0 Y+1, contrast +1, saturation +4, tone shadow soft" in tif.pages[0].description
    # The flat base again, without corrections: the print is the scan once more.
    roll.update_frame(1, adjust=None, develop="flat")
    assert due(Roll.open(roll.folder), 1)
    write_prints(Roll.open(roll.folder))
    assert np.abs(tifffile.imread(written).astype(int) - tifffile.imread(roll.folder / "scans" / "P_001.tif")).max() <= 1
    assert print_path(roll, roll.frame(1)) == written


def test_a_print_is_out_of_date_after_rendering_again_and_a_retake_keeps_its_corrections(tmp_path):
    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    roll = Roll.open(tmp_path / "Q")
    render(roll, scanner.scan(roll, preset))
    Roll.open(roll.folder).update_frame(1, adjust={"m": 2})
    write_prints(Roll.open(roll.folder))
    roll = Roll.open(roll.folder)
    assert not due(roll, 1)
    render(roll, roll.frame(1))  # a new flat file: the print no longer matches it
    assert due(Roll.open(roll.folder), 1)
    retaken = scanner.scan(Roll.open(roll.folder), preset, retake=True)
    assert retaken.number == 1 and Roll.open(roll.folder).frame(1).adjust == {"m": 2}


def test_a_print_that_cannot_be_written_does_not_stop_the_others(tmp_path):
    from tetherinvert.session import PrintsError

    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    roll = Roll.open(tmp_path / "S")
    for _ in range(2):
        render(roll, scanner.scan(Roll.open(roll.folder), preset))
    (roll.folder / "scans" / "S_001.tif").write_bytes(b"not a TIFF")  # frame 1's flat file is damaged
    with pytest.raises(PrintsError, match=r"^1 print not written: ") as failed:
        write_prints(Roll.open(roll.folder))
    assert [path.name for path in failed.value.written] == ["S_002.tif"]
    roll = Roll.open(roll.folder)
    assert due(roll, 1) and not due(roll, 2)
    # A frame without its flat file cannot be printed: it is not due, and not tried.
    (roll.folder / "scans" / "S_001.tif").unlink()
    assert not due(roll, 1) and write_prints(roll) == []


def test_a_roll_keeps_the_preset_it_was_started_with(tmp_path):
    from tetherinvert.session import PresetStore as Store

    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    store = Store()
    store.save(preset)
    assert Roll.state(None) == "none" and Roll.state(tmp_path / "R") == "new"
    before = Roll.open(tmp_path / "R")  # opened before the roll was started
    scanner.scan(Roll.open(tmp_path / "R"), preset)
    assert Roll.state(tmp_path / "R") == "existing" and Roll.open(tmp_path / "R").scanned_with() == preset
    before.set_settings(compare=True)  # an object from before must not erase the roll's preset
    assert Roll.open(tmp_path / "R").preset is not None and Roll.open(tmp_path / "R").compare
    store.save(replace(preset, shutter="1/1000"))  # calibrated again: the roll still has its own
    assert Roll.open(tmp_path / "R").scanned_with().shutter == preset.shutter
    (tmp_path / "Holiday").mkdir()
    assert Roll.state(tmp_path / "Holiday") == "taken"


def test_the_orientation_maths_agrees_with_itself():
    """A point lands where its pixel does, comes back, and the buttons act on what is seen."""
    from tetherinvert import orient

    pixels = np.zeros((40, 60), np.uint8)
    pixels[5, 10] = 255
    for turn in (0, 90, 180, 270):
        for mirror in (False, True):
            o = {"turn": turn, "mirror": mirror}
            shown = orient.image(pixels, o)
            row, col = np.argwhere(shown == 255)[0]
            x, y = orient.point(10.5 / 60, 5.5 / 40, o)
            assert (int(x * shown.shape[1]), int(y * shown.shape[0])) == (col, row)
            assert orient.point_back(x, y, o) == pytest.approx((10.5 / 60, 5.5 / 40))
            assert np.array_equal(orient.image(pixels, orient.turned(o)), np.rot90(shown, -1))
            assert np.array_equal(orient.image(pixels, orient.turned(o, clockwise=False)), np.rot90(shown, 1))
            assert np.array_equal(orient.image(pixels, orient.mirrored(o)), shown[:, ::-1])
            box = (0.1, 0.2, 0.3, 0.25)
            assert orient.rect_back(orient.rect(box, o), o) == pytest.approx(box)
    assert orient.normal(None) == orient.UPRIGHT and orient.normal({"turn": 450}) == {"turn": 90, "mirror": False}


def test_frames_are_written_the_way_up_they_were_scanned(tmp_path):
    from tetherinvert import orient

    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    roll = Roll.open(tmp_path / "T")
    roll.set_settings(rect=list(sim.PICTURE_RECT))
    turned = {"turn": 90, "mirror": True}
    frame = scanner.scan(roll, preset, orientation=turned)
    assert Roll.open(roll.folder).frame(1).orientation == turned
    (flat_path,) = render(roll, frame)
    flat = read(flat_path)
    assert flat.shape == (3000, 2000, 3)  # turned: the camera's 3000 x 2000 stands up
    upright = orient.image(flat, {"turn": 270, "mirror": False})[:, ::-1]  # turned back by hand: mirror, then a quarter left
    assert np.allclose(patch(upright, sim.BASE_ROI), invert.LIFT, atol=0.01)  # the film base where the camera saw it
    assert preview.load(roll, 1).image.shape == (1067, 1600, 3)  # the preview stays as the camera sees it
    # Cropped to the picture area in the camera's frame, then turned.
    roll = Roll.open(roll.folder)
    roll.set_settings(crop=True)
    (cropped,) = render(roll, roll.frame(1))
    x, y, w, h = sim.PICTURE_RECT
    assert tifffile.imread(cropped).shape[:2] == (int((x + w) * 3000) - int(x * 3000), int((y + h) * 2000) - int(y * 2000))
    # The print is made from the turned (and here cropped) flat file, so it is turned too.
    Roll.open(roll.folder).update_frame(1, adjust={"d": 2}, develop="converted")
    (written,) = write_prints(Roll.open(roll.folder))
    assert tifffile.imread(written).shape == tifffile.imread(cropped).shape


def test_deleting_a_frame_takes_all_its_files_and_nothing_else(tmp_path):
    from tetherinvert.session import delete_frame, frame_files

    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    roll = Roll.open(tmp_path / "D")
    for _ in range(3):
        render(roll, scanner.scan(roll, preset), negative=True)
    Roll.open(roll.folder).update_frame(2, adjust={"m": 1})
    write_prints(Roll.open(roll.folder))
    roll = Roll.open(roll.folder)
    second = frame_files(roll, roll.frame(2))
    assert sorted(str(f.relative_to(roll.folder)) for f in second) == [
        "negative/D_002.tif", "preview/D_002.npz", "prints/D_002.tif", "raw/D_002_B.dng", "raw/D_002_G.dng", "raw/D_002_R.dng", "scans/D_002.tif",
    ]  # fmt: skip
    kept = frame_files(roll, roll.frame(1)) + frame_files(roll, roll.frame(3))
    assert sorted(delete_frame(roll, 2)) == sorted(second)
    assert not any(f.exists() for f in second) and all(f.exists() for f in kept)
    assert [f.number for f in Roll.open(roll.folder).frames] == [1, 3]
    assert scanner.scan(Roll.open(roll.folder), preset).number == 4  # a gap stays a gap
    delete_frame(Roll.open(roll.folder), 4)
    assert scanner.scan(Roll.open(roll.folder), preset).number == 4  # the last one's number is taken again, as by a retake
    for number in (1, 3, 4):
        delete_frame(Roll.open(roll.folder), number)
    assert Roll.open(roll.folder).frames == [] and not (roll.folder / "raw").exists() and (roll.folder / "roll.json").exists()  # still the roll


def test_a_scanned_frame_is_turned_in_place_losslessly_with_its_print(tmp_path, capsys):
    from tetherinvert import orient
    from tetherinvert.session import reorient_frame

    scanner = rig()
    preset = scanner.calibrate("Gold", BASE)
    roll = Roll.open(tmp_path / "O")
    render(roll, scanner.scan(roll, preset), negative=True)
    Roll.open(roll.folder).update_frame(1, adjust={"d": 1})
    write_prints(Roll.open(roll.folder))
    files = sorted(roll.folder.rglob("O_001*.tif"))
    assert [str(f.relative_to(roll.folder)) for f in files] == ["negative/O_001.tif", "prints/O_001.tif", "scans/O_001.tif"]

    def described(file):
        with tifffile.TiffFile(file) as tif:
            page = tif.pages[0]
            return page.asarray(), page.description, page.tags["InterColorProfile"].value, page.tags["DateTime"].value, page.tags[700].value

    before = {f: described(f) for f in files}
    upside = {"turn": 180, "mirror": True}
    assert sorted(reorient_frame(Roll.open(roll.folder), 1, upside)) == files
    for file, (pixels, description, profile, stamp, xmp) in before.items():
        now = described(file)
        assert np.array_equal(now[0], orient.image(pixels, upside)) and now[1:] == (description, profile, stamp, xmp)  # the same file, turned
    assert Roll.open(roll.folder).frame(1).orientation == upside
    turned = Roll.open(roll.folder)
    assert not due(turned, 1)  # the print turned with it: nothing to write again
    assert reorient_frame(Roll.open(roll.folder), 1, upside) == []  # already that way up
    assert cli(["orient", str(roll.folder), "1"]) == 0 and "3 files turned" in capsys.readouterr().out  # back upright
    assert all(np.array_equal(described(f)[0], pixels) for f, (pixels, *_) in before.items())


# ----- metadata -----

XMP_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "tiff": "http://ns.adobe.com/tiff/1.0/", "exif": "http://ns.adobe.com/exif/1.0/",
    "exifEX": "http://cipa.jp/exif/1.0/", "aux": "http://ns.adobe.com/exif/1.0/aux/", "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/", "film": "http://ns.tetherinvert.org/film/1.0/", "scan": "http://ns.tetherinvert.org/scan/1.0/",
}  # fmt: skip


def xmp_of(file: Path) -> dict:
    """A TIFF's XMP, parsed: each property of the description as its text, or its list items."""
    import xml.etree.ElementTree as ET

    with tifffile.TiffFile(file) as tif:
        packet = bytes(tif.pages[0].tags[700].value).decode("utf-8")
    description = ET.fromstring(packet[packet.index("<x:xmpmeta") : packet.index("</x:xmpmeta>") + 12]).find(".//rdf:Description", XMP_NS)
    found = {}
    for element in description:
        prefix = next(p for p, uri in XMP_NS.items() if element.tag.startswith("{" + uri + "}"))
        name = f"{prefix}:{element.tag.split('}')[1]}"
        items = element.findall(".//rdf:li", XMP_NS)
        found[name] = [item.text for item in items] if items else element.text
    return found


ROLL_FIELDS = {
    "camera": "Nikon FM2", "lens": "Nikkor 50mm f/1.8 AI-S", "shot_from": "2026-10-03", "shot_to": "2026-10-05", "film": "Ilford HP5 Plus",
    "box_speed": "400", "ei": "800", "developer": "Ilfosol 3 1+9, 9 min, 20 °C", "developed": "2026-10-08", "author": "Jan & Co",
    "copyright": "© 2026 Jan <jan>", "keywords": "berlin, lake, family", "notes": "Weekend at Müggelsee.",
}  # fmt: skip


def test_metadata_goes_into_every_file_where_lightroom_reads_it(tmp_path):
    from tetherinvert import metadata

    scanner = rig()
    preset = scanner.calibrate("HP5", BASE)
    roll = Roll.start(tmp_path / "Roll15", preset, flatfield=True)
    roll.set_metadata(metadata.updated({}, ROLL_FIELDS))
    frame = scanner.scan(roll, preset)
    assert frame.light == "Simulated Scanlight v4b" and frame.scanned is not None and frame.metadata is None  # never prefilled from the scanning camera
    changes = {"aperture": "8", "shutter": "1/125 s", "note": "Anna on the jetty", "keywords": "anna, jetty, Lake", "keywords_left_out": "family"}
    roll.update_frame(1, metadata=metadata.updated(None, changes, frame=True))
    render(Roll.open(roll.folder), Roll.open(roll.folder).frame(1), negative=True)
    Roll.open(roll.folder).update_frame(1, adjust={"grad": 2})
    write_prints(Roll.open(roll.folder))

    files = {kind: paths.tiff(roll, 1, kind) for kind in ("flat", "linear", "print")}
    for kind, file in files.items():
        found = xmp_of(file)
        assert found["tiff:Model"] == "Nikon FM2" and found["aux:Lens"] == found["exifEX:LensModel"] == "Nikkor 50mm f/1.8 AI-S"
        assert found["exif:DateTimeOriginal"] == found["photoshop:DateCreated"] == "2026-10-03"  # date only: no time invented
        assert found["exif:ISOSpeedRatings"] == ["800"] and found["exif:FNumber"] == "8/1" and found["exif:ExposureTime"] == "1/125"
        assert found["dc:creator"] == ["Jan & Co"] and found["dc:rights"] == ["© 2026 Jan <jan>"]
        assert found["dc:description"] == ["Anna on the jetty\nWeekend at Müggelsee."]
        assert found["dc:subject"] == ["berlin", "lake", "anna", "jetty"]  # the roll's but family, then the frame's; "Lake" is lake again
        assert found["film:Stock"] == "Ilford HP5 Plus" and found["film:BoxSpeed"] == "400" and found["film:ExposureIndex"] == "800"
        assert found["film:Developer"] == "Ilfosol 3 1+9, 9 min, 20 °C" and found["film:DateDeveloped"] == "2026-10-08" and found["film:DateShotTo"] == "2026-10-05"
        assert found["scan:Roll"] == "Roll15" and found["scan:Frame"] == "1" and found["scan:Camera"] == "Simulated Camera"
        assert found["scan:Light"] == "Simulated Scanlight v4b, red, green and blue LEDs" and found["scan:Preset"] == "HP5 (colour)"
        assert found["scan:Exposure"].startswith("ISO ") and found["scan:FlatField"] == "none" and found["scan:Software"].startswith("TetherInvert ")
        assert found["scan:File"] == metadata.FILE_KINDS[kind]
        # The scanning camera is TetherInvert Scan's, never the shooting camera's or its exposure's.
        assert not any("Simulated" in str(value) for name, value in found.items() if not name.startswith("scan:"))
        assert {"tiff:Make", "exif:LensModel"}.isdisjoint(found)
    assert xmp_of(files["print"])["scan:Base"] == "Flat" and xmp_of(files["print"])["scan:Corrections"] == "Contrast +2"
    assert "scan:Base" not in xmp_of(files["flat"])

    # Empty fields are not written at all.
    roll = Roll.open(roll.folder)
    roll.set_metadata(metadata.updated(roll.metadata, {key: "" for key in ROLL_FIELDS if key != "film"}))
    roll.update_frame(1, metadata=None)
    render(Roll.open(roll.folder), Roll.open(roll.folder).frame(1))
    found = xmp_of(files["flat"])
    assert sorted(name for name in found if not name.startswith("scan:")) == ["film:Stock"]


def test_keywords_and_fields_are_checked_as_they_are_set():
    from tetherinvert import metadata

    roll = {"keywords": ["berlin", "lake", "family"]}
    assert metadata.keywords(roll, None) == ["berlin", "lake", "family"]
    assert metadata.keywords(roll, {"keywords": ["anna", "berlin"], "keywords_left_out": ["Lake"]}) == ["berlin", "family", "anna"]
    assert metadata.clean("keywords", " a, b ,,A, c") == ["a", "b", "c"]
    shot = {"shot_from": "2026-10-03"}
    assert metadata.frame_date(shot, {}) == "2026-10-03" and metadata.frame_date(shot, {"date": "2026-10-04"}) == "2026-10-04"
    assert [metadata.clean("aperture", text) for text in ("8", "f8", "F/5.6", "5,6")] == ["f/8", "f/8", "f/5.6", "f/5.6"]
    assert [metadata.clean("shutter", text) for text in ("1/125", "1/125 s", "2", '2"', "0.5", "1/4000")] == ["1/125", "1/125", "2", "2", "1/2", "1/4000"]
    assert metadata.clean("shot_from", "2026-10-03 14:05") == "2026-10-03T14:05" and metadata.clean("developed", "2026-10") == "2026-10"
    for key, text, advice in (("shutter", "125", "1/125"), ("shutter", "fast", "1/125"), ("aperture", "wide", "f/8"), ("shot_from", "3.10.2026", "2026-10-03"),
                              ("shot_from", "2026-02-30", "2026-10-03"), ("ei", "800a", "400")):  # fmt: skip
        with pytest.raises(ValueError, match=advice):
            metadata.clean(key, text)
    with pytest.raises(ValueError, match="no roll field 'iso'"):
        metadata.updated({}, {"iso": "400"})
    assert metadata.updated({"film": "HP5", "ei": "800"}, {"ei": " ", "camera": "FM2"}) == {"film": "HP5", "camera": "FM2"}
    assert [metadata.push("400", ei) for ei in ("400", "800", "1600", "200", "640")] == ["box speed", "push +1", "push +2", "pull −1", "push +2/3"]
    assert metadata.dates("2026-10-03", "2026-10-05") == "3–5 Oct 2026" and metadata.dates("2026-09-28", "2026-10-02") == "28 Sep – 2 Oct 2026"
    assert metadata.dates("2026-10-03", "") == "3 Oct 2026" and metadata.dates("2026-10", "") == "2026-10"
    fields = metadata.updated({}, ROLL_FIELDS)
    assert metadata.roll_summary(fields) == ["Nikon FM2 · Ilford HP5 Plus at EI 800", "3–5 Oct 2026 · Ilfosol 3 1+9, 9 min, 20 °C"]
    assert metadata.frame_summary(fields, {"note": "Anna", "keywords": ["anna"], "keywords_left_out": ["family"]}) == "Anna · 3 keywords, 1 of the roll's left out"
    assert metadata.roll_summary({}) == [] and metadata.roll_summary({"lens": "50 mm", "author": "Jan"}) == []  # nothing for the header


def test_changing_metadata_rewrites_only_the_xmp_of_every_file(colour_roll, tmp_path):
    from tetherinvert import metadata
    from tetherinvert.session import retag
    from tetherinvert.tiffio import read_xmp

    roll = own_copy(colour_roll, tmp_path)
    write_prints(roll)
    files = sorted(roll.folder.rglob("*.tif"))
    assert {f.parent.name for f in files} == {"scans", "prints", "negative"} and len(files) == 9

    def pixels(file):
        with tifffile.TiffFile(file) as tif:
            page = tif.pages[0]
            return page.asarray(), page.description, page.tags["InterColorProfile"].value, page.tags["DateTime"].value

    before = {f: pixels(f) for f in files}
    roll.set_metadata(metadata.updated({}, {"film": "Portra 400", "keywords": "test"}))
    roll.update_frame(2, metadata={"note": "the second"})
    assert sorted(retag(Roll.open(roll.folder))) == files
    for file, (array, *rest) in before.items():
        now = pixels(file)
        assert np.array_equal(now[0], array) and list(now[1:]) == rest  # the pixels, description, profile and date as they were
        found = xmp_of(file)
        assert found["film:Stock"] == "Portra 400" and found["dc:subject"] == ["test"]
        assert found.get("dc:description") == (["the second"] if "_002" in file.name else None)
    assert not list(roll.folder.rglob("*.part"))
    # A frame alone: only its files. A longer packet than before is fine too.
    roll.update_frame(1, metadata={"note": "x" * 5000})
    assert sorted(f.name for f in retag(Roll.open(roll.folder), [1])) == ["Roll.01_001.tif"] * 3
    assert "x" * 5000 in read_xmp(paths.tiff(roll, 1, "print")) and "x" * 5000 not in read_xmp(paths.tiff(roll, 2, "print"))
    assert np.array_equal(tifffile.imread(paths.tiff(roll, 1, "flat")), before[paths.tiff(roll, 1, "flat")][0])
    # A file without XMP gets it, its pixels unchanged.
    from tetherinvert.tiffio import set_xmp

    from tetherinvert.tiffio import write_tiff

    plain = tmp_path / "plain.tif"
    write_tiff(plain, before[files[0]][0], 2.2, "no metadata")
    set_xmp(plain, metadata.xmp(roll, roll.frame(1), "flat"))
    assert np.array_equal(tifffile.imread(plain), before[files[0]][0]) and xmp_of(plain)["scan:Frame"] == "1"


def test_metadata_on_the_command_line(colour_roll, tmp_path, capsys):
    roll = own_copy(colour_roll, tmp_path)
    folder = str(roll.folder)
    assert cli(["metadata", folder]) == 0 and "no metadata entered" in capsys.readouterr().out
    assert cli(["metadata", folder, "film=HP5 Plus", "ei=800", "keywords=a, b"]) == 0
    out = capsys.readouterr().out
    assert "metadata written into 6 files" in out and "film (Film stock): HP5 Plus" in out and "keywords (Keywords): a, b" in out
    assert xmp_of(paths.tiff(roll, 3, "flat"))["film:Stock"] == "HP5 Plus"
    assert cli(["metadata", folder, "--frame", "2", "note=jetty", "keywords_left_out=a"]) == 0
    out = capsys.readouterr().out
    assert "written into 2 files" in out and "note (Note): jetty" in out and "keywords written: b" in out and "date written: none" in out
    assert cli(["metadata", folder, "ei="]) == 0 and "ei" not in Roll.open(roll.folder).metadata  # an empty value clears
    capsys.readouterr()
    assert cli(["metadata", folder, "ei=fast"]) == 1 and "not a film speed" in capsys.readouterr().err
    assert cli(["metadata", folder, "speed=400"]) == 1 and "no roll field 'speed'" in capsys.readouterr().err
    assert cli(["metadata", folder, "film"]) == 1 and "name=value" in capsys.readouterr().err
    assert cli(["metadata", str(tmp_path)]) == 1 and "not a roll folder" in capsys.readouterr().err


# ----- roll.json: one way to change a roll -----

# A preset never calibrated: these tests only store its copy in a roll and take no exposure with it.
UNCALIBRATED = Preset("Gold", "rgb", (200, 180, 160, 0), "1/60", base=(30000.0, 20000.0, 10000.0), roi=(0.1, 0.1, 0.1, 0.1))


def frame_of(number: int, scanned: float = 1.0) -> Frame:
    """A frame as roll.json holds it; nothing on disk behind it."""
    return Frame(number, [f"raw/R_{number:03d}_R.dng"], "Gold", "rgb", [1.0, 1.0, 1.0], [0.1, 0.1, 0.1, 0.1], scanned=scanned)


def two_frames(folder: Path) -> Roll:
    roll = Roll.start(folder, UNCALIBRATED, flatfield=True)
    roll.add_frame(frame_of(1), UNCALIBRATED)
    roll.add_frame(frame_of(2), UNCALIBRATED)
    return roll


def test_a_roll_opened_before_match_the_roll_was_set_keeps_it_when_it_adds_a_frame(tmp_path):
    Roll.start(tmp_path / "R", UNCALIBRATED, flatfield=True)
    scanning = Roll.open(tmp_path / "R")  # the scan thread opens the roll when the scan starts
    Roll.open(tmp_path / "R").set_settings(matched=True)  # Match the roll, clicked while it exposes
    scanning.add_frame(frame_of(1), UNCALIBRATED)
    assert Roll.open(tmp_path / "R").matched and scanning.matched


ROLL_CHANGES = {  # each named change, and what it changed
    "start": (lambda roll: Roll.start(roll.folder, replace(UNCALIBRATED, name="Other"), flatfield=False), lambda r: not r.flatfield and r.preset["name"] == "Gold"),
    "set_settings": (lambda roll: roll.set_settings(crop=True, rect=[0.1, 0.1, 0.8, 0.8]), lambda r: r.crop and r.rect == [0.1, 0.1, 0.8, 0.8]),
    "add_frame": (lambda roll: roll.add_frame(frame_of(3), UNCALIBRATED), lambda r: [f.number for f in r.frames] == [1, 2, 3]),
    "note_bounds": (lambda roll: roll.note_bounds(replace(roll.frame(2), bounds={"dense": [1, 2, 3]}, rendered=5.0)), lambda r: r.frame(2).rendered == 5.0),
    "update_frame": (lambda roll: roll.update_frame(2, develop="converted"), lambda r: r.frame(2).develop == "converted"),
    "remove_frame": (lambda roll: roll.remove_frame(2), lambda r: [f.number for f in r.frames] == [1]),
    "set_metadata": (lambda roll: roll.set_metadata({"film": "Portra"}), lambda r: r.metadata == {"film": "Portra"}),
    "set_sheet": (lambda roll: roll.set_sheet({"rebate": False}), lambda r: r.sheet == {"sprockets": True, "rebate": False}),
    "set_next": (lambda roll: roll.set_next(7), lambda r: r.next == 7),
}


@pytest.mark.parametrize("change", ROLL_CHANGES)
def test_every_change_keeps_what_another_object_changed_and_leaves_its_caller_as_written(tmp_path, change):
    two_frames(tmp_path / "R")
    caller = Roll.open(tmp_path / "R")  # opened before the other object's changes
    other = Roll.open(tmp_path / "R")
    other.set_settings(matched=True)
    other.set_sheet({"sprockets": True})
    other.update_frame(1, adjust={"d": 2})
    make, took = ROLL_CHANGES[change]
    result = make(caller) or caller  # Roll.start gives the roll it started
    on_disk = Roll.open(tmp_path / "R")
    assert took(on_disk)
    assert on_disk.matched and on_disk.sheet["sprockets"] and on_disk.frame(1).adjust == {"d": 2}
    assert result == on_disk


HOLD_THE_ROLL = "import fcntl, sys\nhandle = open(sys.argv[1], 'a')\nfcntl.flock(handle, fcntl.LOCK_EX)\nprint('locked', flush=True)\nsys.stdin.readline()\n"


def test_a_change_waits_while_another_process_holds_the_roll(tmp_path):
    import subprocess
    import sys
    import threading

    Roll.start(tmp_path / "R", UNCALIBRATED, flatfield=True)
    holder = subprocess.Popen([sys.executable, "-c", HOLD_THE_ROLL, str(tmp_path / "R" / ".roll.lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        done = threading.Event()
        worker = threading.Thread(target=lambda: (Roll.open(tmp_path / "R").set_settings(matched=True), done.set()))
        worker.start()
        assert not done.wait(0.5) and not Roll.open(tmp_path / "R").matched
        holder.communicate("go\n", timeout=5)
        worker.join(5)
        assert done.is_set() and Roll.open(tmp_path / "R").matched
    finally:
        holder.kill()


def test_only_the_roll_settings_are_set_as_settings(tmp_path):
    roll = Roll.start(tmp_path / "R", UNCALIBRATED, flatfield=True)
    for name in ("frames", "preset", "next", "metadata", "sheet", "folder", "flat_field"):
        with pytest.raises(TypeError, match=name):
            roll.set_settings(**{name: None})
    with pytest.raises(TypeError, match="bogus"):
        roll.set_settings(matched=True, bogus=1)
    assert not Roll.open(tmp_path / "R").matched  # refused as a whole


def test_a_render_that_finishes_after_a_rescan_leaves_the_new_frame_alone(tmp_path):
    roll = two_frames(tmp_path / "R")
    rendering = roll.frame(2)  # the render thread's frame, before the rescan
    roll.add_frame(frame_of(2, scanned=9.0), UNCALIBRATED)  # rescanned: the same file names
    rendering.bounds, rendering.rendered = {"dense": [1.0, 1.0, 1.0]}, 5.0
    Roll.open(tmp_path / "R").note_bounds(rendering)
    new = Roll.open(tmp_path / "R").frame(2)
    assert new.scanned == 9.0 and new.bounds is None and new.rendered is None


def test_a_folder_given_a_flat_field_on_the_command_line_is_a_roll_the_window_continues(tmp_path):
    PresetStore().save(rig().calibrate("Gold", BASE))
    assert cli(["--sim", "flatfield", str(tmp_path / "R"), "--preset", "Gold", "--yes"]) == 0
    assert Roll.state(tmp_path / "R") == "existing" and Roll.open(tmp_path / "R").flatfield


def test_a_roll_rendered_again_keeps_the_settings_it_started_with(colour_roll, tmp_path):
    roll = own_copy(colour_roll, tmp_path)
    moved = lambda done, total: done == 1 and Roll.open(roll.folder).set_settings(rect=[0.2, 0.2, 0.5, 0.5])  # noqa: E731  (a scan meanwhile)
    render_roll(Roll.open(roll.folder), progress=moved)
    assert [Roll.open(roll.folder).frame(n).bounds["rect"] for n in (1, 2, 3)] == [list(sim.PICTURE_RECT)] * 3


def test_the_about_box_says_the_version_and_the_build_in_words(monkeypatch):
    """Help › About: the version, the build (from the app's _build.py, else git, else 'installed from
    source') and what it runs on; the same as plain text for an issue."""
    import sys
    import types

    from tetherinvert import __version__, about

    details = dict(about.details())
    assert details["Version"] == __version__ and details["Python"] and "Qt" in details and "RAW decoder" in details
    assert details["Build"].startswith(("source checkout, commit ", "installed from source"))
    built = types.ModuleType("tetherinvert._build")
    built.BUILD = "app, commit abc1234, built 2026-10-17 20:00 UTC, GitHub run 12"
    monkeypatch.setitem(sys.modules, "tetherinvert._build", built)
    assert about.build() == built.BUILD and f"Build: {built.BUILD}" in about.text()
    assert about.text().startswith(f"TetherInvert {__version__}\n")


def test_the_keyboard_drawing_shows_the_windows_keys_and_is_the_manuals():
    """Help › Keyboard Shortcuts draws keyboard.svg() from the window's keys; the manual's picture must be
    the same drawing (after changing a shortcut: python -m tetherinvert.keyboard)."""
    from tetherinvert import keyboard
    from tetherinvert.window import viewer

    drawn = keyboard.used()
    assert all(chr(key.value) in drawn and drawn[chr(key.value)][0] == "adjust" for key in viewer.KEYS)  # every correction key
    assert drawn["Q"][1] == "D +" and drawn["A"][1] == "D −" and drawn["G"][1] == "Contrast" and drawn["Y"][1] == "Sat"
    assert keyboard.MANUAL_COPY.read_text() == keyboard.svg(), "the manual's keyboard picture is out of date: python -m tetherinvert.keyboard"
