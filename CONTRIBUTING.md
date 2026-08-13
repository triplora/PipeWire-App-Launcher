# Contributing

Thank you for helping improve PipeWire App Launcher.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m pipewire_launcher
```

Ubuntu development hosts also need `pipewire-jack`. AppImage builds need the
packages listed in the README.

## Pull requests

1. Create a focused branch from `main`.
2. Add or update tests for behavior changes.
3. Run the complete test suite.
4. Update the changelog for user-visible changes.
5. Keep commits small and avoid checking in build outputs or user profiles.

Do not include credentials, private audio projects, personal paths, or the
contents of `~/.config/pipewire-app-launcher/`.

## Community standards

By participating, you agree to follow the
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Read [SUPPORT.md](SUPPORT.md) before
opening an Issue so bug reports, feature proposals, security concerns, and
general support requests reach the correct channel.

Report vulnerabilities through the private process in
[SECURITY.md](SECURITY.md), never through a public Issue or Pull Request.
