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


_NON_STREAM_OUTPUT_MARKERS = (
    ":capture_",
    ":monitor_",
    ":input_",
    "Midi-Bridge",
)


def _is_stream_output(port_name: str) -> bool:
    """Return whether a port name looks like an application stream output.

    Excludes hardware capture ports (``:capture_*``), sink monitors
    (``:monitor_*``), capture streams (``:input_*``) and MIDI ports, while
    still accepting JACK-style stream names that lack the ``output_`` prefix.
    """

    return not any(marker in port_name for marker in _NON_STREAM_OUTPUT_MARKERS)


def _missing_playback_candidates(
    output_ports: dict[str, tuple[str, ...]],
) -> list[str]:
    """Unlinked app playback stream outputs.

    Hardware capture, sink monitor, capture-stream and MIDI ports never
    qualify.
    """

    return [
        name
        for name, links in output_ports.items()
        if not links and _is_stream_output(name)
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


def _sink_playback_targets(
    playback_ports: dict[str, tuple[str, ...]],
    sink_node_name: str | None,
) -> dict[str, str]:
    """Pick the ``playback_*`` target per channel for a specific sink node.

    Falls back to :func:`_playback_targets` when the sink node is unknown or
    exposes no playback ports.
    """

    if sink_node_name is None:
        return _playback_targets(playback_ports)
    targets: dict[str, str] = {}
    for name in playback_ports:
        if name.startswith(f"{sink_node_name}:") and ":playback_" in name:
            targets.setdefault(_channel_token(name), name)
    if targets:
        return targets
    return _playback_targets(playback_ports)


def _link_outputs_to_playback(
    runner: CommandRunner,
    output_ports: dict[str, tuple[str, ...]],
    playback_ports: dict[str, tuple[str, ...]],
    sink_node_name: str | None = None,
) -> int:
    """Link unlinked app stream outputs to the hardware playback outputs."""

    targets = _sink_playback_targets(playback_ports, sink_node_name)
    created = 0
    for name, links in output_ports.items():
        if links or not _is_stream_output(name):
            continue
        target = targets.get(_channel_token(name))
        if target is None:
            continue
        if runner(["pw-link", name, target]).returncode == 0:
            created += 1
    return created


def _hardware_ports_visible(
    runner: CommandRunner,
    resolver: Callable[[str], str | None],
) -> bool:
    """Return whether physical hardware audio ports are visible to pw-link.

    Checks the ``:playback_`` input ports (speakers) and the ``:capture_``
    output ports (microphones); either one is enough to confirm the hardware
    devices have registered with PipeWire.
    """

    input_ports = _pw_link_ports(runner, resolver, "-i")
    if any(":playback_" in name for name in input_ports):
        return True
    output_ports = _pw_link_ports(runner, resolver, "-o")
    return any(":capture_" in name for name in output_ports)


def _pw_cli_nodes(
    runner: CommandRunner,
    resolver: Callable[[str], str | None],
) -> list[dict[str, str]]:
    """Parse ``pw-cli list-objects Node`` into per-node property dicts."""

    if resolver("pw-cli") is None:
        return []
    result = runner(["pw-cli", "list-objects", "Node"])
    if result.returncode != 0:
        return []
    nodes: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in result.stdout.splitlines():
        if (
            "PipeWire:Interface:Node" in raw_line
            and raw_line.lstrip().startswith("id ")
        ):
            current = {}
            nodes.append(current)
            continue
        if current is None or "=" not in raw_line:
            continue
        key, _, value = raw_line.strip().partition("=")
        current[key.strip()] = value.strip().strip('"')
    return nodes


def _hardware_sink_nodes(
    runner: CommandRunner,
    resolver: Callable[[str], str | None],
) -> list[dict[str, str]]:
    """Return the ALSA hardware sink nodes present in the graph."""

    return [
        props
        for props in _pw_cli_nodes(runner, resolver)
        if props.get("media.class") == "Audio/Sink"
        and props.get("node.name", "").startswith("alsa_output.")
    ]


def _wpctl_default_sink(
    runner: CommandRunner,
    resolver: Callable[[str], str | None],
) -> str | None:
    """Return the node description of the default sink from ``wpctl status``.

    WirePlumber marks the default sink with ``*`` in the Sinks section; the
    description is matched back to a node name via ``pw-cli``.
    """

    if resolver("wpctl") is None:
        return None
    result = runner(["wpctl", "status"])
    if result.returncode != 0:
        return None
    in_sinks = False
    for raw_line in result.stdout.splitlines():
        stripped = raw_line.strip()
        if "Sinks:" in stripped:
            in_sinks = True
            continue
        if not in_sinks:
            continue
        if stripped.startswith(("├─", "└─")):
            return None
        if "*" not in stripped:
            continue
        rest = stripped.lstrip("│├└─* \t")
        _number, separator, description = rest.partition(". ")
        if not separator:
            continue
        return description.partition(" [")[0].strip() or None
    return None


def _main_sink_node_name(
    runner: CommandRunner,
    resolver: Callable[[str], str | None],
) -> str | None:
    """Exact node name of the default hardware sink, or None when unknown."""

    sinks = _hardware_sink_nodes(runner, resolver)
    if not sinks:
        return None
    default_description = _wpctl_default_sink(runner, resolver)
    if default_description:
        for props in sinks:
            if props.get("node.description") == default_description:
                return props["node.name"]
    return None


def restore_default_audio_links(
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
    timeout_ms: int = 5000,
    poll_interval_ms: int = 500,
) -> int:
    """Reconnect app streams to the hardware playback outputs after startup.

    Polls ``pw-link`` every ``poll_interval_ms`` (bounded by ``timeout_ms``)
    until physical hardware ports (``:playback_`` inputs or ``:capture_``
    outputs) become visible. The main hardware sink is then identified via
    ``pw-cli`` + ``wpctl`` so the FL/FR channels are forced onto the active
    default device (WirePlumber leaves idle, stopped streams unlinked), and
    every unlinked application stream output is linked to its matching channel.
    Returns the number of links created.
    """

    if resolver("pw-link") is None:
        return 0
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        if _hardware_ports_visible(runner, resolver):
            output_ports = _pw_link_ports(runner, resolver, "-o")
            playback_ports = _pw_link_ports(runner, resolver, "-i")
            sink_node_name = _main_sink_node_name(runner, resolver)
            return _link_outputs_to_playback(
                runner, output_ports, playback_ports, sink_node_name
            )
        if time.monotonic() >= deadline:
            return 0
        time.sleep(poll_interval_ms / 1000.0)


def qpwgraph_running(
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
) -> bool:
    """Return whether a ``qpwgraph`` instance is currently running."""

    if resolver("pgrep") is None:
        return False
    return runner(["pgrep", "-x", "qpwgraph"]).returncode == 0


def restart_qpwgraph(
    *,
    runner: CommandRunner = run_command,
    resolver: Callable[[str], str | None] = shutil.which,
    popen: Callable[..., Any] = subprocess.Popen,
) -> bool:
    """Restart qpwgraph so it registers a fresh system tray icon.

    When the PipeWire sockets go down and come back up, a leftover qpwgraph
    keeps a stale connection and never re-registers its Ubuntu tray icon.
    Force-close any running instance and launch a clean one in the background.
    Returns whether the fresh instance was launched.
    """

    if (
        qpwgraph_running(runner=runner, resolver=resolver)
        and resolver("killall") is not None
    ):
        runner(["killall", "-9", "qpwgraph"])
    if resolver("qpwgraph") is None:
        return False
    try:
        popen(
            ["qpwgraph"],
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
        link_restorer: Callable[..., int] = restore_default_audio_links,
        qpwgraph_restarter: Callable[..., bool] = restart_qpwgraph,
    ) -> None:
        self._runner = runner
        self._resolver = resolver
        self._popen = popen
        self._message_box = message_box
        self._poll_interval_ms = poll_interval_ms
        self._start_timeout_ms = start_timeout_ms
        self._link_restorer = link_restorer
        self._qpwgraph_restarter = qpwgraph_restarter

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
        self._restart_qpwgraph()
        return True

    def _restore_default_links(self) -> int:
        """Reconnect app streams to the hardware outputs after startup."""

        return self._link_restorer(runner=self._runner, resolver=self._resolver)

    def _restart_qpwgraph(self) -> bool:
        """Restart qpwgraph so it re-registers its system tray icon."""

        return self._qpwgraph_restarter(
            runner=self._runner,
            resolver=self._resolver,
            popen=self._popen,
        )

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
