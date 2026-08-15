"""Discovery, enable/disable state, and launch commands for installed audio applications.

Audio applications are found by scanning XDG application directories for
``.desktop`` entries (never executing anything) and accepting candidates whose
freedesktop ``Categories`` include audio-related values or whose executable is
a known audio application.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from pipewire_launcher.application_detection import (
    ApplicationCandidate,
    Resolver,
    scan_application_candidates,
)
from pipewire_launcher.core import APP_ID, command_parts


_AUDIO_CATEGORIES = frozenset({
    "audio",
    "audiovideo",
    "audiovideoediting",
    "midi",
    "music",
    "sequencer",
})

_KNOWN_AUDIO_EXECUTABLES = frozenset({
    "ardour",
    "ardour6",
    "ardour7",
    "ardour8",
    "audacity",
    "bitwig-studio",
    "calfjackhost",
    "carla",
    "easeroute",
    "easyeffects",
    "gmidimonitor",
    "helvum",
    "hydrogen",
    "jamesdsp",
    "lmms",
    "mixxx",
    "muse",
    "musescore",
    "musescore3",
    "musescore4",
    "pulseeffects",
    "pavucontrol",
    "qjackctl",
    "qpwgraph",
    "qtractor",
    "raysession",
    "reaper",
    "rosegarden",
    "yoshimi",
    "zrythm",
})


def _has_audio_categories(candidate: ApplicationCandidate) -> bool:
    return any(
        category.casefold() in _AUDIO_CATEGORIES
        for category in candidate.categories
    )


def _is_known_audio_application(candidate: ApplicationCandidate) -> bool:
    return Path(candidate.executable).name.casefold() in _KNOWN_AUDIO_EXECUTABLES


def _is_audio_application(candidate: ApplicationCandidate) -> bool:
    return _has_audio_categories(candidate) or _is_known_audio_application(candidate)


def detect_audio_applications(
    directories: Sequence[Path] | None = None,
    *,
    resolver: Resolver = shutil.which,
) -> tuple[ApplicationCandidate, ...]:
    """Return installed audio applications, deterministically sorted."""

    return scan_application_candidates(
        directories,
        resolver=resolver,
        predicate=_is_audio_application,
    )


class AudioApplicationStore:
    """Persistent enable/disable state for detected audio applications."""

    def __init__(self, path: Path | None = None):
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = path or config / APP_ID / "audio_applications.json"

    def load(self) -> dict[str, bool]:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("apps"), dict):
            raise ValueError("Unsupported or invalid audio applications file")
        return {str(key): bool(item) for key, item in value["apps"].items()}

    def save(self, enabled: Mapping[str, bool]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "apps": {str(k): bool(v) for k, v in enabled.items()}},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=".audio-apps-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class AudioApplicationManager:
    """Coordinates detection, enable/disable state, and launch commands."""

    def __init__(
        self,
        store: AudioApplicationStore | None = None,
        detect=detect_audio_applications,
        *,
        resolver: Resolver = shutil.which,
    ):
        self.store = store or AudioApplicationStore()
        self._detect = detect
        self._resolver = resolver
        self._applications: tuple[ApplicationCandidate, ...] = ()
        self._enabled: dict[str, bool] = {}

    def refresh(self, directories: Sequence[Path] | None = None) -> tuple[ApplicationCandidate, ...]:
        """Re-scan for installed audio applications."""

        self._applications = self._detect(directories, resolver=self._resolver)
        return self._applications

    def applications(self) -> tuple[ApplicationCandidate, ...]:
        return self._applications

    def load_enabled(self) -> None:
        self._enabled = self.store.load()

    def is_enabled(self, application: ApplicationCandidate) -> bool:
        return self._enabled.get(application.desktop_id, True)

    def set_enabled(self, application: ApplicationCandidate, enabled: bool) -> None:
        self._enabled[application.desktop_id] = enabled
        self.store.save(self._enabled)

    def enabled_state(self) -> dict[str, bool]:
        return dict(self._enabled)

    def launch_command(self, application: ApplicationCandidate) -> tuple[str, list[str]]:
        """Build the launch command through the ``pw-jack`` PipeWire wrapper."""

        return command_parts(application.to_profile())
