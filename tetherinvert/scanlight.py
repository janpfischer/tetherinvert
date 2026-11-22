"""Scanlight (jackw01) over USB serial. v2/v3, v4 and the Big Scanlight share one protocol.

Packet, both directions: 0xFE, header, payload length, payload.

    host -> device   0  SET_COLOR       r, g, b, w, ir, save   (one byte each, 0-255)
                     1  GET_DEFAULT_RGB no payload
                     2  GET_FW_VERSION  no payload
    device -> host   1  LED_TEMP        int32 big-endian, millidegrees C, about every 200 ms
                     3  FW_VERSION      uint32 big-endian: firmware id low 16 bits, hardware id high
                     4  DEFAULT_RGB     r, g, b

The white LED cannot be lit together with R, G or B.

The light keeps a colour default in flash (firmware `rgb_preset`): what its buttons light in RGB
mode, set in the Scanlight web app by lining up the channels on clear film. Only a SET_COLOR with
`save` not 0 changes it. TetherInvert reads it (`default_rgb`, for the rgb-together kind) and always
sends `save` = 0. A light whose default was never set reads 255, 255, 255: the firmware puts that
in an empty flash, so "not set" cannot be told from "set to full".
"""

from __future__ import annotations

import time
from typing import Optional

START = 0xFE
SET_COLOR, GET_DEFAULT_RGB, GET_FW_VERSION = 0, 1, 2
LED_TEMP, FW_VERSION, DEFAULT_RGB = 1, 3, 4

# Every Scanlight enumerates as the stock Raspberry Pi Pico CDC device.
PICO_VID, PICO_PIDS = 0x2E8A, {0x000A, 0x0009}
HARDWARE = {0: "Big Scanlight", 1: "Scanlight v4a", 2: "Scanlight v2/v3", 3: "Scanlight v4b"}
WHITE_CAPABLE = {0, 1, 3}
CLOSE_CONFIRM_S = 1.0  # seconds close() waits for the light to confirm it has taken "off"


class LightError(RuntimeError):
    pass


def find_port() -> str:
    """The Scanlight's serial port, or raise with the candidates listed."""
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    pico = [p.device for p in ports if p.vid == PICO_VID and p.pid in PICO_PIDS]
    if len(pico) == 1:
        return pico[0]
    if not pico:
        raise LightError("No Scanlight found. Use its data USB-C port (the other one is power only).")
    raise LightError("Several Pico serial ports found, pass one explicitly: " + ", ".join(pico))


def packet(header: int, payload: bytes = b"") -> bytes:
    return bytes([START, header, len(payload)]) + payload


class Scanlight:
    def __init__(self, port: Optional[str] = None, serial_obj=None) -> None:
        if serial_obj is None:
            import serial

            serial_obj = serial.Serial(port or find_port(), baudrate=115200, timeout=0.1)
        self._serial = serial_obj
        self._buffer = bytearray()
        self._temperature: Optional[float] = None
        self.color = (0, 0, 0, 0)

    def set(self, r: int = 0, g: int = 0, b: int = 0, w: int = 0) -> None:
        values = [max(0, min(255, int(v))) for v in (r, g, b, w)]
        if values[3] and any(values[:3]):
            raise ValueError("white cannot be lit together with R, G or B")
        self._serial.write(packet(SET_COLOR, bytes(values + [0, 0])))  # ir = 0, save = 0: never overwrite the light's colour default
        self.color = tuple(values)

    def off(self) -> None:
        self.set(0, 0, 0, 0)

    def info(self, timeout: float = 2.0) -> tuple[int, int]:
        """(firmware id, hardware id). Raises LightError when the light does not answer."""
        word = int.from_bytes(self._ask(GET_FW_VERSION, FW_VERSION, 4, timeout), "big")
        return word & 0xFFFF, word >> 16

    def default_rgb(self, timeout: float = 2.0) -> tuple[int, int, int]:
        """The colour default stored in the light: r, g, b. Raises LightError when it does not answer."""
        r, g, b = self._ask(GET_DEFAULT_RGB, DEFAULT_RGB, 3, timeout)
        return r, g, b

    def _ask(self, question: int, answer: int, length: int, timeout: float) -> bytes:
        """Send a question and wait for its answer's first `length` bytes; telemetry in between is kept."""
        self._buffer.clear()
        if hasattr(self._serial, "reset_input_buffer"):
            self._serial.reset_input_buffer()
        self._serial.write(packet(question))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for header, data in self._read():
                if header == answer and len(data) >= length:
                    return data[:length]
        raise LightError("the Scanlight did not answer")

    def describe(self) -> str:
        firmware, hardware = self.info()
        return f"{HARDWARE.get(hardware, f'hw{hardware}')} (fw {firmware})"

    def has_white(self) -> bool:
        return self.info()[1] in WHITE_CAPABLE

    def temperature(self) -> Optional[float]:
        """Latest LED temperature in degrees C from the telemetry received so far, if any."""
        self._read()
        return self._temperature

    def close(self) -> None:
        try:
            self.off()
            # Closing the port right after "off" can lose it: with nothing else to close in between
            # (`tetherinvert light` opens no camera) the light stays on in about half the tries, and
            # waiting for the bytes to leave (flush) is not enough. The light handles packets in
            # order, so its answer to a question sent after "off" means it has taken "off".
            try:
                self.info(timeout=CLOSE_CONFIRM_S)
            except Exception:
                pass  # a light that does not answer is closed anyway
        finally:
            self._serial.close()

    def _read(self) -> list[tuple[int, bytes]]:
        """Read what is waiting and return every complete packet."""
        self._buffer.extend(self._serial.read(256))
        packets = []
        buf = self._buffer
        while True:
            start = buf.find(bytes([START]))
            if start < 0:
                buf.clear()
                break
            del buf[:start]
            if len(buf) < 3 or len(buf) < 3 + buf[2]:
                break
            end = 3 + buf[2]
            header, data = buf[1], bytes(buf[3:end])
            del buf[:end]
            if header == LED_TEMP and len(data) >= 4:
                self._temperature = int.from_bytes(data[:4], "big", signed=True) / 1000.0
            packets.append((header, data))
        return packets
