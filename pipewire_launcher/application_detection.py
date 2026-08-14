"""Conservative, execution-free discovery of installed JACK applications."""

from __future__ import annotations

import configparser
import os
import re
import shlex
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipewire_launcher.core import Profile


Resolver = Callable[[str], str | None]

_FIELD_CODES = {
    "%f",
    "%F",
    "%u",
    "%U",
    "%d",
    "%D",
    "%n",
    "%N",
    "%i",
    "%c",
    "%k",
    "%v",
    "%m",
}
_UNSAFE_CHARACTERS = frozenset(";&|<>`")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ApplicationCandidate:
    """One safely resolved JACK-capable desktop application."""

    desktop_id: str
    name: str
    executable: str
    arguments: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    source: str = ""

    def profile_key(self) -> tuple[str, tuple[str, ...]]:
        return self.executable, self.arguments

    def to_profile(self) -> Profile:
        return Profile(
            name=self.name,
            executable=self.executable,
            arguments=list(self.arguments),
            environment=dict(self.environment),
            notes=f"Detected from {self.desktop_id}.",
        )


def application_directories(
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> tuple[Path, ...]:
    """Return XDG application directories in desktop-entry precedence order."""

    env = os.environ if environment is None else environment
    user_home = Path.home() if home is None else Path(home)
    data_home = env.get("XDG_DATA_HOME", "").strip()
    user_data = Path(data_home).expanduser() if data_home else user_home / ".local/share"

    raw_system_dirs = env.get("XDG_DATA_DIRS", "").strip()
    if raw_system_dirs:
        system_dirs = [
            Path(value).expanduser()
            for value in raw_system_dirs.split(":")
            if value.strip()
        ]
    else:
        system_dirs = [Path("/usr/local/share"), Path("/usr/share")]

    result: list[Path] = []
    for data_dir in [user_data, *system_dirs]:
        candidate = data_dir / "applications"
        if candidate not in result:
            result.append(candidate)
    return tuple(result)


def _desktop_id(path: Path, directory: Path) -> str:
    relative = path.relative_to(directory)
    return "-".join(relative.parts)


def _read_desktop_entry(path: Path) -> dict[str, str] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    parser = configparser.ConfigParser(
        interpolation=None,
        strict=False,
        delimiters=("=",),
        comment_prefixes=("#",),
        inline_comment_prefixes=None,
    )
    parser.optionxform = str
    try:
        parser.read_string(payload)
    except configparser.Error:
        return None

    if not parser.has_section("Desktop Entry"):
        return None
    return dict(parser.items("Desktop Entry", raw=True))


def _is_true(value: str | None) -> bool:
    return bool(value and value.strip().casefold() == "true")


def _resolve_executable(command: str, resolver: Resolver) -> str | None:
    if not command or "\x00" in command:
        return None
    if "/" not in command:
        resolved = resolver(command)
        return str(Path(resolved)) if resolved else None

    path = Path(command).expanduser()
    try:
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
    except OSError:
        return None
    return str(path)


def _try_exec_available(value: str, resolver: Resolver) -> bool:
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        return False
    return bool(parts and _resolve_executable(parts[0], resolver))


def _contains_unsafe_syntax(token: str) -> bool:
    return (
        "\n" in token
        or "\r" in token
        or "$(" in token
        or any(character in token for character in _UNSAFE_CHARACTERS)
    )


def _remove_field_codes(tokens: Iterable[str]) -> tuple[str, ...] | None:
    result: list[str] = []
    for token in tokens:
        if token in _FIELD_CODES:
            continue
        token = token.replace("%%", "%")
        if "%" in token or _contains_unsafe_syntax(token):
            return None
        result.append(token)
    return tuple(result)


def _parse_exec(
    value: str,
    resolver: Resolver,
) -> tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]] | None:
    try:
        raw_tokens = shlex.split(value, posix=True)
    except ValueError:
        return None
    if not raw_tokens or any(_contains_unsafe_syntax(token) for token in raw_tokens):
        return None

    environment: dict[str, str] = {}
    command_index = 0
    if Path(raw_tokens[0]).name == "env":
        command_index = 1
        if command_index < len(raw_tokens) and raw_tokens[command_index] == "--":
            command_index += 1
        while command_index < len(raw_tokens):
            token = raw_tokens[command_index]
            if "=" not in token:
                break
            key, value_part = token.split("=", 1)
            if not _ENVIRONMENT_NAME.fullmatch(key):
                return None
            environment[key] = value_part
            command_index += 1

    if command_index >= len(raw_tokens):
        return None

    command = raw_tokens[command_index]
    if command.startswith("-"):
        return None
    resolved = _resolve_executable(command, resolver)
    if not resolved:
        return None

    arguments = _remove_field_codes(raw_tokens[command_index + 1 :])
    if arguments is None:
        return None
    return resolved, arguments, tuple(sorted(environment.items()))


