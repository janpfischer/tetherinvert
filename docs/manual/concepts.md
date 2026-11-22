# How TetherInvert thinks

Five ideas carry the whole program. Read them once; the other pages assume them.

## 1. A film preset per film stock

Every film stock has its own orange mask and density. **Calibration** meters the **film base** (the
clear film between frames, the brightest thing a negative lets through) and solves the exposure
that puts it at 90 % of the sensor's range in red, green and blue alike: exposed to the right, as
close to the ceiling as is safe. Because all three channels land at the same place, the film base
becomes neutral: the white balance is set by the film itself, not by eye.

The result is a **film preset**: the light's levels, one shutter speed, ISO and f-stop, and the film
base per channel. You calibrate once per stock (Portra 400, Gold 200, HP5…) and use the preset for
every roll of it.

What the camera cannot set or report (the f-stop of a lens without electronic contacts, your own
light's setting) is a **by-hand setting**: you state it at calibration, and every session reminds
you of it.

## 2. A roll is a folder

A **roll** is one film, kept as one folder: its frames, a copy of the preset it was scanned with,
its settings, its metadata, its corrections. Closing the window loses nothing; the roll can be
continued any time. A new roll needs a name no folder has yet.

The roll keeps its **own copy** of the preset, as it was when the roll was started. Calibrating the
preset again, or deleting it, never changes a roll half-way.

Scanning a roll in the window is a **session**. A roll can have several, for example when you scan
half a roll today and the rest tomorrow.

## 3. A flat field per session

Light and lens are never perfectly even: the corners are dimmer than the centre, differently for
each colour. At the start of a session TetherInvert photographs the bare light without film: the
**flat field**. Every scan is divided by it, colour by colour, so the film base measured at
calibration is the film base everywhere in the frame. Holder, light and focus change between
sessions, so the flat field belongs to the session, not to the rig.

## 4. Scan flat: the master

Every frame is scanned as a **flat positive**, `scans/Roll01_001.tif`: inverted, white balanced on
the film base, with headroom at both ends and no curve. It looks milky on purpose. In log density it
is an exact copy of the picture, so anything can be made from it later, without the RAW files. This
is the file for your own editing, and the master everything else is made from.

## 5. Develop, then print

Each frame is **developed** from its flat scan: a **base** (Flat, Converted, Converted + white) and
**corrections** (density, colour, contrast, saturation, tone), seen on screen as you make them.
When the roll is done, it is **printed**: every frame at once, into `prints/`. The flat scans stay
as they are. Prints are always made from the flat scans, never from the RAW files.

A print is optional. Stop at the flat scans and edit elsewhere, stop at the prints, or take the
prints on as a better start: your choice per roll.

## The roll folder

```
~/Scans/Roll01/
    roll.json              settings, metadata, frames, measured points, bases and corrections
    .roll.lock             hidden: held while roll.json is changed, so the window and the command line wait for each other
    flatfield.npz          this session's picture of the bare light
    raw/Roll01_001_R.ARW   the exposures, kept so every frame can be rendered again
    scans/Roll01_001.tif   the scan: flat positive, the master
    scans/Roll01_001_noflat.tif  the same frame without the flat field (if chosen, to compare)
    negative/Roll01_001.tif  linear negative (if chosen)
    prints/Roll01_001.tif  the print: developed from the scan with its base and corrections
    preview/Roll01_001.npz the window's picture of the frame
    Roll01_contact.pdf     the contact sheet
    Roll01_contact.png     the same as a picture (a longer roll: Roll01_contact_2.png, …)
```

Every file is named after the roll's name, which `roll.json` keeps from when the roll was made (the
folder's name then). A roll folder can be moved or renamed: `~/Scans/Roll01` moved to
`~/Film/2026/01` is still roll Roll01, and its files keep their names (`Roll01_001.tif`), so
Lightroom's *Find Missing Folder* finds them again.

Presets live in `~/.tetherinvert/presets.json`.
