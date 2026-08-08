# PipeWire App Launcher

[![Tests](https://github.com/triplora/PipeWire-App-Launcher/actions/workflows/tests.yml/badge.svg)](https://github.com/triplora/PipeWire-App-Launcher/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Desktop profile manager for launching JACK applications through PipeWire's
`pw-jack` compatibility layer. Designed for Ubuntu 24.04 and distributed as a
portable AppImage.

> **Project status:** alpha. Version 0.1.1 is functionally validated on Ubuntu
> 24.04 with Ardour connected to PipeWire through the JACK compatibility layer.

## Features

- Create, edit, duplicate, delete, enable, and search application profiles.
- Select an executable with the native file picker.
- Build the exact `pw-jack -- application arguments...` invocation.
- Configure working directory and environment variables per profile.
- Start and stop applications without invoking a shell.
- Persistent JSON configuration in
  `~/.config/pipewire-app-launcher/profiles.json`.
- Import/export profile collections and automatic atomic saves.
- Runtime checks for PipeWire, `pw-jack`, and the selected executable.
- Process log and status indicator.

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

The build needs internet access once to install the pinned Python dependencies
and download `appimagetool`. On Ubuntu 24.04, install the native build
dependency first:

```bash
sudo apt update
sudo apt install libxcb-cursor0 pipewire-jack python3-venv curl
./scripts/build-appimage.sh
```

The build script bundles `libxcb-cursor.so.0` in the AppImage and verifies that
the Qt xcb platform plugin has no unresolved native libraries. Machines that
run the resulting AppImage do not need `libxcb-cursor0` installed separately.

The result is written to `dist/PipeWire-App-Launcher-x86_64.AppImage`. Run it
with:

```bash
chmod +x dist/PipeWire-App-Launcher-x86_64.AppImage
./dist/PipeWire-App-Launcher-x86_64.AppImage
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

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow. Please
report vulnerabilities according to [SECURITY.md](SECURITY.md), not through a
public issue.

## License

Copyright (C) 2026 Triplora. Released under the
[GNU General Public License v3.0 or later](LICENSE).
