# Command line

Everything the window does is also a command. `tetherinvert --help` and `tetherinvert <command> --help`
list every option.

```
tetherinvert gui                        # the window
tetherinvert status                     # what is connected, and the camera's settings
tetherinvert doctor                     # a test shot and what went wrong
tetherinvert presets

tetherinvert calibrate "Portra 400" --base 0.03,0.30,0.03,0.40
tetherinvert calibrate "HP5" --base 0.03,0.30,0.03,0.40 --white --aperture f/8
tetherinvert calibrate "Gold together" --base 0.03,0.30,0.03,0.40 --together
tetherinvert --no-light calibrate "Gold own" --base 0.03,0.30,0.03,0.40 --kind own-rgb --light "RGB LED panel, each colour at 100 %"
tetherinvert light                      # the Scanlight as a manual light: r, g, b, w [level], rgb R G B, off, q

tetherinvert flatfield ~/Scans/Roll01 --preset "Portra 400"
tetherinvert scan ~/Scans/Roll01 --preset "Portra 400" --picture 0.07,0.10,0.86,0.80
tetherinvert scan ~/Scans/Roll01 --preset "Portra 400" --negative --retake
tetherinvert scan ~/Scans/Roll01 --preset "Portra 400" --compare
tetherinvert scan ~/Scans/Roll01 --preset "Portra 400" --turn 90 --mirror
tetherinvert scan ~/Scans/Roll01 --preset "Portra 400" --number 00
tetherinvert render ~/Scans/Roll01 --negative --crop --primaries prophoto --no-flatfield

tetherinvert develop ~/Scans/Roll01 3 converted
tetherinvert prints ~/Scans/Roll01 --match
tetherinvert metadata ~/Scans/Roll01 camera="Nikon FM2" film="HP5 Plus" ei=800 shot_from=2026-10-03
tetherinvert metadata ~/Scans/Roll01 --frame 7 note="Anna on the jetty" keywords_left_out=family
tetherinvert contactsheet ~/Scans/Roll01 --sprockets

tetherinvert orient ~/Scans/Roll01 7 --turn 180
tetherinvert delete ~/Scans/Roll01 7
tetherinvert delete ~/Scans/Roll01 00
tetherinvert clean ~/Scans/Roll01       # delete the RAW files
tetherinvert lightroom ~/Scans/Roll01/scans ~/Scans/Roll01/prints
```

- Rectangles are `x,y,w,h` as fractions of the frame.
- Frames are given by their number on the film: `00`, `0`, `1`, …
- `tetherinvert flatfield` makes the folder a roll that uses its flat field: the window continues it
  like any other roll.
- The window and the command line can work on the same roll; each change waits for the other's.
- Each command opens only what it needs: `light` only the Scanlight, `preview` only the camera.
  `status` and `doctor` say which device is missing and still show the other.
- `--sim` runs any command on the simulated rig. `--no-light` never opens the Scanlight's port.

## Environment variables

| Variable | Effect |
|---|---|
| `TETHERINVERT_HOME` | Where presets and notes live instead of `~/.tetherinvert` |
| `TETHERINVERT_SIM=1` | Same as `--sim` |
| `TETHERINVERT_NO_LIGHT=1` | Same as `--no-light` |
| `TETHERINVERT_SIM_PICTURES` | With `--sim`, a folder of photos (JPEG, TIFF, PNG) the simulated roll shows: frame *n* is the *n*th by name, starting over past the last |
| `TETHERINVERT_PORT` | The Scanlight's serial port, when the automatic choice is wrong |
| `TETHERINVERT_LIGHTROOM_WAIT` | Seconds `tetherinvert lightroom` waits for the plug-in (default 120) |
