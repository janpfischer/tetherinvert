"""The window's two worker threads: one owns the camera and the light (live view, calibration,
flat field, scanning), one renders, so the next frame can be scanned while the previous one is
still being written.

Each is a QObject moved onto its own QThread. The window talks to them only by signals, and
connects their signals to its bound methods, never to lambdas: a lambda would run on the
worker's thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import orient, paths
from .calibrate import CalibrationError, Roi
from .camera import CARD_COPY
from .diagnose import take_test_shot, versions
from .flatfield import FlatFieldError
from .session import (
    MISSING,
    NEEDS_SCANLIGHT,
    NO_LIGHT_LABEL,
    NoLight,
    Preset,
    PresetStore,
    PrintsError,
    Roll,
    ScanError,
    Scanner,
    bench,
    clipping,
    no_light,
    open_camera,
    open_light,
    render,
    render_roll,
    reorient_frame,
    retag,
    write_prints,
)
from .calibrate import WrongColour
from .kinds import EXPOSURES, KINDS
from .sim import film_removed, set_by_hand

IDLE_LIGHT = (30, 30, 30, 0)  # for framing before any preset exists
NOT_CONNECTED = "not connected"
LIGHT_CHECK_S = 3.0  # how often an idle Scanlight is asked whether it still answers: nothing else notices a pulled cable
LIGHT_CHECK_TIMEOUT_S = 0.5  # a live light answers in milliseconds; live view waits this long for a silent one
LOST = {  # a device that stopped answering during a job; it is looked for again every 2 s
    "camera": "The camera does not answer any more. It is connected again as soon as it is back.",
    "light": "The Scanlight does not answer any more. It is connected again as soon as it is back.",
}


class Hardware(QObject):
    """Owns the camera and the light. Lives on its own thread; everything arrives as a signal.

    The two are kept in separate slots: each is opened, retried and lost on its own, so a
    Scanlight that drops out (or is plugged in late) never costs the camera, whose reopening is
    the slow part on macOS (the system camera daemon may hold a free camera for minutes)."""

    frame = Signal(bytes)
    devices = Signal(str, str)
    temperature = Signal(object)  # the Scanlight's LED temperature in °C, None when it has sent none
    exposure = Signal(dict)  # iso, shutter, aperture as the camera reports them; None where it cannot
    message = Signal(str)
    busy = Signal(bool)
    calibration_step = Signal(object)  # a calibrate.Step: a shot with its histogram, or the shutter solved
    calibrated = Signal(str, str)  # the end of a calibration: (the preset's name, "") once saved, ("", why not) otherwise
    flat_ready = Signal(str)
    scanned = Signal(str, int)
    failed = Signal(str)
    # own-rgb: the operator sets the light to a colour before every shot. ("flat" or "scan", the
    # letter, why the last shot was refused); ("", "", "") when nothing is waiting for a shot.
    prompt = Signal(str, str, str)
    colour_shot = Signal(str, object, str)  # a colour of an own-rgb calibration: (letter, its ColourShot as a dict, "") or (letter, None, why not)
    doctor_line = Signal(str)  # a line of the doctor's report (diagnose.py), as it is found
    doctor_done = Signal(bool)  # the report is complete: whether the test shot decodes

    def __init__(self, sim: bool) -> None:
        super().__init__()
        self._rig = bench(sim)
        self._light = self._camera = None
        self._labels = {"camera": NOT_CONNECTED, "light": NOT_CONNECTED}
        self._next_try = {"camera": 0.0, "light": 0.0}
        self._problem = {"camera": "", "light": ""}
        self._timer: Optional[QTimer] = None
        self._next_exposure = 0.0
        self._card_check: Optional[float] = None
        self._missed = 0
        self._next_light_check = 0.0
        self._framing: Optional[tuple] = IDLE_LIGHT  # None: an own light's preset, nothing is sent
        self._frame_scan = None  # an own-rgb frame between its shots (session.FrameScan)
        self._flat: Optional[dict] = None  # an own-rgb flat field between its shots: folder, preset, planes, worked
        # Set by the window's Stop, straight from its thread: this thread is busy calibrating then,
        # and a queued signal would only arrive once the calibration is over.
        self.stop_calibration = threading.Event()

    @Slot()
    def start(self) -> None:
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(60)

    @Slot()
    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
        if self._frame_scan is not None:  # a window closed mid-frame: nothing of the frame stays in raw/
            self._frame_scan.cancel()
            self._frame_scan = None
        self._drop("light", say=False)
        self._drop("camera", say=False)

    @Slot(int)
    def set_interval(self, ms: int) -> None:
        """Live view slows down while a photo is looked at, and is there at once on return."""
        if self._timer:
            self._timer.setInterval(ms)

    # ----- the two slots -----

    def _drop(self, which: str, say: bool = True) -> None:
        device = self._light if which == "light" else self._camera
        if device is None:
            return
        try:
            device.close()
        except Exception:
            pass
        if which == "light":
            self._light = None
        else:
            self._camera = None
        self._labels[which] = NOT_CONNECTED
        self._next_try[which] = time.monotonic() + 2.0
        if say:
            self.devices.emit(self._labels["camera"], self._labels["light"])

    def _connect(self) -> None:
        """Opens whichever device is missing, each at most every 2 s, and says once why one failed."""
        now = time.monotonic()
        for which in ("light", "camera"):
            # Without a Scanlight the slot holds NoLight, and a Scanlight plugged in later is
            # still looked for, unless --no-light keeps TetherInvert off its port altogether.
            looking = isinstance(self._light, NoLight) and not no_light()
            if ((self._light if which == "light" else self._camera) is not None and not (which == "light" and looking)) or now < self._next_try[which]:
                continue
            self._next_try[which] = now + 2.0
            if which == "light" and no_light():  # never even look at the port: another program may be driving the Scanlight
                if not isinstance(self._light, NoLight):
                    self._light, self._labels["light"] = NoLight(), NO_LIGHT_LABEL
                    self.devices.emit(self._labels["camera"], self._labels["light"])
                continue
            try:
                if which == "light":
                    light = open_light(self._rig)
                    try:
                        if self._framing is not None:
                            light.set(*self._framing)
                        label = light.describe()
                    except Exception:
                        light.close()  # an open port that does not answer would make every retry find it busy
                        raise
                    self._labels["light"], self._light = label, light
                    self._next_light_check = now + LIGHT_CHECK_S
                else:
                    camera = open_camera(self._rig)
                    live = "" if getattr(camera, "has_live_view", True) else " (no live view: frame and focus on the camera)"
                    self._labels["camera"], self._camera = camera.model + live, camera
                    self._missed = 0
                    self._next_exposure = 0.0
                    self._card_check = time.monotonic() + 5.0  # the body reports a changed setting only after a moment
                self._problem[which] = ""
                self.devices.emit(self._labels["camera"], self._labels["light"])
            except Exception as exc:
                optional = which == "light"  # an own light needs no Scanlight: the slot holds NoLight, and this is news, not a failure
                if optional and self._light is None:
                    self._light, self._labels["light"] = NoLight(), NO_LIGHT_LABEL
                    self.devices.emit(self._labels["camera"], self._labels["light"])
                if str(exc) != self._problem[which]:  # say why once, not every two seconds
                    self._problem[which] = str(exc)
                    self.devices.emit(self._labels["camera"], self._labels["light"])
                    (self.message if optional else self.failed).emit(f"{MISSING[which]} ({exc})" + (" An own light needs none." if optional else ""))

    def _report_exposure(self) -> None:
        self._next_exposure = time.monotonic() + 3.0
        try:
            self.exposure.emit({key: self._camera.get(key) for key in ("iso", "shutter", "aperture")})
        except Exception:
            pass

    def _check_light(self) -> None:
        """Between jobs nothing else talks to the light, so a pulled cable would go unnoticed
        until the next scan; the camera's live view keeps running meanwhile."""
        self._next_light_check = time.monotonic() + LIGHT_CHECK_S
        try:
            degrees = self._light.temperature()  # before info(), which clears what has not been read yet
            self._light.info(timeout=LIGHT_CHECK_TIMEOUT_S)
        except Exception:
            self._drop("light")
            self.failed.emit(LOST["light"])
            return
        self.temperature.emit(degrees)

    def _tick(self) -> None:
        self._connect()
        if self._light is not None and not isinstance(self._light, NoLight) and time.monotonic() >= self._next_light_check:
            self._check_light()
        camera = self._camera
        if camera is None:  # live view and the exposure report need only the camera
            return
        if time.monotonic() >= self._next_exposure:
            self._report_exposure()
        if self._card_check is not None and time.monotonic() >= self._card_check:
            self._card_check = None
            try:
                if camera.keeps_card_copy():
                    self.message.emit(CARD_COPY)
            except Exception:
                pass
        if not getattr(camera, "has_live_view", True):
            return
        try:
            data = camera.preview()
        except Exception:
            data = None
        if data:
            self._missed = 0
            self.frame.emit(data)
            return
        self._missed += 1
        if self._missed > 40:  # a dropped frame is normal; this many in a row means the camera is gone
            self._missed = 0
            self._drop("camera")

    @Slot(object)
    def set_framing(self, levels: Optional[tuple]) -> None:
        """The Scanlight dimmed for framing; None for an own light's preset: nothing is sent then."""
        self._framing = tuple(levels) if levels is not None else None
        if self._light is not None and self._framing is not None:
            try:
                self._light.set(*self._framing)
            except Exception as exc:
                self.failed.emit(str(exc))

    # ----- jobs -----

    @Slot(str, str, object, object, object)
    def calibrate(self, name: str, kind: str, roi: Roi, by_hand: dict, extra: Optional[dict] = None) -> None:
        """Calibrate and save a preset. Always ends with `calibrated`, so the wizard knows.
        `extra`: an own light's `light_note`, and for own-rgb the `shots` of its colours."""
        saved: list[str] = []
        problem = self._job(lambda scanner: saved.append(self._calibrate(scanner, name, kind, roi, by_hand, extra or {})), kind)
        self.calibrated.emit(saved[0] if saved and not problem else "", problem or "")

    def _calibrate(self, scanner: Scanner, name: str, kind: str, roi: Roi, by_hand: dict, extra: dict) -> str:
        self.message.emit(f"Calibrating {name}…")
        note = extra.get("light_note", "")
        if KINDS[kind].prompted:  # its colours were shot at the wizard's prompts: only the preset is left to make
            preset = scanner.assemble(name, roi, kind, extra.get("shots", {}), by_hand=by_hand, light_note=note)
        else:
            set_by_hand(scanner.camera, kind, "O")  # only the simulator: the operator's light switched on as it is
            stop = self.stop_calibration.is_set
            preset = scanner.calibrate(name, roi, kind, cancelled=stop, by_hand=by_hand, progress=self.calibration_step.emit, light_note=note)
        PresetStore().save(preset)
        return preset.name

    @Slot(object, str, str)
    def calibrate_colour(self, roi: Roi, kind: str, letter: str) -> None:
        """One colour of an own-rgb calibration, the operator having set the light to it: its own job."""
        found: list = []

        def work(scanner: Scanner) -> None:
            set_by_hand(scanner.camera, kind, letter)  # only the simulator
            found.append(scanner.calibrate_colour(roi, letter, kind, progress=self.calibration_step.emit))

        problem = self._job(work, kind)
        self.colour_shot.emit(letter, found[0].__dict__ if found and not problem else None, problem or "")

    @Slot(str, object)
    def flatfield(self, folder: str, preset: Preset) -> None:
        if KINDS[preset.kind].prompted:  # own-rgb: colour by colour, each shot its own job at the operator's word
            self._flat = {"folder": folder, "preset": preset, "planes": {}, "worked": {}}
            self.prompt.emit("flat", KINDS[preset.kind].exposures[0], "")
            return
        self._job(lambda scanner: self._flatfield(scanner, folder, preset), preset.kind)

    def _flatfield(self, scanner: Scanner, folder: str, preset: Preset) -> None:
        self.message.emit("Capturing the flat field…")
        roll = Roll.open(Path(folder))
        set_by_hand(scanner.camera, preset.kind, "O")  # only the simulator
        with film_removed(scanner.camera):  # only the simulator needs this; at the rig the operator did it
            flat = scanner.capture_flatfield(roll, preset)  # which marks the roll as using it
        self.flat_ready.emit(f"Flat field captured: the light falls off by up to {flat.falloff():.0%}.")

    @Slot(str, object, bool, object)
    def scan(self, folder: str, preset: Preset, retake: bool, settings: dict) -> None:
        self._job(lambda scanner: self._scan(scanner, folder, preset, retake, settings), preset.kind)

    def _scan(self, scanner: Scanner, folder: str, preset: Preset, retake: bool, settings: dict) -> None:
        roll = Roll.open(Path(folder))
        roll.set_settings(**{key: settings[key] for key in ("rect", "crop", "flatfield", "compare")})  # the window's, stored by the job that uses them
        shots = scanner.begin_scan(roll, preset, retake, orientation=settings.get("orientation"), number=settings.get("number"))
        if shots.prompted:  # own-rgb: each shot waits for the operator, as its own job (`shoot`)
            self._frame_scan = shots
            self.prompt.emit("scan", shots.next_letter, "")
            return
        try:
            set_by_hand(scanner.camera, preset.kind, "O")  # only the simulator
            while not shots.complete:
                self.message.emit(f"Exposing {shots.next_letter}…")
                shots.shoot()
            frame = shots.finish()
        finally:
            shots.cancel()
        self.scanned.emit(folder, frame.number)

    @Slot()
    def shoot(self) -> None:
        """The shot the operator was prompted for: of an own-rgb flat field or frame."""
        # A wrong colour is asked for again (prompt); any other failure gives the flat field or the frame up.
        if self._flat is not None:
            flat = self._flat
            if self._job(lambda scanner: self._flat_shot(scanner, flat), flat["preset"].kind) and self._flat is flat:
                self.cancel_prompt()
        elif self._frame_scan is not None:
            shots = self._frame_scan
            if self._job(lambda scanner: self._scan_shot(scanner, shots), shots.preset.kind) and self._frame_scan is shots:
                self.cancel_prompt()

    def _flat_shot(self, scanner: Scanner, flat: dict) -> None:
        preset = flat["preset"]
        letter = next(letter for letter in KINDS[preset.kind].exposures if letter not in flat["planes"])
        set_by_hand(scanner.camera, preset.kind, letter)  # only the simulator
        try:
            with film_removed(scanner.camera):
                planes, worked = scanner.flat_colour(preset, letter)
        except WrongColour as exc:
            self.prompt.emit("flat", letter, str(exc))
            return
        flat["planes"].update(planes)
        flat["worked"].update(worked)
        waiting = [letter for letter in KINDS[preset.kind].exposures if letter not in flat["planes"]]
        if waiting:
            self.prompt.emit("flat", waiting[0], "")
            return
        roll = Roll.open(Path(flat["folder"]))
        made = scanner.finish_flatfield(roll, preset, flat["planes"], flat["worked"])  # may refuse (film in, flicker): cancel_prompt then
        self._flat = None
        self.prompt.emit("", "", "")
        self.flat_ready.emit(f"Flat field captured: the light falls off by up to {made.falloff():.0%}.")

    def _scan_shot(self, scanner: Scanner, shots) -> None:
        shots.use(scanner)  # the devices as connected now
        letter = shots.next_letter
        set_by_hand(scanner.camera, shots.preset.kind, letter)  # only the simulator
        self.message.emit(f"Exposing {EXPOSURES[letter].name}…")
        try:
            shots.shoot()
        except WrongColour as exc:
            self.prompt.emit("scan", letter, str(exc))
            return
        if not shots.complete:
            self.prompt.emit("scan", shots.next_letter, "")
            return
        frame = shots.finish()  # may fail (a full disk): the frame is still held, so cancel_prompt removes what was staged
        self._frame_scan = None
        self.prompt.emit("", "", "")
        self.scanned.emit(str(shots.roll.folder), frame.number)

    @Slot()
    def cancel_prompt(self) -> None:
        """Cancel frame / Cancel flat field: what was shot of it is removed."""
        if self._frame_scan is not None:
            self._frame_scan.cancel()
            self.message.emit(f"Frame {paths.label(self._frame_scan.number)} cancelled: nothing of it is kept.")
        if self._flat is not None:
            self.message.emit("Flat field cancelled.")
        self._frame_scan = self._flat = None
        self.prompt.emit("", "", "")

    @Slot()
    def doctor(self) -> None:
        """`tetherinvert doctor` on the devices this thread holds, so the window need not let go of
        the camera: what is installed, what is connected, a test shot and whether it decodes."""
        say = self.doctor_line.emit
        for line in versions():
            say(line)
        self._connect()
        say("")
        for which in ("camera", "light"):
            if (self._light if which == "light" else self._camera) is None:
                say(f"NOT CONNECTED: {MISSING[which]}" + (f" ({self._problem[which]})" if self._problem[which] else ""))
        self.busy.emit(True)
        decodes = False
        try:
            decodes = take_test_shot(self._camera, self._light, say)
        except Exception as exc:  # perhaps a device went away mid-shot: drop only the one that no longer answers
            say(f"\nRESULT: the test shot failed: {exc}")
            if self._camera is not None:
                self._drop_silent_devices()
        finally:
            self.set_framing(self._framing)
            if self._camera is not None:
                self._report_exposure()
            self.busy.emit(False)
            self.doctor_done.emit(decodes)

    def _job(self, work, kind: Optional[str] = None) -> Optional[str]:
        """Runs a job on a Scanner made from what is connected. A Scanlight kind needs both devices,
        an own light the camera only; a missing one is refused by name, and a Scanlight
        kind without a Scanlight by the Scanner (NEEDS_SCANLIGHT). Returns why the job did not
        get done (also sent as `failed`), or None."""
        needs = ("camera",) if kind is not None and not KINDS[kind].scanlight else ("camera", "light")
        self._connect()
        missing = [which for which in needs if (self._light if which == "light" else self._camera) is None]
        if "light" in needs and isinstance(self._light, NoLight):
            missing.append("scanlight")
        if missing:
            problem = " ".join(NEEDS_SCANLIGHT["switched off" if no_light() else "none"] if which == "scanlight" else MISSING[which] for which in missing)
            self.failed.emit(problem)
            return problem
        scanner = Scanner(self._light, self._camera, log=self.message.emit)
        self.busy.emit(True)
        problem = None
        try:
            work(scanner)
        except (CalibrationError, FlatFieldError, ScanError, KeyError) as exc:  # the rig is fine, the request was not
            problem = str(exc.args[0]) if isinstance(exc, KeyError) else str(exc)
            self.failed.emit(problem)
            self.set_framing(self._framing)
        except Exception as exc:  # perhaps a device went away mid-job: drop only the one that no longer answers
            problem = str(exc)
            self.failed.emit(problem)
            self._drop_silent_devices()
            self.set_framing(self._framing)
        finally:
            if self._camera is not None:
                self._report_exposure()
            self.busy.emit(False)
        return problem

    def _drop_silent_devices(self) -> None:
        """After a job failed for no reason of its own: ask each device whether it still answers
        and drop only the one that does not, so the camera is not reopened when only the Scanlight's
        cable moved."""
        if self._light is not None and not isinstance(self._light, NoLight):
            try:
                self._light.info(timeout=2.0)
            except Exception:
                self._drop("light")
                self.failed.emit(LOST["light"])
        try:
            self._camera.get("shutter")
        except Exception:
            self._drop("camera")
            self.failed.emit(LOST["camera"])


