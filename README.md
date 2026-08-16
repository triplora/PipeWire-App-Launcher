# PipeWire App Launcher

[![Tests](https://github.com/triplora/PipeWire-App-Launcher/actions/workflows/tests.yml/badge.svg)](https://github.com/triplora/PipeWire-App-Launcher/actions/workflows/tests.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

> **Project status:** alpha. Version 0.1.4 was functionally validated on Ubuntu
> 24.04 with PipeWire discovery and JACK applications launched through
> `pw-jack`.

## About the Project

PipeWire-App-Launcher is a desktop profile manager that launches JACK
applications through PipeWire's `pw-jack` compatibility layer. It is designed
and validated for **Ubuntu 24.04** and distributed as a portable AppImage, so it
runs without a system-wide installation.

Beyond the classic profile manager, the launcher behaves like a small audio
routing assistant:

- It checks that the PipeWire server (and its `pipewire-pulse` /
  `wireplumber` companions) is alive **before** opening the window, and offers
  to start it through graphical dialogs when it is not.
- It lists every installed audio application in a dedicated "Audio Apps" panel,
  so multiple sound processes can be started and stopped with one click.
- It monitors PipeWire in the **background** and automatically reconnects
  virtual cables whenever a new stream appears — a new Firefox tab, a player, a
  DAW — linking it straight to the active hardware sink.

## New Features

### Server Diagnostics and Health

On every startup the launcher verifies the PipeWire server before opening the
main window:

- Checks the per-user systemd unit (`systemctl --user is-active pipewire`),
  falling back to process and socket probes when no user bus is available.
- If the server is down, a graphical dialog offers to start it:

  > O servidor de áudio PipeWire não está rodando. Deseja iniciá-lo agora?

- Answering "Sim" starts `pipewire`, `pipewire-pulse`, and `wireplumber`
  through `systemctl --user start` and waits until the server is ready.
- Answering "Não" (or a failed start) closes the launcher with a warning
  instead of opening a broken session.
- Once the server is up, all pre-existing application streams are reconnected
  to the hardware outputs.

### Audio Apps Panel

The **Audio Apps** tab turns the launcher into a lightweight sound-process
manager:

- Scans XDG application directories (`/usr/share/applications`, ...) for
  `.desktop` entries whose `Categories` include audio-related values (`Audio`,
  `AudioVideo`, `Music`, `MIDI`, ...) or whose executable is a known audio
  application (Ardour, Audacity, Carla, Bitwig Studio, REAPER, LMMS, Hydrogen,
  Rosegarden, ...).
- Detection is parsing-only: nothing is ever executed while scanning.
- Each row shows an enable/disable toggle, the executable, the exact
  `pw-jack` command, and a live status column.
- Checking/unchecking an application persists to
  `~/.config/pipewire-app-launcher/audio_applications.json`.
- "Start selected" / "Stop selected" launch or stop the application through
  `pw-jack` with the same supervised graceful-stop / forced-kill fallback used
  by profiles.
- The list is populated automatically at startup and can be refreshed with
  "Scan for audio apps".

### Dynamic Background Monitoring

The launcher now routes new audio streams as they appear, with no user action:

- A background daemon thread keeps a long-lived `pactl subscribe` listener open
  (falling back to `pw-mon` when `pactl` is not available).
- Whenever a new stream is created — `Event 'new' on sink-input` (or a new node
  reported by `pw-mon`) — the watcher waits out the event burst and re-runs the
  auto-link routine.
- Newly created application ports (a fresh Firefox tab, a restarted player, a
  duplicated stream) are connected to the active hardware sink's `FL`/`FR`
  channels automatically.
- The restorer is idempotent: already-linked streams are never touched, and
  capture, MIDI, and monitor ports are ignored.
- This fixes the orphaned-stream problem where tabs that were open before a
  server restart would stay silent until manually reloaded or duplicated.

## Visual Examples

Screenshots of the main areas of the launcher (drag your captures into
`docs/images/`):

![Interface do Painel](docs/images/audio_panel.png)

![Grafo de Conexões](docs/images/pipewire_graph.png)

![Editor de Perfil](docs/images/profile_editor.png)

![Descoberta de Nós PipeWire](docs/images/pipewire_discovery.png)

![Diagnóstico do Servidor](docs/images/health_check.png)

## How to Run

### With Conda (recommended)

The project is developed inside the `pipewire-launcher-dev` environment. Create
it once, then reuse it:

```bash
conda create -n pipewire-launcher-dev python=3.11
conda activate pipewire-launcher-dev
sudo apt install pipewire-jack
pip install -r requirements.txt
python -m pipewire_launcher
```

If the environment already exists, simply activate it and run:

```bash
conda activate pipewire-launcher-dev
python -m pipewire_launcher
```

### From source (venv)

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

## PipeWire health check

Before opening the main window the launcher verifies that the PipeWire server is
running, checking the per-user systemd unit (`systemctl --user is-active
pipewire`) and falling back to process/socket probes when no user bus is
available. If the server is down it asks:

> O servidor de áudio PipeWire não está rodando. Deseja iniciá-lo agora?

Answering "Sim" starts `pipewire`, `pipewire-pulse`, and `wireplumber` through
`systemctl --user start` in the background and waits briefly for the server to
come up. Answering "Não" (or a failed startup) closes the launcher with a
warning instead of opening a broken session.

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

## Contributing, support and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards. Read
[SUPPORT.md](SUPPORT.md) before opening an Issue.

Report vulnerabilities according to [SECURITY.md](SECURITY.md), never through a
public Issue.

## License

Copyright (C) 2026 Triplora. Released under the
[GNU General Public License v3.0 or later](LICENSE).
