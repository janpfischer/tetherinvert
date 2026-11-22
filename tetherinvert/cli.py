"""Command line: everything the window does, scriptable.

    tetherinvert status
    tetherinvert doctor
    tetherinvert calibrate "Portra 400" --base 0.03,0.30,0.03,0.40
    tetherinvert flatfield ~/Scans/Roll01 --preset "Portra 400"
    tetherinvert scan ~/Scans/Roll01 --preset "Portra 400" --picture 0.07,0.10,0.86,0.80
    tetherinvert render ~/Scans/Roll01 --matched
    tetherinvert metadata ~/Scans/Roll01 camera="Nikon FM2" film="HP5 Plus" ei=800
    tetherinvert contactsheet ~/Scans/Roll01 --sprockets
    tetherinvert lightroom ~/Scans/Roll01/scans ~/Scans/Roll01/prints
    tetherinvert light
    tetherinvert gui

Rectangles are x,y,w,h in fractions of the frame. Add --sim to run without the rig, --no-light to
leave the Scanlight alone and scan with an own light.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import metadata, paths
from .calibrate import CalibrationError, Roi, WrongColour
from .invert import BASES
from .kinds import EXPOSURES, KINDS
from .session import (
    NoLight,
    PresetStore,
    PrintsError,
    Roll,
    ScanError,
    Scanner,
    clean_raws,
    bench,
    clipping,
    delete_frame,
    frame_files,
    open_device,
    render,
    render_roll,
    reorient_frame,
    retag,
    write_prints,
)


def stills_go_to(camera) -> str:
    """Where the camera puts each exposure, in words."""
    from .camera import CARD_COPY

    kept = camera.keeps_card_copy()
    if kept is None:
        return "the camera does not say where it saves them"
    return "computer AND memory card. " + CARD_COPY if kept else "computer only, nothing on the memory card"


def reminder(preset) -> str:
    """The preset's settings on one line, saying which ones the operator has to set."""
    return ", ".join(f"{name} {value}" + (" (SET BY HAND)" if by_hand else "") for name, value, by_hand in preset.settings())


NEGATIVE_HELP = "also save the linear negative (not inverted, gamma 1.0) in the roll's negative/ folder"


def rectangle(text: str) -> list[float]:
    values = [float(part) for part in text.split(",")]
    if len(values) != 4 or not all(0 <= v <= 1 for v in values):
        raise argparse.ArgumentTypeError("expected x,y,w,h as fractions between 0 and 1")
    return values


