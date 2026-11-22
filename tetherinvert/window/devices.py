"""The camera and the light, as the hardware thread reports them (workers.Hardware): connected or not,
and the light's LED temperature. They open as a popover from the rail's foot, whose dot is green
while both are there and red while one is missing, where they are needed: on Scan and Roll, in the
wizard, and anywhere during a session (Space scans from every step). On Develop, Finish and the
Presets list nothing uses them, and the dot is grey. What the camera is set to
now is on Scan.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget

from ..kinds import KINDS
from ..session import NO_LIGHT_LABEL
from ..workers import NOT_CONNECTED
from . import style
from .viewer import LIVE


class DevicesPanel(QWidget):
    """A row each; `status`, the chip beside the popover's title, and `note` say whether they are
    there and whether that matters now."""

    def __init__(self) -> None:
        super().__init__()
        self.status = style.label("", "chip", wrap=False)
        self.camera_label, self.light_label = style.label("…"), style.label("…")
        self.temperature_label = style.label("—")  # its own row: added to the light's label it would wrap, and the row not grow
        self.note = style.label("", "muted")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Camera", self.camera_label)
        form.addRow("Light", self.light_label)
        form.addRow("LED temp.", self.temperature_label)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addLayout(form)
        column.addWidget(self.note)


class DevicesPart:
    """A part of MainWindow (mainwindow.py): it works with the window's widgets and the other parts' state."""

    def _init_devices(self) -> None:
        self._camera: dict = {}  # what the camera reports for iso, shutter, aperture
        self._camera_connected = False
        self._light_text = NOT_CONNECTED  # what the hardware thread says of the light
        self._light_missing = True  # the light's label says not connected

    def _on_devices(self, camera: str, light: str) -> None:
        panel = self.devices_panel
        self._light_text = light
        if light == NOT_CONNECTED:
            panel.temperature_label.setText("—")
        self._camera_connected = camera != NOT_CONNECTED
        panel.camera_label.setText(camera)
        style.set_tone(panel.camera_label, "danger" if camera == NOT_CONNECTED else "ok")
        if camera == NOT_CONNECTED:  # the last live picture would otherwise stay up as if it were live
            self.view.no_camera()
            self.strip.set_icon(LIVE, None)
            self.live_page.focus_label.setText("Focus —")
            self.scan_page.exposure_label.setText("—")
            self._camera = {}
        self._show_light()
        if self._calibrating():
            self._update_buttons()  # W2 waits for the camera

    def _show_light(self) -> None:
        """The light's label. No Scanlight is fine for an own light's preset: "none: own light",
        plain. For a Scanlight preset, or none chosen, it is a red warning."""
        preset = self._scanning_preset()
        own = preset is not None and not KINDS[preset.kind].scanlight
        text = NOT_CONNECTED if self._light_text == NO_LIGHT_LABEL and not own else self._light_text
        self.devices_panel.light_label.setText(text)
        style.set_tone(self.devices_panel.light_label, "danger" if text == NOT_CONNECTED else "" if text == NO_LIGHT_LABEL else "ok")
        self._light_missing = text == NOT_CONNECTED
        self._update_devices()

    def _devices_needed(self) -> bool:
        """Where the camera and the light are used: scanning, starting a session (its flat field) and
        calibrating; during a session anywhere, since Space scans from every step."""
        return self._in_session() or self._calibrating() or self._step in ("scan", "roll")

    def _update_devices(self) -> None:
        """The popover's chip and line, and the rail's dot: grey where the devices are not needed."""
        panel = self.devices_panel
        missing = [name for name, gone in (("Camera", not self._camera_connected), ("light", self._light_missing)) if gone]
        if not self._devices_needed():
            chip, tone = "Not needed here", "off"
            panel.note.setText("Not needed to develop or finish the roll. Scan, Roll and a new preset need them.")
        else:
            chip, tone = (" and ".join(missing).capitalize() + " missing", "danger") if missing else ("Ready", "ok")
            panel.note.setText("Needed to scan, to start a session and to calibrate.")
        panel.status.setText(chip)
        style.set_tone(panel.status, tone)
        self.rail.buttons["devices"].set_dot(tone)

    def _on_temperature(self, degrees: Optional[float]) -> None:
        if self._light_text != NOT_CONNECTED:
            self.devices_panel.temperature_label.setText("—" if degrees is None else f"{degrees:.1f} °C")

    def _on_exposure(self, values: dict) -> None:
        known = self._calibrating() and bool(self._camera)
        self._camera = values
        aperture = values.get("aperture") or "aperture on the lens"
        self.scan_page.exposure_label.setText(f"ISO {values.get('iso') or '?'} · {values.get('shutter') or '?'} s · {aperture}")
        if self._calibrating() and not known:
            self._update_buttons()  # W2 now knows what the camera reports
