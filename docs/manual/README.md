# TetherInvert manual

TetherInvert scans film negatives with a digital camera on a copy stand. It drives the camera and if supported the
light. It can shoot each frame under red, green and blue light one after the other, and writes 16-bit
TIFFs: a calibrated, repeatable starting point for every frame of a roll, ready in Lightroom
Classic or any editor you like.

If you do not have a controllable light you can tether scan with a normal light, too.

This manual is for photographers who already scan film with a camera. It does not explain copy
stands, holders or focusing; it explains what TetherInvert does and how to work with it.

## What it does

- **Calibrates once per film stock.** The film base is exposed to the right in red, green and
  blue alike, so the white balance is set by the film itself.
- **Scans fast.** Space bar, next frame. A 36-frame roll takes 11 minutes to scan and under 25 minutes from the first scan to the contact sheet, with
  the frames developed. Calibration, once per film stock, is not counted.
- **Corrects uneven light.** A flat field per session, taken without film.
- **Writes a flat master.** Every frame is saved as a flat positive with nothing clipped and no
  curve: the start for your own editing.
- **Develops and prints, if you want.** A base per frame and corrections modelled on the
  controls of the Fujifilm Frontier SP-3000 (density, colour, contrast, saturation, tone), seen on
  screen and written as prints in one batch. Enough for a finished print, or a better start for
  the next editor.
- **Keeps the grain.** No noise reduction, no sharpening.
- **Records the film's metadata.** Camera, lens, film stock, EI, developer, dates, notes and
  keywords go into every file. The camera on the copy stand is kept apart, so Lightroom shows the
  camera you shot the film with.
- **Makes a contact sheet** of the roll as PDF and PNG, for the archive.
- **Works with Lightroom Classic** (macOS): a hot folder adds the files as they appear, and a
  plug-in shows the film and scan fields in the Metadata panel.

## Where it fits

[NegPy](https://github.com/marcinz606/NegPy) and [Negative Lab Pro](https://www.negativelabpro.com/)
are excellent conversion tools, and TetherInvert owes a lot to NegPy. TetherInvert does less, on purpose:
scan a roll quickly under RGB light, give every frame a calibrated starting point, keep the roll's
metadata, make the contact sheet, then hand over to the editor you already know.

## Before you start

- **macOS or Linux.** The Lightroom integration is macOS only. There is no Windows version (the
  camera library, libgphoto2, has none).
- **A camera that libgphoto2 can control**, shooting RAW.
- **A light.** TetherInvert drives the [Scanlight](https://github.com/jackw01/scanlight) directly. Any
  other light works too, set by hand. See [Lights](lights.md).
- **In active development.** Things can change and break between versions.

## A roll, start to finish

The working pages are sequences of steps with screenshots; the numbers in the screenshots are the
ones in the text.

1. [Install](install.md) TetherInvert.
2. Get to know [the window](window.md): a rail of steps, Roll, Scan, Develop and Finish, with
   Presets, Lightroom and the devices at its foot.
3. Choose how you light the film: [Lights](lights.md).
4. Read [How TetherInvert thinks](concepts.md) once: five ideas the rest builds on.
5. Set up [your rig](rig.md).
6. [Calibrate](calibrating.md) once per film stock.
7. [Start a session](session.md): name the roll, fill in its metadata, start, take the flat field.
8. [Scan](scanning.md) the roll.
9. [Develop](developing.md) the frames.
10. [Finish the roll](finishing.md): print it, make the [contact sheet](contact-sheet.md), and correct
    the [metadata](metadata.md) if needed.
11. Work on in [Lightroom Classic](lightroom.md), or any editor.

Also: the [command line](command-line.md), [when something fails](troubleshooting.md).

The screenshots show the simulated rig that comes with TetherInvert (`tetherinvert --sim gui`): its
negatives are made from ordinary photos, not scanned from film, so they say nothing about the
colours a real rig gives.