def frame_number(text: str) -> int:
    """A frame as on the film: 00, 0, 1, 2, …"""
    try:
        return paths.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tetherinvert", description="Tethered film scanning with a Scanlight, straight to 16-bit TIFF.")
    parser.add_argument("--sim", action="store_true", help="use the simulated camera and light")
    parser.add_argument(
        "--no-light", action="store_true", help="never open the Scanlight's port: scan with an own light, or one another program drives (TETHERINVERT_NO_LIGHT=1)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="show the camera, the light and the camera's exposure")
    commands.add_parser("doctor", help="check the installation and whether a test shot from this camera decodes")
    commands.add_parser("presets", help="list the saved film presets")
    commands.add_parser("gui", help="open the window")

    light = commands.add_parser(
        "light", help="set the LEDs, e.g. 'light 30 30 30' or 'light off'; with no levels, read commands (r, g, b, w [level], rgb R G B, off, q)"
    )
    light.add_argument("levels", nargs="*", help="R G B [W], each 0-255, or 'off'")

    preview = commands.add_parser("preview", help="save one live-view frame as a JPEG")
    preview.add_argument("file", type=Path)

    calibrate = commands.add_parser("calibrate", help="meter the film base and save a preset")
    calibrate.add_argument("name", help="preset name, usually the film stock")
    calibrate.add_argument("--base", type=rectangle, required=True, metavar="X,Y,W,H", help="a patch of clear film base")
    lighting = calibrate.add_mutually_exclusive_group()
    lighting.add_argument("--kind", choices=list(KINDS), help="how the film is lit (default rgb); own-…: your own light, set by hand")
    lighting.add_argument("--white", action="store_true", help="one white-light exposure (black-and-white film) instead of R, G, B")
    lighting.add_argument(
        "--together", action="store_true", help="one colour exposure, R, G and B lit together at the light's stored colour default; only the shutter is calibrated"
    )
    calibrate.add_argument("--light", dest="light_note", help="an own light: what it is and how it is set, e.g. 'Kaiser slimlite plano, full'")
    calibrate.add_argument("--aperture", help="the f-stop, for a lens the camera cannot read (e.g. f/8)")
    calibrate.add_argument("--iso", help="the ISO, if the camera does not report it")

    flat = commands.add_parser("flatfield", help="photograph the bare light (no film) as the roll's flat field")
    flat.add_argument("roll", type=Path)
    flat.add_argument("--preset", required=True)
    flat.add_argument("--yes", action="store_true", help="do not wait for confirmation that the film is out")

    remove = commands.add_parser("delete", help="delete one frame of a roll: its RAW files, TIFFs, print and preview")
    remove.add_argument("roll", type=Path)
    remove.add_argument("frame", type=frame_number, help="as on the film: 00, 0, 1, …")
    remove.add_argument("--yes", action="store_true", help="do not ask")

    turn = commands.add_parser("orient", help="store a scanned frame another way up: its TIFFs and print are turned in place")
    turn.add_argument("roll", type=Path)
    turn.add_argument("frame", type=frame_number, help="as on the film: 00, 0, 1, …")
    turn.add_argument("--turn", type=int, choices=(0, 90, 180, 270), default=0, help="clockwise from the way the camera saw it")
    turn.add_argument("--mirror", action="store_true", help="mirrored left to right (after turning)")

    clean = commands.add_parser("clean", help="delete the RAW files of every frame that has a TIFF")
    clean.add_argument("roll", type=Path)
    clean.add_argument("--yes", action="store_true", help="do not ask")

    hot = commands.add_parser("lightroom", help="open Lightroom Classic and have it add the TIFFs of folders as they appear")
    hot.add_argument("folders", type=Path, nargs="*", metavar="folder", help="the folders to watch: a roll's scans/, prints/ or both")
    hot.add_argument("--stop", action="store_true", help="tell Lightroom Classic to stop watching")

    scan = commands.add_parser("scan", help="scan the frame in the holder into a roll folder")
    scan.add_argument("roll", type=Path, help="roll folder; its name becomes the file name prefix")
    scan.add_argument("--preset", required=True)
    scan.add_argument("--negative", action="store_true", help=NEGATIVE_HELP)
    scan.add_argument("--retake", action="store_true", help="replace the last frame instead of adding one")
    scan.add_argument(
        "--number", type=frame_number, help="the number on the film next to this frame (00, 0, 1, …); a number the roll has is rescanned. Default: counts on"
    )
    scan.add_argument("--picture", type=rectangle, metavar="X,Y,W,H", help="the picture area; set once per roll")
    scan.add_argument("--compare", action=argparse.BooleanOptionalAction, help="also write each file without the flat field")
    scan.add_argument("--turn", type=int, choices=(0, 90, 180, 270), default=0, help="turn the frame's files clockwise by this many degrees")
    scan.add_argument("--mirror", action="store_true", help="mirror the frame's files left to right (after turning)")

    prints = commands.add_parser("prints", help="write every frame's print (prints/) that is missing or out of date")
    prints.add_argument("roll", type=Path)
    prints.add_argument("--match", action=argparse.BooleanOptionalAction, help="develop with points shared across the roll, so it matches (kept for the roll)")

    about = commands.add_parser("metadata", help="show or set a roll's or a frame's metadata; setting it rewrites the metadata in the files")
    about.add_argument("roll", type=Path)
    about.add_argument("--frame", type=frame_number, help="a frame's fields instead of the roll's (as on the film: 00, 0, 1, …)")
    about.add_argument(
        "fields", nargs="*", metavar="field=value",
        help=f"roll: {', '.join(k for k, *_ in metadata.ROLL_FIELDS)}; frame: {', '.join(k for k, _ in metadata.FRAME_FIELDS)}. "
        "An empty value clears a field; keywords are separated by commas",
    )  # fmt: skip

    sheet = commands.add_parser("contactsheet", help="an A4 contact sheet of the roll, PDF and PNG: every frame as its print will be, on film strips")
    sheet.add_argument("roll", type=Path)
    sheet.add_argument("--out", type=Path, help="where the PDF goes (default: <roll>/<Roll>_contact.pdf); the PNG goes beside it, named the same")
    sheet.add_argument("--rebate", action=argparse.BooleanOptionalAction, help="the film rebate with its edge print (default on; kept for the roll)")
    sheet.add_argument("--sprockets", action=argparse.BooleanOptionalAction, help="sprocket holes, 135 only (default off; kept for the roll)")

    base = commands.add_parser("develop", help="the base a frame's print is developed from")
    base.add_argument("roll", type=Path)
    base.add_argument("frame", type=frame_number, help="as on the film: 00, 0, 1, …")
    base.add_argument("base", choices=BASES)

    again = commands.add_parser("render", help="render a roll's frames again from the kept RAW files")
    again.add_argument("roll", type=Path)
    again.add_argument("--negative", action="store_true", help=NEGATIVE_HELP)
    again.add_argument("--frame", type=frame_number, help="only this frame (as on the film: 00, 0, 1, …)")
    again.add_argument("--picture", type=rectangle, metavar="X,Y,W,H")
    again.add_argument("--crop", action=argparse.BooleanOptionalAction, help="write only the picture area")
    again.add_argument("--per-channel", action=argparse.BooleanOptionalAction, help="stretch each channel on its own (default) or all alike")
    again.add_argument("--primaries", choices=("srgb", "prophoto"))
    again.add_argument("--flatfield", action=argparse.BooleanOptionalAction, help="apply the roll's flat field (default when it has one)")
    again.add_argument("--compare", action=argparse.BooleanOptionalAction, help="also write each file without the flat field")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    before = os.environ.get("TETHERINVERT_NO_LIGHT")
    if args.no_light:
        os.environ["TETHERINVERT_NO_LIGHT"] = "1"  # read by session.no_light(), in this process and the window's threads
    try:
        return run(args)
    except (CalibrationError, ScanError, KeyError, RuntimeError, ValueError) as exc:
        print(f"error: {exc.args[0] if isinstance(exc, KeyError) else exc}", file=sys.stderr)
        return 1
    finally:  # as it was: the tests run many commands in one process
        if before is None:
            os.environ.pop("TETHERINVERT_NO_LIGHT", None)
        else:
            os.environ["TETHERINVERT_NO_LIGHT"] = before


def about(args: argparse.Namespace) -> int:
    """`tetherinvert metadata`: print the fields, or set them and write them into the files."""
    if Roll.state(args.roll) != "existing":
        raise ValueError(f"{args.roll} is not a roll folder (it has no roll.json)")
    roll = Roll.open(args.roll)
    frame = roll.frame(args.frame) if args.frame is not None else None
    changes = {}
    for item in args.fields:
        key, equals, value = item.partition("=")
        if not equals:
            raise ValueError(f"{item!r}: write a field as name=value (an empty value clears it)")
        changes[key.strip()] = value
    if changes:
        if frame is None:
            roll.set_metadata(metadata.updated(roll.metadata, changes))
        else:
            roll.update_frame(frame.number, metadata=metadata.updated(frame.metadata, changes, frame=True) or None)
        roll = Roll.open(args.roll)
        frame = roll.frame(args.frame) if args.frame is not None else None
        files = retag(roll, None if frame is None else [frame.number])
        print(f"metadata written into {len(files)} file{'s' if len(files) != 1 else ''}")
    fields = roll.metadata if frame is None else frame.metadata or {}
    names = metadata.ROLL_FIELDS if frame is None else metadata.FRAME_FIELDS
    for key, label, *_ in names:
        value = fields.get(key)
        if value:
            print(f"{key} ({label}): {', '.join(value) if isinstance(value, list) else value}")
    if frame is not None:
        print(f"keywords written: {', '.join(metadata.keywords(roll.metadata, frame.metadata)) or 'none'}")
        print(f"date written: {metadata.frame_date(roll.metadata, frame.metadata) or 'none'}")
    elif not fields:
        print("no metadata entered")
    return 0


def run(args: argparse.Namespace) -> int:
    store = PresetStore()
    if args.command == "gui":
        from .window import main as gui_main

        return gui_main(sim=args.sim)  # --no-light reaches the window through TETHERINVERT_NO_LIGHT
    if args.command == "doctor":
        from .diagnose import doctor

        return doctor(args.sim)
    if args.command == "presets":
        for name in store.names():
            print(f"{name}: {store.get(name).kind}, {reminder(store.get(name))}")
        return 0
    if args.command == "lightroom":
        from . import lightroom

        if args.stop:
            lightroom.request()
            print("Lightroom Classic was told to stop watching.")
            return 0
        if not args.folders:
            raise ValueError("name the folders to watch (a roll's scans/, prints/ or both), or pass --stop")
        token, notes = lightroom.open_hot_folder(args.folders)
        print("\n".join(notes))
        deadline = time.time() + float(os.environ.get("TETHERINVERT_LIGHTROOM_WAIT", "120"))
        while not lightroom.answered(token):
            if time.time() > deadline:
                print(lightroom.no_answer(), file=sys.stderr)
                return 1
            time.sleep(1.0)
        print(f"Lightroom Classic is watching {lightroom.names(args.folders)}.")
        return 0
    if args.command == "delete":
        roll = Roll.open(args.roll)
        files = frame_files(roll, roll.frame(args.frame))
        if not args.yes and input(f"delete frame {paths.label(args.frame)} of roll {roll.name} ({len(files)} files)? [y/N] ").strip().lower() != "y":
            return 1
        print(f"deleted {len(delete_frame(roll, args.frame))} files of frame {paths.label(args.frame)}")
        return 0
    if args.command == "orient":
        turned = reorient_frame(Roll.open(args.roll), args.frame, {"turn": args.turn, "mirror": args.mirror})
        print(f"{len(turned)} files turned" if turned else "already that way up")
        return 0
    if args.command == "clean":
        roll = Roll.open(args.roll)
        files, size, kept = clean_raws(roll, dry_run=True)
        if kept:
            print(f"frames {', '.join(map(paths.label, kept))} have no TIFF yet and keep their RAW files")
        if files == 0:
            print("nothing to delete")
            return 0
        if not args.yes and input(f"delete {files} RAW files ({size / 1e9:.2f} GB) of roll {roll.name}? [y/N] ").strip().lower() != "y":
            return 1
        files, size, _ = clean_raws(roll)
        print(f"deleted {files} RAW files, {size / 1e9:.2f} GB freed")
        return 0
    if args.command == "metadata":
        return about(args)
    if args.command == "contactsheet":
        from .contact import write_sheet

        if Roll.state(args.roll) != "existing":
            raise ValueError(f"{args.roll} is not a roll folder (it has no roll.json)")
        roll = Roll.open(args.roll)
        options = {key: value for key, value in (("rebate", args.rebate), ("sprockets", args.sprockets)) if value is not None}
        if options:
            roll.set_sheet(options)
        written, pages, missing = write_sheet(roll, args.out)
        print(f"contact sheet: {written}")
        for page in pages:
            print(f"contact sheet: {page}")
        if missing:
            print(f"shown empty, no preview (render them again): frame {', '.join(missing)}", file=sys.stderr)
        return 0
    if args.command == "develop":
        Roll.open(args.roll).frame(args.frame)  # says so when there is no such frame
        Roll.open(args.roll).update_frame(args.frame, develop=args.base)
        print(f"frame {paths.label(args.frame)} is printed from the {args.base} base now")
        return 0
    if args.command == "prints":
        roll = Roll.open(args.roll)
        if args.match is not None:
            roll.set_settings(matched=args.match)
        try:
            written = write_prints(roll, lambda done, total: print(f"  {done}/{total}", end="\r"))
        except PrintsError as exc:
            print("\n".join(str(path) for path in exc.written))
            raise
        print("\n".join(str(path) for path in written) if written else "every print is up to date")
        return 0

    if args.command == "render":
        roll = Roll.open(args.roll)
        if not roll.frames:
            raise ValueError(f"{args.roll} holds no scanned frames")
        given = {key: getattr(args, key) for key in ("crop", "per_channel", "primaries", "flatfield", "compare") if getattr(args, key) is not None}
        if args.picture:
            given["rect"] = args.picture
        if given:
            roll.set_settings(**given)
        if args.frame is not None:  # frame 0 is a frame
            written = render(roll, roll.frame(args.frame), negative=args.negative)
        else:
            written = render_roll(roll, negative=args.negative, progress=lambda done, total: print(f"  {done}/{total}", end="\r"))
        print("\n".join(str(path) for path in written))
        return 0

    # Each command opens only the device it uses, so `light` works with the camera off and
    # `preview` without the Scanlight.
    rig = bench(args.sim)
    if args.command == "status":
        return status(rig)
    if args.command == "light":
        light = open_device(rig, "light", drive=True)  # the one command meant to drive the light: --no-light does not apply
        try:
            if not args.levels:
                manual_light(light)
                return 0
            levels = [0, 0, 0, 0] if args.levels == ["off"] else [int(v) for v in args.levels]
            light.set(*levels)
            if any(levels):
                input("light is on; press Enter to switch it off ")
        finally:
            light.off()
            light.close()
        return 0
    if args.command == "preview":
        camera = open_device(rig, "camera")
        try:
            data = camera.preview()
            if not data:
                raise RuntimeError("this camera gave no live-view frame")
            args.file.write_bytes(data)
        finally:
            camera.close()
        return 0

    # Every job needs the camera. A Scanlight kind needs the light too; an own light only the
    # camera, and the Scanlight's port is then left alone.
    camera = open_device(rig, "camera")
    try:
        if args.command == "calibrate":
            kind = args.kind or ("white" if args.white else "rgb-together" if args.together else "rgb")
        else:
            kind = store.get(args.preset).kind
        light = open_device(rig, "light") if KINDS[kind].scanlight else NoLight()
    except (KeyError, RuntimeError):
        camera.close()
        raise
    scanner = Scanner(light, camera, log=print)
    from .sim import film_removed, set_by_hand

    try:
        if args.command == "calibrate":
            by_hand = {}
            for key in scanner.unknown_settings():  # what the camera cannot tell us is asked for and stored
                given = getattr(args, key)
                if not given and sys.stdin.isatty():
                    given = input(f"the camera does not report its {key}; what is it set to? ").strip()
                if not given:
                    raise ValueError(f"the camera does not report its {key}: pass --{key}")
                by_hand[key] = given
            note = ""
            if not KINDS[kind].scanlight:  # the light is a by-hand setting, like the f-stop
                note = (args.light_note or "").strip()
                if not note and sys.stdin.isatty():
                    note = input("which light is it, and how is it set (e.g. 'Kaiser slimlite plano, full')? ").strip()
                if not note:
                    raise ValueError("an own light is a setting by hand: pass --light 'what it is, how it is set'")
            roi = Roi(*args.base)
            if KINDS[kind].prompted:
                shots = {}
                for letter in KINDS[kind].exposures:
                    problem = ""
                    while letter not in shots:
                        prompt = prompted(camera, kind, note)
                        prompt(letter, problem)
                        try:
                            shots[letter] = scanner.calibrate_colour(roi, letter, kind)
                        except WrongColour as exc:
                            problem = str(exc)
                preset = scanner.assemble(args.name, roi, kind, shots, by_hand=by_hand, light_note=note)
            else:
                set_by_hand(camera, kind, "O")  # only the simulator: the operator's light switched on
                preset = scanner.calibrate(args.name, roi, kind, by_hand=by_hand, light_note=note)
            store.save(preset)
            print(f"saved preset {preset.name!r}: {reminder(preset)}")
        elif args.command == "flatfield":
            preset = store.get(args.preset)
            print(f"preset {preset.name!r}: {reminder(preset)}")
            if not args.yes and sys.stdin.isatty():
                input("take the film out of the holder (leave the holder in place), then press Enter ")
            set_by_hand(camera, preset.kind, "O")  # only the simulator
            with film_removed(camera):  # only the simulator needs this
                flat = scanner.capture_flatfield(Roll.open(args.roll), preset, prompt=prompted(camera, preset.kind, preset.light_note))
            print(f"flat field saved in {args.roll}: the light falls off by up to {flat.falloff():.0%}")
        elif args.command == "scan":
            roll = Roll.open(args.roll)
            given = {key: value for key, value in (("rect", args.picture), ("compare", args.compare)) if value is not None}
            if given:
                roll.set_settings(**given)
            started = time.time()
            preset = store.get(args.preset)
            print(f"preset {preset.name!r}: {reminder(preset)}")
            turned = {"turn": args.turn, "mirror": args.mirror}
            set_by_hand(camera, preset.kind, "O")  # only the simulator
            frame = scanner.scan(
                roll, preset, args.retake, progress=lambda step: print(f"  exposing {step}"), orientation=turned, number=args.number,
                prompt=prompted(camera, preset.kind, preset.light_note),
            )  # fmt: skip
            for path in render(roll, frame, negative=args.negative):
                print(path)
            warning = clipping(roll, frame)
            if warning:
                print(f"WARNING: {warning}")
            print(f"frame {paths.label(frame.number)} done in {time.time() - started:.1f} s")
    finally:
        try:
            light.off()
            light.close()
        finally:
            camera.close()
    return 0


def prompted(camera, kind: str, note: str):
    """own-rgb's prompt in the terminal: the operator sets the light to one colour, then Enter."""
    from .sim import set_by_hand

    def prompt(letter: str, problem: str) -> None:
        if problem:
            print(problem)
        set_by_hand(camera, kind, letter)  # only the simulator: as if the operator had done it
        if sys.stdin.isatty():
            setting = f" ({note})" if note else ""
            input(f"set your light to {EXPOSURES[letter].name} only, at full{setting}, then press Enter to shoot ")

    return prompt


MANUAL_HELP = "r, g, b, w [level]: that colour alone (255 if no level) · rgb R G B: all three · off · q: off and quit"


def manual_light(light) -> None:
    """`tetherinvert light` with no levels: the Scanlight as a manual light, for scanning with an own
    light while the window runs with --no-light. It holds the port the whole time: whether
    the firmware keeps the LEDs lit once the port is closed is not known, and so it does not matter."""
    print(MANUAL_HELP)
    while True:
        try:
            words = input("light> ").split()
        except EOFError:
            words = ["q"]
        if not words:
            continue
        command, numbers = words[0].lower(), words[1:]
        try:
            values = [max(0, min(255, int(number))) for number in numbers]
        except ValueError:
            print(MANUAL_HELP)
            continue
        if command == "q":
            light.off()
            return
        if command == "off":
            light.off()
        elif command in ("r", "g", "b", "w") and len(values) <= 1:
            levels = [0, 0, 0, 0]
            levels["rgbw".index(command)] = values[0] if values else 255
            light.set(*levels)
        elif command == "rgb" and len(values) == 3:
            light.set(*values, 0)
        else:
            print(MANUAL_HELP)


def status(rig) -> int:
    """Both devices, each on its own: a missing one is reported, the other still shown."""
    for which in ("camera", "light"):
        try:
            device = open_device(rig, which)
        except RuntimeError as exc:
            print(f"{which + ':':8}{exc}")
            continue
        try:
            if which == "light":
                print(f"light:  {device.describe()}")
                continue
            print(f"camera: {device.model}")
            for key in ("iso", "shutter", "aperture"):
                print(f"{key + ':':9}{device.get(key) or 'not settable from here'}")
            print("stills:  " + stills_go_to(device))
        finally:
            device.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
