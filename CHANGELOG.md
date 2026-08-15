# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- List installed audio applications from XDG desktop entries by audio-related
  `Categories` (Audio, AudioVideo, Music, MIDI) or known audio executables.
- Add an "Audio Apps" tab with per-application enable toggles persisted to
  `audio_applications.json` and supervised Start/Stop through `pw-jack`.

### Planned

- Display PipeWire and JACK compatibility health.
- Add starter profiles for common Linux audio applications.

## [0.1.4] - 2026-08-14

### Added

- Add a project Code of Conduct and support policy.
- Add Dependabot coverage for Python dependencies and GitHub Actions.
- Add controlled Issue routing with blank Issues disabled.
- Add regression tests for public project hygiene.
- Detect installed JACK-capable applications from XDG desktop entries without
  executing their commands.
- Let users explicitly select detected applications before atomically saving
  new profiles.
- Preserve safe desktop-entry environment assignments and remove supported
  freedesktop field codes.

## [0.1.3] - 2026-08-13

### Added

- Discover PipeWire nodes asynchronously through a supervised `pw-dump`
  process.
- Display discovered node name, type, application, PID, media class, and ID in
  a read-only tree.
- Support refresh, cancellation, bounded output, timeout handling, stale
  callback rejection, and permanent discovery shutdown.
- Preserve the last valid discovery snapshot across refresh failures and
  cancellation.

### Fixed

- Classify launcher-requested process termination as stopped instead of failed.
- Preserve failed status for spontaneous crashes, external termination, and
  process start failures.
- Clear crash diagnostics when termination was explicitly requested by the
  launcher.

### Safety

- Keep discovery and application execution asynchronous and shell-free.
- Retain process ownership until `QProcess` reaches `NotRunning`.
- Keep stop intent scoped to the current process generation.
- Preserve the JSON version 1 profile format from earlier releases.

### Verified

- 148 automated tests passed with zero skips and xfails.
- PipeWire node discovery was validated against a live Ubuntu 24.04 session.
- Ardour and Audacity were launched through `pw-jack`.
- User-requested Stop was validated without leaving launcher processes behind.

## [0.1.2] - 2026-08-11

### Added

- Independently supervise one runtime process per profile.
- Track explicit process states, PIDs, timestamps, and exit codes.
- Keep bounded stdout and stderr logs per profile, with per-profile clearing.
- Prevent duplicate starts and target Stop only at the selected profile.
- Detect spontaneous termination and provide graceful-stop/forced-kill fallback.
- Confirm and supervise active processes when closing the launcher.

### Compatibility

- Continue loading and saving profile collections in JSON version 1 from v0.1.1.
- Descendant process groups are not signaled directly; termination uses the
  supervised QProcess until safe process-session isolation is introduced.
- Launcher shutdown has a bounded final deadline if a process emits no
  completion signal after forced termination.

## [0.1.1] - 2026-08-08

### Fixed

- Bundle `libxcb-cursor.so.0` in the AppImage.
- Fail the build when the Qt xcb plugin has unresolved native libraries.
- Configure the AppImage runtime library path.

### Verified

- AppImage startup on Ubuntu 24.04.
- Ardour launch through `pw-jack` and JACK connection through PipeWire.
- Publication of Ardour audio and MIDI ports in PipeWire.

## [0.1.0] - 2026-08-08

### Added

- Qt 6 desktop interface and persistent application profiles.
- Safe argument-based process launch without a shell.
- Profile import/export, process output, and start/stop controls.
- Reproducible AppImage build script and core unit tests.

[Unreleased]: https://github.com/triplora/PipeWire-App-Launcher/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/triplora/PipeWire-App-Launcher/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/triplora/PipeWire-App-Launcher/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/triplora/PipeWire-App-Launcher/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/triplora/PipeWire-App-Launcher/releases/tag/v0.1.1
[0.1.0]: https://github.com/triplora/PipeWire-App-Launcher/releases/tag/v0.1.0