def _is_known_jack_application(
    desktop_id: str,
    executable: str,
    arguments: tuple[str, ...],
) -> bool:
    del desktop_id
    executable_name = Path(executable).name.casefold()

    if executable_name.startswith("ardour"):
        return True
    if executable_name in {
        "audacity",
        "carla",
        "calfjackhost",
        "xjadeo",
        "raysession",
    }:
        return True
    if executable_name == "gmidimonitor":
        return "--jack" in arguments
    return False


def parse_application_candidate(
    path: Path,
    directory: Path,
    *,
    resolver: Resolver = shutil.which,
) -> ApplicationCandidate | None:
    """Parse one desktop file without executing its command."""

    values = _read_desktop_entry(path)
    if values is None:
        return None
    if values.get("Type", "").strip() != "Application":
        return None
    if _is_true(values.get("Hidden")) or _is_true(values.get("NoDisplay")):
        return None

    name = values.get("Name", "").strip()
    exec_value = values.get("Exec", "").strip()
    if not name or not exec_value:
        return None

    try_exec = values.get("TryExec", "").strip()
    if try_exec and not _try_exec_available(try_exec, resolver):
        return None

    parsed = _parse_exec(exec_value, resolver)
    if parsed is None:
        return None
    executable, arguments, environment = parsed
    desktop_id = _desktop_id(path, directory)

    if not _is_known_jack_application(desktop_id, executable, arguments):
        return None

    return ApplicationCandidate(
        desktop_id=desktop_id,
        name=name,
        executable=executable,
        arguments=arguments,
        environment=environment,
        source=str(path),
    )


def detect_jack_applications(
    directories: Sequence[Path] | None = None,
    *,
    resolver: Resolver = shutil.which,
) -> tuple[ApplicationCandidate, ...]:
    """Return deterministic, deduplicated candidates from desktop entries."""

    search_directories = (
        application_directories()
        if directories is None
        else tuple(Path(directory) for directory in directories)
    )
    seen_desktop_ids: set[str] = set()
    seen_profiles: set[tuple[str, tuple[str, ...]]] = set()
    candidates: list[ApplicationCandidate] = []

    for directory in search_directories:
        try:
            paths = sorted(directory.rglob("*.desktop"))
        except OSError:
            continue
        for path in paths:
            try:
                desktop_id = _desktop_id(path, directory)
            except ValueError:
                continue
            if desktop_id in seen_desktop_ids:
                continue
            # A higher-precedence hidden or invalid entry masks the lower one.
            seen_desktop_ids.add(desktop_id)

            candidate = parse_application_candidate(
                path,
                directory,
                resolver=resolver,
            )
            if candidate is None:
                continue
            key = candidate.profile_key()
            if key in seen_profiles:
                continue
            seen_profiles.add(key)
            candidates.append(candidate)

    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.name.casefold(),
                item.executable,
                item.arguments,
                item.desktop_id,
            ),
        )
    )


def profiles_from_candidates(
    candidates: Iterable[ApplicationCandidate],
    existing_profiles: Iterable[Profile] = (),
) -> list[Profile]:
    """Create profiles while preserving existing profile identities."""

    existing_keys = {
        (profile.executable, tuple(profile.arguments))
        for profile in existing_profiles
    }
    result: list[Profile] = []
    for candidate in candidates:
        if candidate.profile_key() in existing_keys:
            continue
        profile = candidate.to_profile()
        existing_keys.add(candidate.profile_key())
        result.append(profile)
    return result
