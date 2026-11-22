# Install

## The macOS app

Download `TetherInvert-<version>-macos-arm64.dmg` from the
[releases](https://github.com/janpfischer/tetherinvert/releases) and drag TetherInvert into
Applications. It carries its own Python and libraries; nothing else is needed. Apple silicon only.

The app is not yet signed with an Apple Developer ID, so macOS refuses to open a downloaded copy
the first time. Once, either open System Settings → Privacy & Security and choose *Open Anyway*,
or run:

```
xattr -dr com.apple.quarantine /Applications/TetherInvert.app
```

Opened from the Finder, the app shows the window. Its binary also takes every command of the
[command line](command-line.md):

```
/Applications/TetherInvert.app/Contents/MacOS/tetherinvert status
```

## Linux

Download `TetherInvert-<version>-linux-x86_64.tar.gz` (Intel and AMD) or
`TetherInvert-<version>-linux-aarch64.tar.gz` (64-bit ARM) from the
[releases](https://github.com/janpfischer/tetherinvert/releases), unpack it wherever it should
live, and add it to your applications:

```
tar xzf TetherInvert-<version>-linux-x86_64.tar.gz
TetherInvert/install.sh
```

`install.sh` puts TetherInvert in the desktop's applications and `tetherinvert` (the
[command line](command-line.md)) in `~/.local/bin`, for you only. Run it again after moving the
folder; `TetherInvert/install.sh --remove` takes both away. The folder carries its own Python and
libraries. It needs a system at least as new as:

- x86_64: Ubuntu 22.04, Debian 12, Fedora 36
- aarch64: Ubuntu 24.04, Debian 13, Fedora 40 (on a Raspberry Pi: the 64-bit Raspberry Pi OS based
  on Debian 13)

Three things are the system's to allow, not TetherInvert's:

- **Opening the camera.** The rules that let you use a USB camera without root come with your
  distribution's libgphoto2 package, installed on most desktops. If `tetherinvert doctor` sees no
  camera that `lsusb` lists, install it (`libgphoto2-6t64` on Ubuntu 24.04, `libgphoto2` on
  Fedora) and plug the camera in again.
- **The file manager lets go of the camera.** GNOME and others mount a camera as soon as it is
  plugged in, and then hold it: eject it in the file manager's sidebar. To stop that for good:

  ```
  systemctl --user mask --now gvfs-gphoto2-volume-monitor.service
  ```

- **The Scanlight's serial port.** Join the group that owns it (`dialout` on Ubuntu, Debian and
  Fedora, `uucp` on Arch), then log out and back in:

  ```
  sudo usermod -aG dialout $USER
  ```

The Lightroom plug-in is for macOS only.

## From source (macOS and Linux)

With Python 3.11 or newer:

```
git clone https://github.com/janpfischer/tetherinvert.git
cd tetherinvert
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
tetherinvert gui
```

The camera library comes with the `gphoto2` package; nothing to install separately. Building the
macOS or Linux app yourself is described in the project's [README](../../README.md#building-the-macos-app).
