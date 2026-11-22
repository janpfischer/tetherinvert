"""`tetherinvert doctor`: one report that says what is installed, what is connected, and whether a
shot from this camera can be decoded. Nothing is changed on the camera."""

from __future__ import annotations

import platform
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable

from . import __version__
from .paths import home
from .raw import decoder_version, explain, sniff

INTERESTING = re.compile(r"quality|size|format|raw|compress|target|dest|mode|program|drive|effect|aspect", re.IGNORECASE)


def versions() -> list[str]:
    lines = [f"TetherInvert {__version__}, Python {sys.version.split()[0]}, {platform.platform()}", f"decoder: {decoder_version()}"]
    try:
        import gphoto2

        lines.append(f"camera library: libgphoto2 {gphoto2.gp_library_version(gphoto2.GP_VERSION_SHORT)[0]}, python-gphoto2 {gphoto2.__version__}")
    except Exception as exc:
        lines.append(f"camera library: not usable ({exc})")
    for name in ("numpy", "tifffile", "opencv-python-headless", "pyserial", "PySide6"):
        try:
            lines.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:
            lines.append(f"{name}: not installed")
    return lines


def describe_raw(path: Path) -> tuple[bool, list[str]]:
    """(decodes, report lines) for one file from the camera."""
    import rawpy

    size = path.stat().st_size
    with open(path, "rb") as handle:
        head = handle.read(16)
    lines = [f"size: {size / 1e6:.2f} MB", f"first bytes: {head.hex(' ')}  ({sniff(head)})"]
    try:
        with rawpy.imread(str(path)) as raw:
            pattern = "none (not a mosaic)" if raw.raw_pattern is None else "".join(raw.color_desc.decode()[i] for i in raw.raw_pattern.flatten())
            lines.append(f"opens: yes, {raw.sizes.raw_width} x {raw.sizes.raw_height}, pattern {pattern}, type {raw.raw_type.name}")
            per_channel = list(raw.camera_white_level_per_channel or [])
            lines.append(f"black {list(raw.black_level_per_channel)}, white {raw.white_level}, per-channel white {per_channel}")
            image = raw.postprocess(half_size=raw.raw_pattern is not None, gamma=(1, 1), no_auto_bright=True, output_bps=16, user_flip=0)
            means = image.reshape(-1, image.shape[-1]).mean(axis=0)
            lines.append(f"decodes: yes, mean signal R {means[0] / 655.35:.1f} %  G {means[1] / 655.35:.1f} %  B {means[2] / 655.35:.1f} %")
        return True, lines
    except rawpy.LibRawError as exc:
        lines.append("decodes: NO")
        lines.append(explain(path, exc.args[0] if exc.args else exc))
        return False, lines


def doctor(sim: bool, say: Callable[[str], None] = print) -> int:
    """Run the checks. Returns 0 when a test shot decodes. Each device is opened on its own, so a
    missing Scanlight is reported and the camera still checked; without a camera there is no
    test shot, so that fails."""
    from .session import bench, open_device

    for line in versions():
        say(line)
    rig = bench(sim)
    devices = {}
    say("")
    for which in ("camera", "light"):
        try:
            devices[which] = open_device(rig, which)
        except RuntimeError as exc:
            say(f"NOT CONNECTED: {exc}")
    light, camera = devices.get("light"), devices.get("camera")
    try:
        return 0 if take_test_shot(camera, light, say) else 1
    finally:
        try:
            if light is not None:
                light.off()
                light.close()
        finally:
            if camera is not None:
                camera.close()


def take_test_shot(camera, light, say: Callable[[str], None]) -> bool:
    """The checks on devices already open: by `doctor`, or by the window's hardware thread (Help ›
    Run Doctor). Whether the shot decodes; leaves the light at the test shot's level and both open."""
    if camera is None:
        say("\nRESULT: no camera, so no test shot.")
        return False
    say(f"camera: {camera.model}")
    say(f"light:  {light.describe() if light is not None else 'not connected: the test shot is taken in whatever light there is'}")
    for key in ("iso", "shutter", "aperture"):
        say(f"{key}: {camera.get(key) or 'not settable from here'}")

    if light is not None:
        light.set(30, 30, 30)
    say("\ntest shot at the camera's current exposure…")
    folder = home() / "doctor"
    for old in folder.glob("test-shot*"):
        old.unlink()
    path = camera.capture(folder / "test-shot", check=False)
    say(f"camera's name for it: {getattr(camera, 'last_name', '?')}")
    say(f"saved as: {path}")
    decodes, lines = describe_raw(path)
    for line in lines:
        say(line)

    from .cli import stills_go_to

    say(f"stills go to: {stills_go_to(camera)}")
    say("\ncamera settings that affect the file:")
    for name, value in camera.report():
        if INTERESTING.search(name) or name.startswith("("):
            say(f"  {name} = {value}")
    say("\nRESULT: " + ("this camera's files decode; scanning should work." if decodes else "the test shot does not decode, see above."))
    return decodes
