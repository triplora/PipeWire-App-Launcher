# PipeWire App Launcher

[![Tests](https://github.com/triplora/PipeWire-App-Launcher/actions/workflows/tests.yml/badge.svg)](https://github.com/triplora/PipeWire-App-Launcher/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Desktop profile manager for launching JACK applications through PipeWire's
`pw-jack` compatibility layer. Designed for Ubuntu 24.04 and distributed as a
portable AppImage.

> **Project status:** alpha. Version 0.1.4 was functionally validated on Ubuntu
> 24.04 with PipeWire discovery and JACK applications launched through
> `pw-jack`.

## Features

- Create, edit, duplicate, delete, enable, and search application profiles.
- Select an executable with the native file picker.
- Build the exact `pw-jack -- application arguments...` invocation.
- Detect a conservative catalog of installed JACK-capable applications from
  XDG desktop entries without executing them.
- Add only the detected applications explicitly selected by the user.
- List installed audio applications from XDG desktop entries matching
  audio-related categories (`Audio`, `AudioVideo`, `Music`, MIDI) or known
  audio executables.
- Enable/disable each detected audio application with an inline toggle and
  start/stop it through `pw-jack` from a dedicated "Audio Apps" tab.
- Configure working directory and environment variables per profile.
- Start and stop applications without invoking a shell.
- Persistent JSON configuration in
  `~/.config/pipewire-app-launcher/profiles.json`.
- Import/export profile collections and automatic atomic saves.
- Runtime checks for PipeWire, `pw-jack`, and the selected executable.
- Process log and status indicator.
- Independent per-profile process supervision and bounded logs.
- Explicit lifecycle status, PID, and timestamps.
- Asynchronous PipeWire node discovery.
- Read-only discovery tree with refresh/cancel behavior.

## Run from source

```bash
sudo apt update
sudo apt install pipewire-jack python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m pipewire_launcher
```

Quick host check:

```bash
pw-cli info 0
pw-jack -- true
```

## Tests

Core tests use only the Python standard library:

```bash
python -m unittest discover -s tests -v
```

## Build the AppImage

The build requires Python 3.10 or newer, `python3-venv`, the pinned Python
dependencies, `libxcb-cursor0`, and a locally supplied `appimagetool`.
Only `x86_64` is supported. The build never downloads or executes
`appimagetool` automatically.

```bash
sudo apt update
sudo apt install libxcb-cursor0 pipewire-jack python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

For the official immutable `appimagetool` 1.9.1 x86_64 asset, download it
from the [official release](https://github.com/AppImage/appimagetool/releases/tag/1.9.1)
and verify its GitHub API-published SHA-256 digest before making it executable:

```bash
curl -fL -o /tmp/appimagetool-x86_64.AppImage \
  https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
echo 'ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0  /tmp/appimagetool-x86_64.AppImage' | sha256sum --check
chmod +x /tmp/appimagetool-x86_64.AppImage
```

The AppImage format also requires the official x86_64 type-2 runtime. Download
the fixed official asset and verify its published digest before use:

```bash
curl -fL -o /tmp/runtime-x86_64 \
  https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64
echo '1cc49bcf1e2ccd593c379adb17c9f85a36d619088296504de95b1d06215aebbf  /tmp/runtime-x86_64' | sha256sum --check
```

Build v0.1.4 with isolated directories. Both directories must be outside the
checkout; the output directory is preserved and existing artifacts are never
overwritten:

```bash
work_dir="$(mktemp -d /tmp/pipewire-app-launcher-work.XXXXXX)"
output_dir="$(mktemp -d /tmp/pipewire-app-launcher-output.XXXXXX)"
./scripts/build-appimage.sh \
  --work-dir "$work_dir" \
  --output-dir "$output_dir" \
  --appimagetool /tmp/appimagetool-x86_64.AppImage \
  --appimagetool-sha256 ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0 \
  --runtime-file /tmp/runtime-x86_64 \
  --runtime-sha256 1cc49bcf1e2ccd593c379adb17c9f85a36d619088296504de95b1d06215aebbf
sha256sum "$output_dir/PipeWire-App-Launcher-x86_64.AppImage"
```

`--work-dir` contains only temporary venv, PyInstaller products and `AppDir`;
they are removed after the run. If omitted, a safe temporary work directory
is created. `--output-dir` is also created when omitted as a safe temporary
directory, and its final AppImage is preserved. The artifact name is
`PipeWire-App-Launcher-x86_64.AppImage`.

The build script bundles `libxcb-cursor.so.0` in the AppImage and verifies that
the Qt xcb platform plugin has no unresolved native libraries. Machines that
run the resulting AppImage do not need `libxcb-cursor0` installed separately.

Run the result with:

```bash
chmod +x "$output_dir/PipeWire-App-Launcher-x86_64.AppImage"
"$output_dir/PipeWire-App-Launcher-x86_64.AppImage"
```

`pw-jack` and a running PipeWire session remain host dependencies; audio
applications need the user's live session bus and audio sockets.

## First profile: Ardour

Create a profile with:

```text
Name: Ardour
Executable: /usr/bin/ardour
Arguments: (empty)
```

The command preview should be:

```bash
pw-jack -- /usr/bin/ardour
```

After launching, verify that Ardour registered its ports:

```bash
pw-link -io | grep '^ardour:'
```

## Profile command model

The preview is a display-safe rendering. At execution time the program calls
`QProcess.start("pw-jack", ["--", executable, ...])` directly. It never passes
the profile through `/bin/sh`, so shell operators in arguments or environment
values are not evaluated.

## Audio Applications tab

The "Audio Apps" tab scans XDG application directories (for example
`/usr/share/applications`) for desktop entries whose `Categories` include
audio-related values or whose executable is a known audio application. The list
is populated automatically at startup and can be refreshed with
"Scan for audio apps".

Each row has an enabled toggle and status; checking/unchecking persists to
`~/.config/pipewire-app-launcher/audio_applications.json`. "Start selected"
launches the application through the `pw-jack` wrapper and "Stop selected"
stops it with the same supervised graceful-stop/forced-kill fallback used by
profiles.

The panel never executes anything while scanning; detection is parsing-only.

## Contributing, support and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards. Read
[SUPPORT.md](SUPPORT.md) before opening an Issue.

Report vulnerabilities according to [SECURITY.md](SECURITY.md), never through a
public Issue.

## License

Copyright (C) 2026 Triplora. Released under the
[GNU General Public License v3.0 or later](LICENSE).
