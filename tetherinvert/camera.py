"""Tethered camera through libgphoto2 (python-gphoto2). macOS and Linux; no vendor SDK.

One PTP session serves settings, live view and stills, so every call takes the same lock.
The guards below exist because of how libgphoto2 behaves, not out of caution:

* A choice widget with no choices (aperture on a manual lens) crashes the process when its
  value is read, so it is never read.
* Property writes are asynchronous: a write counts only once the body reports it back.
* The events a still leaves behind must be consumed, or the next capture fails. They are
  drained only after a still: draining an idle Canon body crashes it.
* The downloaded file is deleted from the body, or some bodies fill up and stop shooting.
* The body is asked not to keep stills on its card. Some take that only from their own menu.
* A buffer handed out by the binding is only valid while the object it came from is alive.
* Choice labels are translated by gettext, so the message locale is pinned to English.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .raw import sniff

# Vendors name the aperture differently; ISO and shutter are the same everywhere.
PROPERTIES = {"iso": ("iso",), "shutter": ("shutterspeed",), "aperture": ("f-number", "aperture")}
RAW_SUFFIXES = {
    ".arw", ".cr2", ".cr3", ".crw", ".dng", ".erf", ".iiq", ".nef", ".nrw", ".orf",
    ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srw", ".x3f", ".3fr",
}  # fmt: skip
WRITE_SETTLE_S = 3.0
CLAIMED = -53  # GP_ERROR_IO_USB_CLAIM: another program holds the camera


CARD_COPY = (
    "The camera also keeps every exposure on its memory card: it did not take the request to send them "
    "to the computer only. Set that on the camera. Sony: MENU → Network → Cnct./PC Remote → "
    "Remote Shoot Setting → Still Img. Save Dest. → Destination Only."
)


class CameraError(RuntimeError):
    pass


def load_gphoto() -> Any:
    os.environ["LANGUAGE"] = "C"
    try:
        import gphoto2
    except ImportError as exc:
        raise CameraError("python-gphoto2 is not installed: pip install gphoto2 (macOS and Linux only)") from exc
    return gphoto2


class GphotoCamera:
    def __init__(self, gp: Any = None) -> None:
        self._gp = gp or load_gphoto()
        self._camera: Any = None
        self._lock = threading.RLock()
        self._names: dict[str, Optional[str]] = {}
        self._can_preview = True
        self.model = ""
        self.last_name = ""  # the camera's own name for the last still

    @property
    def has_live_view(self) -> bool:
        return self._can_preview

    # ----- session -----

    def open(self) -> None:
        with self._lock:
            if self._camera is not None:
                return
            camera = self._gp.Camera()
            try:
                camera.init()
            except self._gp.GPhoto2Error as exc:
                if getattr(exc, "code", None) == CLAIMED:
                    raise CameraError(
                        "Another program holds the camera. On macOS that is usually the system camera "
                        "daemon; unplug and replug the cable, and quit Photos, Image Capture or sync clients. "
                        "On Linux it is usually the file manager: unmount the camera there."
                    ) from exc
                raise CameraError(f"could not open the camera (is it in PC Remote mode?): {exc}") from exc
            self._camera = camera
            self._names = {}
            self.model = self._read_model()
            self._can_preview = self._read_preview_ability()
            self._prefer_memory_capture()

    def close(self) -> None:
        with self._lock:
            if self._camera is not None:
                try:
                    self._camera.exit()
                except Exception:
                    pass
                self._camera = None

    def _require(self) -> Any:
        if self._camera is None:
            self.open()
        return self._camera

    def _read_model(self) -> str:
        try:
            for line in str(self._camera.get_summary()).splitlines():
                if line.lower().startswith("model"):
                    return line.split(":", 1)[-1].strip()
        except Exception:
            pass
        return "camera"

    def _read_preview_ability(self) -> bool:
        """False only when the driver entry says there is no live view. Asking such a body
        for a preview anyway can wedge its session until it is power-cycled."""
        try:
            operations = int(self._camera.get_abilities().operations)
            return bool(operations & self._gp.GP_OPERATION_CAPTURE_PREVIEW)
        except Exception:
            return True

    def _prefer_memory_capture(self) -> None:
        """Ask the body to hand stills to the computer and not to keep them on its card.

        Some bodies take that only from their own menu. Then a setting that already reaches the
        computer is left as it is (never swapped for another one), and `keeps_card_copy()`
        says that the card is written too."""
        try:
            offered = self._choices_of(self._camera.get_single_config("capturetarget"))
        except self._gp.GPhoto2Error:
            return
        to_computer = [choice for choice in offered if re.search(r"\bram\b|sdram", choice, re.IGNORECASE)]
        only = [choice for choice in to_computer if "card" not in choice.lower()]
        if any(self._write("capturetarget", choice) for choice in only):
            return
        try:
            current = self._value_of(self._camera.get_single_config("capturetarget"))
        except self._gp.GPhoto2Error:
            return
        if current in to_computer:
            return
        for choice in to_computer:  # computer and card: better than card alone
            if choice not in only and self._write("capturetarget", choice):
                return

    def keeps_card_copy(self) -> Optional[bool]:
        """True when the body also writes every still to its memory card; None when it does not say."""
        with self._lock:
            try:
                value = self._value_of(self._require().get_single_config("capturetarget"))
            except self._gp.GPhoto2Error:
                return None
        return "card" in value.lower() if value else None

    # ----- settings -----

    def _name(self, key: str) -> Optional[str]:
        if key not in self._names:
            self._names[key] = None
            for name in PROPERTIES[key]:
                try:
                    self._require().get_single_config(name)
                except self._gp.GPhoto2Error:
                    continue
                self._names[key] = name
                break
        return self._names[key]

    def _choices_of(self, widget: Any) -> list[str]:
        if widget.get_type() not in (self._gp.GP_WIDGET_RADIO, self._gp.GP_WIDGET_MENU):
            return []
        return [widget.get_choice(i) for i in range(widget.count_choices())]

    def _value_of(self, widget: Any) -> Optional[str]:
        if widget.get_type() in (self._gp.GP_WIDGET_RADIO, self._gp.GP_WIDGET_MENU) and widget.count_choices() == 0:
            return None  # reading it would crash the process
        return widget.get_value()

    def choices(self, key: str) -> list[str]:
        """What the body offers for 'iso', 'shutter' or 'aperture'; empty when it offers nothing."""
        with self._lock:
            name = self._name(key)
            if name is None:
                return []
            found = self._choices_of(self._require().get_single_config(name))
            if key == "iso":
                found = [c for c in found if c.isdigit()] or found  # drop Auto and multi-frame modes
            return found

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            name = self._name(key)
            if name is None:
                return None
            return self._value_of(self._require().get_single_config(name))

    def set(self, key: str, value: str) -> bool:
        """Write a setting and wait until the body reports it. False when it does not take."""
        with self._lock:
            name = self._name(key)
            return name is not None and self._write(name, value)

    def _write(self, name: str, value: str, settle_s: Optional[float] = None) -> bool:
        camera = self._require()
        settle_s = WRITE_SETTLE_S if settle_s is None else settle_s
        try:
            widget = camera.get_single_config(name)
            if self._value_of(widget) == value:
                return True
            offered = self._choices_of(widget)
            if offered and value not in offered:
                return False
            widget.set_value(value)
            camera.set_single_config(name, widget)
            deadline = time.monotonic() + settle_s
            while time.monotonic() < deadline:
                if self._value_of(camera.get_single_config(name)) == value:
                    return True
                time.sleep(0.1)
        except self._gp.GPhoto2Error:
            pass
        return False

    def report(self) -> list[tuple[str, str]]:
        """Every choice setting the camera exposes, as (name, value), for diagnosis."""
        found: list[tuple[str, str]] = []

        def walk(widget: Any) -> None:
            for index in range(widget.count_children()):
                child = widget.get_child(index)
                if child.count_children():
                    walk(child)
                elif child.get_type() in (self._gp.GP_WIDGET_RADIO, self._gp.GP_WIDGET_MENU) and child.count_choices():
                    found.append((child.get_name(), str(child.get_value())))

        with self._lock:
            try:
                walk(self._require().get_config())
            except Exception as exc:  # diagnosis must never be the thing that fails
                found.append(("(settings could not be listed)", str(exc)))
        return found

    # ----- stills and live view -----

    def capture(self, stem: Path, check: bool = True) -> Path:
        """Take one RAW and write it to `stem` plus the camera's own suffix. Returns the path.

        `check=False` saves whatever the camera hands over, for diagnosis.
        """
        gp = self._gp
        with self._lock:
            camera = self._require()
            try:
                try:
                    taken = camera.capture(gp.GP_CAPTURE_IMAGE)
                    suffix = os.path.splitext(taken.name)[1]
                    if check and suffix.lower() not in RAW_SUFFIXES:
                        raise CameraError(f"the camera returned {suffix or 'no suffix'}, not a RAW; set image quality to RAW only")
                    # The buffer belongs to this file object and dies with it, so the object needs a
                    # name that outlives the copy. Chained into one expression it is freed first,
                    # and the first 32 bytes of the copy are garbage.
                    downloaded = camera.file_get(taken.folder, taken.name, gp.GP_FILE_TYPE_NORMAL)
                    data = bytes(memoryview(downloaded.get_data_and_size()))
                    del downloaded
                    self.last_name = taken.name
                    try:
                        camera.file_delete(taken.folder, taken.name)
                    except gp.GPhoto2Error:
                        pass
                finally:
                    self._drain_events()
            except gp.GPhoto2Error as exc:
                raise CameraError(f"capture failed: {exc}") from exc
        if check and sniff(data[:16]) in ("JPEG", "HEIF"):
            raise CameraError(
                f"the camera sent a {sniff(data[:16])} picture named {taken.name}; set image quality to RAW only, "
                "and in its PC Remote settings let RAW be the file that is sent to the computer"
            )
        if check and len(data) < 100_000:
            raise CameraError(f"the camera sent only {len(data)} bytes for {taken.name}; check the cable and try again")
        path = Path(str(stem) + suffix)  # not with_suffix: a roll name may contain dots
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".part")
        partial.write_bytes(data)
        os.replace(partial, path)
        return path

    def _drain_events(self) -> None:
        quiet_ms, budget_s = (300, 2.0) if "fuji" in self.model.lower() else (50, 1.0)
        deadline = time.monotonic() + budget_s
        try:
            while time.monotonic() < deadline:
                kind, _ = self._camera.wait_for_event(quiet_ms)
                if kind == self._gp.GP_EVENT_TIMEOUT:
                    return
        except self._gp.GPhoto2Error:
            pass

    def preview(self) -> Optional[bytes]:
        """One live-view JPEG, or None when the body has no live view or dropped the frame."""
        if not self._can_preview:
            return None
        with self._lock:
            try:
                frame = self._require().capture_preview()
                return bytes(memoryview(frame.get_data_and_size())) or None
            except self._gp.GPhoto2Error:
                return None
