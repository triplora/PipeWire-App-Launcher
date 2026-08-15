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
    """Start the full PipeWire user ecosystem in the background.

    ``pipewire-pulse`` must be started explicitly so the PulseAudio
    compatibility server (pulse-server module) comes up with PipeWire; without
    it the system sound tray icon disappears. ``wireplumber`` provides the
    session and routing policies.
    """

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


def _link_target(raw_line: str) -> str:
    """Extract the remote port name from a ``pw-link`` arrow line."""

    line = raw_line.strip()
    for marker in ("|-> ", "|<- "):
        if line.startswith(marker):
            return line[len(marker):].strip()
    return ""


def _parse_link_listing(text: str) -> dict[str, tuple[str, ...]]:
    """Parse ``pw-link -l`` output into ``{port name: linked port names}``.

    Port names are plain lines; every indented ``|->`` / ``|<-`` line that
    follows a port describes one link attached to it.
    """

    ports: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.strip()
        if stripped.startswith(("|->", "|<-")):
            target = _link_target(raw_line)
            if current is not None and target:
                ports[current].append(target)
            continue
        current = stripped
        ports.setdefault(current, [])
    return {name: tuple(links) for name, links in ports.items()}


def _pw_link_ports(
    runner: CommandRunner,
    resolver: Callable[[str], str | None],
    option: str,
) -> dict[str, tuple[str, ...]]:
    """List ``pw-link`` ports for ``option`` (``-o`` outputs, ``-i`` inputs)."""

    if resolver("pw-link") is None:
        return {}
    result = runner(["pw-link", "-l", option])
    if result.returncode != 0:
        return {}
    return _parse_link_listing(result.stdout)


def _channel_token(port_name: str) -> str:
    """Return the trailing channel token (``FL``, ``FR``, ``MONO``, ...)."""

    return port_name.rpartition("_")[2].strip()


def _missing_playback_candidates(
    output_ports: dict[str, tuple[str, ...]],
) -> list[str]:
    """Unlinked app playback stream outputs.

    Only ports named ``output_*`` qualify; this excludes sink monitors
    (``monitor_*``), hardware capture ports (``capture_*``), capture streams
    (``input_*``) and MIDI ports.
    """

    return [
        name
        for name, links in output_ports.items()
        if not links and "output_" in name
    ]


def _playback_targets(
    playback_ports: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """Pick one hardware ``playback_*`` target per channel token.

    Only ports named ``:playback_*`` (the physical speaker outputs) qualify.
    Prefers the sink node that already carries the most stream links (the
    active default sink); ties fall back to the first node reported by
    ``pw-link``.
    """

    by_node: dict[str, dict[str, tuple[str, ...]]] = {}
    for name, links in playback_ports.items():
        if ":playback_" not in name:
            continue
        node, _separator, _ = name.partition(":")
        by_node.setdefault(node, {})[name] = links
    ordered = sorted(
        by_node.values(),
        key=lambda ports: sum(len(links) for links in ports.values()),
        reverse=True,
    )
    targets: dict[str, str] = {}
    for ports in ordered:
        for name in ports:
            targets.setdefault(_channel_token(name), name)
    return targets


def _link_outputs_to_playback(
    runner: CommandRunner,
    output_ports: dict[str, tuple[str, ...]],
    playback_ports: dict[str, tuple[str, ...]],
) -> int:
    """Link unlinked app stream outputs to the hardware playback outputs."""

    targets = _playback_targets(playback_ports)
    created = 0
    for name, links in output_ports.items():
        if links or "output_" not in name:
            continue
        target = targets.get(_channel_token(name))
        if target is None:
            continue
        if runner(["pw-link", name, target]).returncode == 0:
            created += 1
    return created


def restore_default_audio_links(
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
    timeout_ms: int = 3000,
    poll_interval_ms: int = 250,
) -> int:
    """Reconnect app streams to the hardware playback outputs after startup.

    Waits (bounded by ``timeout_ms``) for the hardware ``playback_*`` input
    ports to appear, then links every unlinked application stream output to
    the matching channel of the most active playback node. Returns the number
    of links created.
    """

    if resolver("pw-link") is None:
        return 0
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        playback_ports = _pw_link_ports(runner, resolver, "-i")
        if playback_ports:
            output_ports = _pw_link_ports(runner, resolver, "-o")
            return _link_outputs_to_playback(runner, output_ports, playback_ports)
        if time.monotonic() >= deadline:
            return 0
        time.sleep(poll_interval_ms / 1000.0)


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
        link_restorer: Callable[..., int] = restore_default_audio_links,
    ) -> None:
        self._runner = runner
        self._resolver = resolver
        self._popen = popen
        self._message_box = message_box
        self._poll_interval_ms = poll_interval_ms
        self._start_timeout_ms = start_timeout_ms
        self._link_restorer = link_restorer

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
        self._restore_default_links()
        return True

    def _restore_default_links(self) -> int:
        """Reconnect app streams to the hardware outputs after startup."""

        return self._link_restorer(runner=self._runner, resolver=self._resolver)

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
