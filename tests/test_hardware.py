"""The window's hardware thread keeps the camera and the Scanlight apart: each is opened, retried
and lost on its own. Driven tick by tick with fake devices and a clock of its own, so no
timer, thread or rig is involved; the fakes count how often each device is opened and closed."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("PySide6")

from tetherinvert import session, sim, workers  # noqa: E402
from tetherinvert.cli import main as cli  # noqa: E402


class Gone(OSError):
    pass


class Rig:
    """Two plugs the test pulls and pushes. A device opened while plugged in stops answering when
    its plug is pulled, as a real one does."""

    def __init__(self) -> None:
        self.plugged = {"camera": True, "light": True}
        self.opened = {"camera": 0, "light": 0}
        self.closed = {"camera": 0, "light": 0}
        self.frames = True  # False: the camera answers settings but sends no live-view frames

    def open(self, which: str):
        if not self.plugged[which]:
            raise Gone(f"no {which} on the bus")
        self.opened[which] += 1
        return FakeLight(self) if which == "light" else FakeCamera(self)

    def check(self, which: str) -> None:
        if not self.plugged[which]:
            raise Gone(f"the {which} went away")


class FakeLight:
    def __init__(self, rig: Rig) -> None:
        self.rig, self.color = rig, None

    def set(self, r=0, g=0, b=0, w=0) -> None:
        self.rig.check("light")
        self.color = (r, g, b, w)

    def off(self) -> None:
        self.set()

    def info(self, timeout: float = 2.0):
        self.rig.check("light")
        return 7, 3

    def describe(self) -> str:
        return "Fake Scanlight"

    def temperature(self):
        self.rig.check("light")
        return 31.5

    def close(self) -> None:
        self.rig.closed["light"] += 1


class FakeCamera:
    model = "Fake Camera"
    has_live_view = True

    def __init__(self, rig: Rig) -> None:
        self.rig = rig

    def preview(self):
        self.rig.check("camera")
        return b"frame" if self.rig.frames else None

    def get(self, key):
        self.rig.check("camera")
        return "1/4" if key == "shutter" else "100"

    def keeps_card_copy(self) -> bool:
        return False

    def close(self) -> None:
        self.rig.closed["camera"] += 1


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def bench(monkeypatch):
    """A Hardware on the fake rig, with what it said collected."""
    rig, clock = Rig(), Clock()
    monkeypatch.setattr(workers, "open_light", lambda _rig: rig.open("light"))
    monkeypatch.setattr(workers, "open_camera", lambda _rig: rig.open("camera"))
    monkeypatch.setattr(workers, "time", types.SimpleNamespace(monotonic=clock))
    hardware = workers.Hardware(sim=False)
    said = {"devices": [], "failed": [], "frames": 0, "temperature": []}
    hardware.devices.connect(lambda camera, light: said["devices"].append((camera, light)))
    hardware.temperature.connect(said["temperature"].append)
    hardware.failed.connect(said["failed"].append)
    hardware.frame.connect(lambda data: said.__setitem__("frames", said["frames"] + 1))

    def tick(seconds: float = 0.06, times: int = 1) -> None:
        for _ in range(times):
            clock.now += seconds
            hardware._tick()

    return types.SimpleNamespace(rig=rig, hardware=hardware, said=said, tick=tick, clock=clock)


def test_a_scanlight_lost_during_live_view_leaves_the_camera_alone_and_is_found_again(bench):
    bench.tick()
    assert bench.said["devices"][-1] == ("Fake Camera", "Fake Scanlight")
    bench.rig.plugged["light"] = False
    bench.tick(1.0, times=4)  # the light is asked every 3 s
    assert bench.said["devices"][-1] == ("Fake Camera", "not connected")
    assert any("Scanlight does not answer" in text for text in bench.said["failed"])
    frames = bench.said["frames"]
    bench.tick(times=5)
    assert bench.said["frames"] == frames + 5  # live view went on
    bench.rig.plugged["light"] = True
    bench.tick(2.1)
    assert bench.said["devices"][-1] == ("Fake Camera", "Fake Scanlight")
    assert bench.hardware._light.color == workers.IDLE_LIGHT  # the framing light is set again
    assert bench.rig.opened == {"camera": 1, "light": 2}
    assert bench.rig.closed["camera"] == 0


def test_the_lights_temperature_is_reported_with_each_check_while_it_answers(bench):
    bench.tick()
    bench.tick(1.0, times=7)  # checks at 3 s and 6 s
    assert bench.said["temperature"] == [31.5, 31.5]
    bench.rig.plugged["light"] = False
    bench.tick(1.0, times=3)
    assert bench.said["temperature"] == [31.5, 31.5]  # a light that does not answer reports nothing


def test_a_camera_lost_to_missed_frames_does_not_reopen_the_light(bench):
    bench.tick()
    bench.rig.frames = False
    bench.tick(times=41)
    assert bench.said["devices"][-1] == ("not connected", "Fake Scanlight")
    bench.rig.frames = True
    bench.tick(2.1)
    assert bench.said["devices"][-1] == ("Fake Camera", "Fake Scanlight")
    assert bench.rig.opened == {"camera": 2, "light": 1}
    assert bench.rig.closed == {"camera": 1, "light": 0}


def test_a_scanlight_that_appears_late_is_found_without_touching_the_camera(bench):
    bench.rig.plugged["light"] = False
    bench.tick()
    assert bench.said["devices"][-1] == ("Fake Camera", session.NO_LIGHT_LABEL)  # an own light needs none
    failures = len(bench.said["failed"])
    bench.tick(2.1, times=3)
    assert len(bench.said["failed"]) == failures  # why it is missing is said once, not every 2 s
    assert bench.said["frames"] == 4  # live view needs only the camera
    bench.rig.plugged["light"] = True
    bench.tick(2.1)
    assert bench.said["devices"][-1] == ("Fake Camera", "Fake Scanlight")
    assert bench.rig.opened == {"camera": 1, "light": 1}
    assert bench.rig.closed["camera"] == 0


def test_a_scanlight_that_opens_but_does_not_answer_is_closed_again(bench, monkeypatch):
    monkeypatch.setattr(FakeLight, "describe", lambda self: (_ for _ in ()).throw(Gone("no answer")))
    bench.tick()
    bench.tick(2.1)
    assert bench.rig.opened["light"] == 2 and bench.rig.closed["light"] == 2  # each try's port closed again
    assert bench.said["devices"][-1] == ("Fake Camera", session.NO_LIGHT_LABEL)


@pytest.mark.parametrize("lost, kept, name", [("light", "camera", "Scanlight"), ("camera", "light", "camera")])
def test_a_failed_job_drops_only_the_device_that_no_longer_answers(bench, lost, kept, name):
    bench.tick()

    def work(scanner):
        bench.rig.plugged[lost] = False
        raise Gone("I/O error in the middle of the scan")

    bench.hardware._job(work)
    assert bench.rig.closed == {lost: 1, kept: 0}
    assert getattr(bench.hardware, f"_{kept}") is not None and getattr(bench.hardware, f"_{lost}") is None
    assert bench.said["failed"][0] == "I/O error in the middle of the scan"  # shown as it was
    assert bench.said["failed"][1:] == [workers.LOST[lost]]  # and nothing about the other one
    assert name in workers.LOST[lost]


def test_a_failed_job_with_both_devices_answering_drops_nothing(bench):
    bench.tick()

    def work(scanner):
        raise Gone("something odd")

    bench.hardware._job(work)
    assert bench.said["failed"] == ["something odd"]
    assert bench.rig.closed == {"camera": 0, "light": 0}
    assert bench.hardware._light is not None and bench.hardware._camera is not None


@pytest.mark.parametrize("missing", ["light", "camera"])
def test_a_job_without_one_of_its_devices_is_refused_by_name(bench, missing):
    bench.rig.plugged[missing] = False
    bench.tick()
    ran = []
    bench.hardware._job(ran.append)
    assert ran == []
    assert bench.said["failed"][-1] == (session.NEEDS_SCANLIGHT["none"] if missing == "light" else session.MISSING[missing])


def test_without_a_scanlight_the_slot_holds_no_light_and_only_an_own_light_job_runs(bench):
    """No Scanlight is news, not a failure; a Scanlight job is refused by name, an own light's
    needs the camera alone; a Scanlight plugged in later is still found."""
    bench.rig.plugged["light"] = False
    bench.tick()
    assert isinstance(bench.hardware._light, session.NoLight) and not any("Scanlight" in text for text in bench.said["failed"])
    ran = []
    bench.hardware._job(ran.append, "rgb")
    assert ran == [] and bench.said["failed"][-1] == session.NEEDS_SCANLIGHT["none"]
    bench.hardware._job(ran.append, "own-colour")
    assert len(ran) == 1 and isinstance(ran[0].light, session.NoLight)
    bench.hardware.set_framing(None)  # an own light's preset: nothing to send
    bench.rig.plugged["light"] = True
    bench.tick(2.1)
    assert bench.said["devices"][-1] == ("Fake Camera", "Fake Scanlight") and bench.hardware._light.color is None  # found; still not lit


def test_with_no_light_the_scanlights_port_is_never_opened(bench, monkeypatch):
    monkeypatch.setenv("TETHERINVERT_NO_LIGHT", "1")
    bench.tick(2.1, times=4)
    assert bench.rig.opened["light"] == 0 and isinstance(bench.hardware._light, session.NoLight)
    assert bench.said["devices"][-1] == ("Fake Camera", session.NO_LIGHT_LABEL) and bench.said["failed"] == []
    ran = []
    bench.hardware._job(ran.append, "rgb")
    assert ran == [] and bench.said["failed"][-1] == session.NEEDS_SCANLIGHT["switched off"]


def test_the_light_command_needs_no_camera(monkeypatch):
    lit = []
    monkeypatch.setattr(sim.Bench, "camera", lambda self: (_ for _ in ()).throw(Gone("no camera on the bus")))
    monkeypatch.setattr("builtins.input", lambda prompt="": lit.append(prompt))
    monkeypatch.setattr(sim.SimLight, "set", lambda self, r=0, g=0, b=0, w=0: lit.append((r, g, b, w)))
    assert cli(["--sim", "light", "0", "255", "0"]) == 0
    assert (0, 255, 0, 0) in lit


def test_the_light_command_with_no_levels_is_a_manual_light_until_q(monkeypatch):
    """The Scanlight as an own light: commands from the terminal, the port held, no camera."""
    lit, typed = [], iter(["g", "r 128", "rgb 200 180 160", "nonsense", "off", "w", "q"])
    monkeypatch.setattr(sim.Bench, "camera", lambda self: (_ for _ in ()).throw(Gone("no camera on the bus")))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    monkeypatch.setattr(sim.SimLight, "set", lambda self, r=0, g=0, b=0, w=0: lit.append((r, g, b, w)))
    monkeypatch.setattr(sim.SimLight, "off", lambda self: lit.append("off"))
    assert cli(["--sim", "--no-light", "light"]) == 0  # --no-light does not keep this one off the light
    assert lit[:6] == [(0, 255, 0, 0), (128, 0, 0, 0), (200, 180, 160, 0), "off", (0, 0, 0, 255), "off"]


def test_status_without_a_light_reports_it_and_still_shows_the_camera(monkeypatch, capsys):
    monkeypatch.setattr(session, "open_light", lambda rig=None, drive=False: (_ for _ in ()).throw(Gone("no such port")))
    assert cli(["--sim", "status"]) == 0
    out = capsys.readouterr().out
    assert "camera: Simulated Camera" in out
    assert session.MISSING["light"] in out and "no such port" in out


def test_scanning_without_a_camera_is_refused_by_name(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sim.Bench, "camera", lambda self: (_ for _ in ()).throw(Gone("no camera on the bus")))
    assert cli(["--sim", "scan", str(tmp_path / "Roll"), "--preset", "none"]) == 1
    assert session.MISSING["camera"] in capsys.readouterr().err