class Renderer(QObject):
    """Turns scanned frames into TIFFs, one after the other, off the scanning thread."""

    done = Signal(str)
    failed = Signal(str)
    finished = Signal()
    settled = Signal(str, object)  # folder, frame numbers: done with them, written or not
    printed = Signal(str)  # folder: prints written; the pictures are the same, the captions are not
    print_step = Signal(str, int, int)  # folder, prints done, prints in the batch: after each one
    sheet = Signal(str)  # the contact sheet's job is over: the PDF's path, or "" when it failed

    @Slot(str, int, bool)
    def frame(self, folder: str, number: int, negative: bool) -> None:
        try:
            roll = Roll.open(Path(folder))
            written = render(roll, roll.frame(number), negative=negative)
            self.done.emit("Wrote " + ", ".join(path.name for path in written))
            warning = clipping(roll, roll.frame(number))
            if warning:
                self.failed.emit(warning)
        except Exception as exc:
            self.failed.emit(f"Frame {paths.label(number)} could not be rendered: {exc}")
        finally:
            self.settled.emit(folder, [number])
            self.finished.emit()

    @Slot(str, bool, object)
    def roll(self, folder: str, negative: bool, settings: dict) -> None:
        numbers: list[int] = []
        try:
            roll = Roll.open(Path(folder))
            numbers = [frame.number for frame in roll.frames]
            roll.set_settings(**settings)  # the window's, stored by the job that uses them
            written = render_roll(roll, negative=negative)
            self.done.emit(f"Rendered {len(written)} files again from the RAW files.")
        except Exception as exc:
            self.failed.emit(f"The roll could not be rendered: {exc}")
        finally:
            self.settled.emit(folder, numbers)
            self.finished.emit()

    @Slot(str, int, str)
    def turn(self, folder: str, number: int, change: str) -> None:
        """A scanned frame another way up: `change` (orient.CHANGES) applied to the way it is
        stored now, which is read here, after any turn queued before it."""
        try:
            roll = Roll.open(Path(folder))
            now = orient.CHANGES[change](roll.frame(number).orientation)
            turned = reorient_frame(roll, number, now)
            self.done.emit(f"Frame {paths.label(number)} is {orient.label(now)} now: {len(turned)} file{'s' if len(turned) != 1 else ''} turned.")
        except Exception as exc:
            self.failed.emit(f"Frame {paths.label(number)} could not be turned: {exc}")
        finally:
            self.settled.emit(folder, [number])
            self.finished.emit()

    @Slot(str, object)
    def retag(self, folder: str, numbers: Optional[list]) -> None:
        """The metadata as it is in roll.json now, into the TIFFs of these frames (None: all).
        Here, after any render or print queued before: those write the metadata too."""
        try:
            files = retag(Roll.open(Path(folder)), numbers)
            self.done.emit(f"Metadata written into {len(files)} file{'s' if len(files) != 1 else ''}.")
        except Exception as exc:
            self.failed.emit(f"The metadata could not be written into the files: {exc}")
        finally:
            self.finished.emit()

    @Slot(str)
    def contact(self, folder: str) -> None:
        """The roll's contact sheet, from the previews: every frame as its print will be."""
        from .contact import write_sheet

        path = ""
        try:
            written, pages, missing = write_sheet(Roll.open(Path(folder)))
            path = str(written)
            self.done.emit(f"Contact sheet written: {', '.join(f.name for f in (written, *pages))}.")
            if missing:
                self.failed.emit(f"On the contact sheet without a picture (no preview; render them again): frame {', '.join(missing)}.")
        except Exception as exc:
            self.failed.emit(f"The contact sheet could not be written: {exc}")
        finally:
            self.sheet.emit(path)
            self.finished.emit()

    @Slot(str)
    def prints(self, folder: str) -> None:
        """Every print of the roll that is out of date, made from the TIFFs already written."""
        try:
            problem = ""
            try:
                written = write_prints(Roll.open(Path(folder)), lambda done, total: self.print_step.emit(folder, done, total))
            except PrintsError as exc:
                written, problem = exc.written, str(exc)
            self.done.emit(f"Wrote {len(written)} print{'s' if len(written) != 1 else ''}.")
            if problem:
                self.failed.emit(problem)
        except Exception as exc:
            self.failed.emit(f"The prints could not be written: {exc}")
        finally:
            self.printed.emit(folder)
            self.finished.emit()
