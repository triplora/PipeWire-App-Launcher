from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_ID = "pipewire-app-launcher"


@dataclass
class Profile:
    name: str
    executable: str
    arguments: list[str] = field(default_factory=list)
    working_directory: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    enabled: bool = True
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def from_dict(cls, value: dict) -> "Profile":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in value.items() if k in allowed}
        data["arguments"] = [str(x) for x in data.get("arguments", [])]
        data["environment"] = {
            str(k): str(v) for k, v in data.get("environment", {}).items()
        }
        return cls(**data)


def parse_arguments(text: str) -> list[str]:
    return shlex.split(text, posix=True)


def parse_environment(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Environment line {line_number} must use KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            raise ValueError(f"Invalid environment name on line {line_number}: {key!r}")
        result[key] = value
    return result


def command_parts(profile: Profile) -> tuple[str, list[str]]:
    return "pw-jack", ["--", profile.executable, *profile.arguments]


def command_preview(profile: Profile) -> str:
    program, arguments = command_parts(profile)
    env = " ".join(f"{k}={shlex.quote(v)}" for k, v in sorted(profile.environment.items()))
    command = shlex.join([program, *arguments])
    return f"{env} {command}".strip()


def validate_profile(profile: Profile) -> list[str]:
    errors: list[str] = []
    if not profile.name.strip():
        errors.append("Profile name is required.")
    if not profile.executable.strip():
        errors.append("Executable is required.")
    elif "/" in profile.executable:
        path = Path(profile.executable).expanduser()
        if not path.is_file():
            errors.append("Executable file does not exist.")
        elif not os.access(path, os.X_OK):
            errors.append("Selected file is not executable.")
    elif shutil.which(profile.executable) is None:
        errors.append(f"Executable {profile.executable!r} was not found in PATH.")
    if profile.working_directory and not Path(profile.working_directory).expanduser().is_dir():
        errors.append("Working directory does not exist.")
    if shutil.which("pw-jack") is None:
        errors.append("pw-jack was not found. Install the pipewire-jack package.")
    return errors


class ProfileStore:
    def __init__(self, path: Path | None = None):
        config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.path = path or config / APP_ID / "profiles.json"

    def load(self) -> list[Profile]:
        if not self.path.exists():
            return []
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("profiles"), list):
            raise ValueError("Unsupported or invalid profile file")
        return [Profile.from_dict(item) for item in value["profiles"]]

    def save(self, profiles: list[Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "profiles": [asdict(p) for p in profiles]},
            indent=2,
            ensure_ascii=False,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=".profiles-", dir=self.path.parent)
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
