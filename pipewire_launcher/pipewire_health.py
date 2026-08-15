"""Startup health check for the PipeWire audio server.

Detection prefers the per-user systemd unit state and falls back to process
and socket probes when no usable user bus exists. If the server is not
running, the coordinator asks the user whether to start it and aborts the
launcher when they decline or when startup fails.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QMessageBox, QWidget

from pipewire_launcher.core import APP_ID


_START_QUESTION = (
    "O servidor de áudio PipeWire não está rodando. Deseja iniciá-lo agora?"
)
_START_DECLINED_WARNING = (
    "O servidor de áudio PipeWire é necessário para usar o launcher.\n"
    "O aplicativo será encerrado."
)
_START_FAILED_WARNING = (
    "Não foi possível iniciar o servidor PipeWire.\n"
    "O aplicativo será encerrado."
)

_PIPEWIRE_SERVICES = ("pipewire", "pipewire-pulse", "wireplumber")


@dataclass(frozen=True)
class CommandResult:
    """Return value of a non-interactive command invocation."""

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


def run_command(arguments: Sequence[str], *, timeout: float = 5.0) -> CommandResult:
    """Execute a command without a shell and return its captured output."""

    try:
        completed = subprocess.run(
            list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(
            returncode=-1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def systemd_unit_active(
    service: str = "pipewire",
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
) -> bool | None:
    """Return True/False when systemd gives a definitive answer, None otherwise.

    ``systemctl --user is-active`` exits with status 0 only for an active unit,
    so a nonzero status (inactive, unknown, or a missing user bus) is not
    treated as a definitive negative.
    """

    if resolver("systemctl") is None:
        return None
    result = runner(["systemctl", "--user", "is-active", service])
    if result.returncode != 0:
        return None
    return result.stdout.strip().casefold() == "active"


def pipewire_process_running(
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Return whether a PipeWire server process or runtime socket exists."""

    if resolver("pgrep") is not None:
        result = runner(["pgrep", "-x", "pipewire"])
        return result.returncode == 0
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return False
    try:
        return Path(runtime).joinpath("pipewire-0").is_socket()
    except OSError:
        return False


def pipewire_running(
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Return whether the PipeWire audio server is running and active."""

    systemd = systemd_unit_active(runner=runner, resolver=resolver)
    if systemd is True:
        return True
    return pipewire_process_running(runner=runner, resolver=resolver)


def start_pipewire_services(
    services: Sequence[str] = _PIPEWIRE_SERVICES,
    *,
    resolver: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., Any] = subprocess.Popen,
) -> bool:
    """Start the PipeWire user units in the background."""

    if resolver("systemctl") is None:
        return False
    try:
        popen(
            ["systemctl", "--user", "start", *services],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


class PipeWireHealthCheck:
    """Ask the user about a missing PipeWire server and start it when asked."""

    def __init__(
        self,
        *,
        runner: CommandRunner = run_command,
        resolver: Callable[[str], str | None] = shutil.which,
        popen: Callable[..., Any] = subprocess.Popen,
        message_box: Any = QMessageBox,
        poll_interval_ms: int = 250,
        start_timeout_ms: int = 8000,
    ) -> None:
        self._runner = runner
        self._resolver = resolver
        self._popen = popen
        self._message_box = message_box
        self._poll_interval_ms = poll_interval_ms
        self._start_timeout_ms = start_timeout_ms

    def running(self) -> bool:
        return pipewire_running(runner=self._runner, resolver=self._resolver)

    def check(self, parent: QWidget | None = None) -> bool:
        """Return True to proceed with the launcher, False to abort."""

        if self.running():
            return True
        if not self._ask_to_start(parent):
            return False
        if not start_pipewire_services(
            resolver=self._resolver,
            popen=self._popen,
        ):
            return self._abort_start_failed(parent)
        if not self._wait_until_running():
            return self._abort_start_failed(parent)
        return True

    def _ask_to_start(self, parent: QWidget | None) -> bool:
        answer = self._message_box.question(
            parent,
            f"{APP_ID} — PipeWire not running",
            _START_QUESTION,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            return True
        self._message_box.warning(
            parent,
            f"{APP_ID} — PipeWire required",
            _START_DECLINED_WARNING,
        )
        return False

    def _abort_start_failed(self, parent: QWidget | None) -> bool:
        self._message_box.warning(
            parent,
            f"{APP_ID} — PipeWire start failed",
            _START_FAILED_WARNING,
        )
        return False

    def _wait_until_running(self) -> bool:
        deadline = time.monotonic() + self._start_timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if self.running():
                return True
            time.sleep(self._poll_interval_ms / 1000.0)
        return self.running()
