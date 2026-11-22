# When something fails

Start with **Help › Run Doctor…** in the window. It shows what is installed and connected, takes one
test shot without changing any camera setting, and says whether that file decodes. The shot is kept
in `~/.tetherinvert/doctor/` (**Show test shot** opens it).

Without the window, the same report comes from the [command line](command-line.md):

```
tetherinvert doctor
```

Close the window first: only one program can hold the camera.

| Message | What to do |
|---|---|
| *holds a JPEG / HEIF picture, not a RAW* | Set image quality to RAW only, and check that the camera's PC Remote settings do not send the JPEG to the computer. |
| *looks like a RAW file, but the decoder cannot read it* | The decoder does not know this RAW type. Switch it in the camera menu, or update the decoder (`pip install -U rawpy`). |
| *too small to be a RAW* | The download was cut short: cable or port. |
| *Another program holds the camera* | Unplug and replug; quit Photos, Image Capture and sync clients. On Linux, unmount the camera in the file manager (see [Install](install.md#linux)). |
| *The camera also keeps every exposure on its memory card* | The camera did not take the request to save to the computer only. Set it in the camera's menu (see [Your rig](rig.md)). |
| *The Scanlight is not connected* | Plug in its data port (the left USB-C; the other is power only). The window finds it within a few seconds without touching the camera. |
| *The camera does not answer any more* | The camera was lost; the window looks for it every 2 s. The Scanlight stays connected. |
| *Lightroom Classic has not picked up the request* | The plug-in is not loaded, or an older one still runs after an update: restart Lightroom Classic, then check File → Plug-in Manager. |
| The TetherInvert fields are empty in Lightroom | The photos were imported or synchronized by hand: click the roll's folder and choose *Library › Plug-in Extras › TetherInvert: read metadata*. See [Photos you imported yourself](lightroom.md#photos-you-imported-yourself). |
| Lightroom shows an old camera, lens, date or exposure | Lightroom reads these only when a file is added: *Metadata › Read Metadata from Files*. See [After you change metadata in TetherInvert](lightroom.md#after-you-change-metadata-in-tetherinvert). |
| Frames clipped (in red in the log, after a scan) | See [the clipping warning](scanning.md#if-a-frame-is-clipped). |
| The flat field is refused | Film still in the holder; or, with your own light, a light that flickers. |

A shot that fails during calibration is kept in `~/.tetherinvert/failed/`.

## Reporting a problem

Open an [issue](https://github.com/janpfischer/tetherinvert/issues) with what you did, what happened,
the window's log (**Log**, then **Copy**), the doctor's report (**Help › Run Doctor…**, then **Copy
report**, which includes the details from *About TetherInvert*), and your rig (camera, lens, light,
operating system). For a RAW file that does not decode, attach the test shot (**Show test shot**) or
the file from `~/.tetherinvert/failed/`.
