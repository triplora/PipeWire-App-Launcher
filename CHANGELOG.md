# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Planned

- Detect installed JACK-capable applications.
- Display PipeWire and JACK compatibility health.
- Add starter profiles for common Linux audio applications.

## [0.1.2] - Unreleased

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

[Unreleased]: https://github.com/triplora/PipeWire-App-Launcher/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/triplora/PipeWire-App-Launcher/releases/tag/v0.1.1
[0.1.0]: https://github.com/triplora/PipeWire-App-Launcher/releases/tag/v0.1.0
