"""Asynchronous lifecycle control for PipeWire and qpwgraph."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from pipewire_launcher.pipewire_health import (
    pipewire_running,
    qpwgraph_running,
    restart_pipewire_services,
    restore_default_audio_links,
    start_pipewire_services_sync,
    start_qpwgraph,
    stop_pipewire_services,
    stop_qpwgraph,
)


class AudioStackState(Enum):
    """Stable and transitional states exposed to the interface."""

    STOPPED = "stopped"
    PIPEWIRE_ONLY = "pipewire_only"
    RUNNING = "running"
    ORPHANED_QPWGRAPH = "orphaned_qpwgraph"
    CHECKING = "checking"
    STARTING = "starting"
    STOPPING = "stopping"
    RESTARTING = "restarting"
    FAILED = "failed"

    @property
    def busy(self) -> bool:
        return self in {
            AudioStackState.CHECKING,
            AudioStackState.STARTING,
            AudioStackState.STOPPING,
            AudioStackState.RESTARTING,
        }


@dataclass(frozen=True)
class AudioStackSnapshot:
    """Observed state of the two managed applications."""

    pipewire: bool
    qpwgraph: bool

    @property
    def state(self) -> AudioStackState:
        if self.pipewire and self.qpwgraph:
            return AudioStackState.RUNNING
        if self.pipewire:
            return AudioStackState.PIPEWIRE_ONLY
        if self.qpwgraph:
            return AudioStackState.ORPHANED_QPWGRAPH
        return AudioStackState.STOPPED


class AudioStackOperationError(RuntimeError):
    """A bounded lifecycle operation did not reach its expected state."""


class AudioStackController(QObject):
    """Serialize stack operations outside the GUI thread and publish state."""

    state_changed = Signal(object)
    operation_failed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        runner=None,
        resolver: Callable[[str], str | None] = shutil.which,
        popen=subprocess.Popen,
        poll_interval_ms: int = 1500,
        operation_poll_ms: int = 100,
        operation_timeout_ms: int = 8000,
        detector: Callable[[], AudioStackSnapshot] | None = None,
        pipewire_starter: Callable[[], bool] | None = None,
        pipewire_stopper: Callable[[], bool] | None = None,
        pipewire_restarter: Callable[[], bool] | None = None,
        qpwgraph_starter: Callable[[], bool] | None = None,
        qpwgraph_stopper: Callable[[], bool] | None = None,
        link_restorer: Callable[[], int] | None = None,
        watcher_start: Callable[[], object] | None = None,
        watcher_stop: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        from pipewire_launcher.pipewire_health import run_command

        self._runner = runner or run_command
        self._resolver = resolver
        self._popen = popen
        self._operation_poll_s = operation_poll_ms / 1000.0
        self._operation_timeout_s = operation_timeout_ms / 1000.0
        self._detector = detector or self._detect
        self._pipewire_starter = pipewire_starter or (
            lambda: start_pipewire_services_sync(
                runner=self._runner, resolver=self._resolver
            )
        )
        self._pipewire_stopper = pipewire_stopper or (
            lambda: stop_pipewire_services(
                runner=self._runner, resolver=self._resolver
            )
        )
        self._pipewire_restarter = pipewire_restarter or (
            lambda: restart_pipewire_services(
                runner=self._runner, resolver=self._resolver
            )
        )
        self._qpwgraph_starter = qpwgraph_starter or (
            lambda: start_qpwgraph(resolver=self._resolver, popen=self._popen)
        )
        self._qpwgraph_stopper = qpwgraph_stopper or (
            lambda: stop_qpwgraph(
                runner=self._runner,
                resolver=self._resolver,
                timeout_ms=int(self._operation_timeout_s * 1000),
                poll_interval_ms=max(10, operation_poll_ms),
            )
        )
        self._link_restorer = link_restorer or (
            lambda: restore_default_audio_links(
                runner=self._runner, resolver=self._resolver
            )
        )
        self._watcher_start = watcher_start or (lambda: None)
        self._watcher_stop = watcher_stop or (lambda: None)
        self._state = AudioStackState.CHECKING
        self._snapshot: AudioStackSnapshot | None = None
        self._generation = 0
        self._future: Future | None = None
        self._closed = False
        self._cancel_event = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="audio-stack-controller"
        )
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self.refresh)
        self._completion_timer = QTimer(self)
        self._completion_timer.setInterval(25)
        self._completion_timer.timeout.connect(self._collect_future)

    @property
    def state(self) -> AudioStackState:
        return self._state

    @property
    def snapshot(self) -> AudioStackSnapshot | None:
        return self._snapshot

    @property
    def busy(self) -> bool:
        return self._future is not None

    def start_monitoring(self) -> bool:
        if self._closed:
            return False
        self._poll_timer.start()
        return self.refresh()

    def refresh(self) -> bool:
        transitional = (
            AudioStackState.CHECKING if self._snapshot is None else None
        )
        return self._submit(transitional, self._detector)

    def start_stack(self) -> bool:
        return self._submit(AudioStackState.STARTING, self._start_stack)

    def stop_stack(self) -> bool:
        return self._submit(AudioStackState.STOPPING, self._stop_stack)

    def restart_stack(self) -> bool:
        return self._submit(AudioStackState.RESTARTING, self._restart_stack)

    def trigger(self) -> bool:
        if self._state == AudioStackState.RUNNING:
            return self.stop_stack()
        if self._state == AudioStackState.PIPEWIRE_ONLY:
            return self.restart_stack()
        if self._state in {
            AudioStackState.STOPPED,
            AudioStackState.ORPHANED_QPWGRAPH,
            AudioStackState.FAILED,
        }:
            return self.start_stack()
        return False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_event.set()
        self._generation += 1
        self._cancel_event.clear()
        self._poll_timer.stop()
        self._completion_timer.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _submit(
        self,
        transitional: AudioStackState | None,
        operation,
    ) -> bool:
        if self._closed or self._future is not None:
            return False
        self._generation += 1
        if transitional is not None:
            self._state = transitional
            self.state_changed.emit(transitional)
        self._future = self._executor.submit(operation)
        self._completion_timer.start()
        return True

    def _collect_future(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        self._completion_timer.stop()
        self._future = None
        if self._closed:
            return
        try:
            snapshot = future.result()
        except Exception as exc:
            message = " ".join(str(exc).split())[:240] or type(exc).__name__
            self._state = AudioStackState.FAILED
            self.operation_failed.emit(message)
            self.state_changed.emit(self._state)
            QTimer.singleShot(0, self.refresh)
            return
        self._snapshot = snapshot
        self._state = snapshot.state
        self.state_changed.emit(self._state)

    def _detect(self) -> AudioStackSnapshot:
        return AudioStackSnapshot(
            pipewire=pipewire_running(
                runner=self._runner, resolver=self._resolver
            ),
            qpwgraph=qpwgraph_running(
                runner=self._runner, resolver=self._resolver
            ),
        )

    def _wait_for(self, predicate, description: str) -> None:
        deadline = time.monotonic() + self._operation_timeout_s
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                raise AudioStackOperationError("Audio stack operation cancelled")
            if predicate():
                return
            time.sleep(self._operation_poll_s)
        if predicate():
            return
        raise AudioStackOperationError(f"Timed out waiting for {description}")

    def _start_stack(self) -> AudioStackSnapshot:
        current = self._detector()
        if not current.pipewire and current.qpwgraph:
            if not self._qpwgraph_stopper():
                raise AudioStackOperationError("Could not stop orphaned qpwgraph")
        if not current.pipewire:
            if not self._pipewire_starter():
                raise AudioStackOperationError("Could not start PipeWire services")
            self._wait_for(lambda: self._detector().pipewire, "PipeWire startup")
        if not self._detector().qpwgraph:
            if not self._qpwgraph_starter():
                raise AudioStackOperationError("Could not start qpwgraph")
        self._wait_for(
            lambda: self._detector().state == AudioStackState.RUNNING,
            "PipeWire and qpwgraph startup",
        )
        self._link_restorer()
        self._watcher_start()
        return self._detector()

    def _stop_stack(self) -> AudioStackSnapshot:
        self._watcher_stop()
        current = self._detector()
        if current.qpwgraph and not self._qpwgraph_stopper():
            raise AudioStackOperationError("Could not stop qpwgraph")
        if current.pipewire and not self._pipewire_stopper():
            raise AudioStackOperationError("Could not stop PipeWire services")
        self._wait_for(
            lambda: self._detector().state == AudioStackState.STOPPED,
            "audio stack shutdown",
        )
        return self._detector()

    def _restart_stack(self) -> AudioStackSnapshot:
        self._watcher_stop()
        current = self._detector()
        if current.qpwgraph and not self._qpwgraph_stopper():
            raise AudioStackOperationError("Could not stop qpwgraph")
        if current.pipewire:
            if not self._pipewire_restarter():
                raise AudioStackOperationError("Could not restart PipeWire services")
        elif not self._pipewire_starter():
            raise AudioStackOperationError("Could not start PipeWire services")
        self._wait_for(lambda: self._detector().pipewire, "PipeWire restart")
        if not self._qpwgraph_starter():
            raise AudioStackOperationError("Could not start qpwgraph")
        self._wait_for(
            lambda: self._detector().state == AudioStackState.RUNNING,
            "qpwgraph restart",
        )
        self._link_restorer()
        self._watcher_start()
        return self._detector()
