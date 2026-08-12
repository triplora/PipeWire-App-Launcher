"""Asynchronous, read-only execution boundary for ``pw-dump``."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Sequence

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QProcess, QTimer, Signal

from pipewire_launcher.pipewire_discovery import (
    DiscoveryState,
    PipeWireDiscoverySnapshot,
    PipeWireDumpParseError,
    parse_pw_dump,
)


class RunnerState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def active(self) -> bool:
        return self in {self.STARTING, self.RUNNING, self.CANCELLING}


class DiscoveryFailureCategory(str, Enum):
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    FAILED_TO_START = "failed_to_start"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    NONZERO_EXIT = "nonzero_exit"
    CRASHED = "crashed"
    STDOUT_TOO_LARGE = "stdout_too_large"
    STDERR_TOO_LARGE = "stderr_too_large"
    INVALID_OUTPUT = "invalid_output"
    PARSER_ERROR = "parser_error"
    ALREADY_RUNNING = "already_running"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class PipeWireDumpResult:
    request_id: object | None
    snapshot: PipeWireDiscoverySnapshot
    stderr: bytes
    exit_code: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "stderr", bytes(self.stderr))


@dataclass(frozen=True)
class PipeWireDiscoveryFailure:
    category: DiscoveryFailureCategory
    message: str
    request_id: object | None
    stderr: bytes = b""
    exit_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stderr", bytes(self.stderr))


class PipeWireDiscoveryRunner(QObject):
    """Own and asynchronously execute one bounded ``pw-dump`` request."""

    _POST_KILL_WATCHDOG_MS = 100
    _REAP_INTERVAL_MS = 25
    _SHUTDOWN_WAIT_MS = 100

    state_changed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    request_rejected = Signal(object)
    finished = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        executable: str = "pw-dump",
        arguments: Sequence[str] = ("-N",),
        timeout_ms: int = 2000,
        terminate_grace_ms: int = 500,
        stdout_limit_bytes: int = 4 * 1024 * 1024,
        stderr_limit_bytes: int = 512 * 1024,
        process_factory: Callable[[QObject], QProcess] | None = None,
    ) -> None:
        super().__init__(parent)
        if not isinstance(executable, str):
            raise TypeError("executable must be a string")
        if not executable.strip():
            raise ValueError("executable must not be empty")
        if isinstance(arguments, (str, bytes)):
            raise TypeError("arguments must be a sequence of strings")
        try:
            normalized_arguments = tuple(arguments)
        except TypeError as exc:
            raise TypeError("arguments must be a sequence of strings") from exc
        if any(not isinstance(argument, str) for argument in normalized_arguments):
            raise TypeError("arguments must be a sequence of strings")
        for name, value, allow_zero in (
            ("timeout_ms", timeout_ms, False),
            ("terminate_grace_ms", terminate_grace_ms, True),
            ("stdout_limit_bytes", stdout_limit_bytes, False),
            ("stderr_limit_bytes", stderr_limit_bytes, False),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0 or (value == 0 and not allow_zero):
                comparison = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be {comparison}")

        self._executable = executable
        self._arguments = normalized_arguments
        self._timeout_ms = timeout_ms
        self._terminate_grace_ms = terminate_grace_ms
        self._stdout_limit = stdout_limit_bytes
        self._stderr_limit = stderr_limit_bytes
        self._process_factory = process_factory or (lambda owner: QProcess(owner))
        self._state = RunnerState.IDLE
        self._result: PipeWireDumpResult | None = None
        self._error: PipeWireDiscoveryFailure | None = None
        self._process: Any | None = None
        self._retired_process: Any | None = None
        self._retired_reap_timer: QTimer | None = None
        self._retired_kill_sent = False
        self._shutdown_requested = False
        self._deferred_delete_requested = False
        self._deferred_delete_release_scheduled = False
        self._allow_deferred_delete = False
        self._timeout_timer: QTimer | None = None
        self._grace_timer: QTimer | None = None
        self._kill_watchdog_timer: QTimer | None = None
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._request_id: object | None = None
        self._generation = 0
        self._completed = True
        self._pending_category: DiscoveryFailureCategory | None = None
        self._pending_state: RunnerState | None = None

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def active(self) -> bool:
        return self._state.active

    @property
    def result(self) -> PipeWireDumpResult | None:
        return self._result

    @property
    def error(self) -> PipeWireDiscoveryFailure | None:
        return self._error

    def start(self, request_id: object | None = None) -> bool:
        if self._shutdown_requested or self._deferred_delete_requested:
            return False
        if self.active:
            self.request_rejected.emit(
                PipeWireDiscoveryFailure(
                    DiscoveryFailureCategory.ALREADY_RUNNING,
                    "discovery request already running",
                    request_id,
                )
            )
            return False
        if self._retired_process is not None:
            try:
                retired_state = self._retired_process.state()
            except (AttributeError, RuntimeError):
                retired_state = QProcess.NotRunning
            if retired_state != QProcess.NotRunning:
                self.request_rejected.emit(
                    PipeWireDiscoveryFailure(
                        DiscoveryFailureCategory.ALREADY_RUNNING,
                        "previous discovery process is still being reaped",
                        request_id,
                    )
                )
                return False
            self._finalize_retired_process(self._retired_process)

        self._generation += 1
        generation = self._generation
        self._request_id = request_id
        self._result = None
        self._error = None
        self._stdout.clear()
        self._stderr.clear()
        self._pending_category = None
        self._pending_state = None
        self._completed = False
        self._set_state(RunnerState.STARTING)
        try:
            self._profile_id = "" if request_id is None else (
                request_id if isinstance(request_id, str) else str(request_id)
            )
        except Exception as exc:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.INTERNAL_ERROR,
                f"request identity conversion failed: {type(exc).__name__}",
                RunnerState.FAILED,
            )
            return True

        executable_missing = (
            ("/" not in self._executable and shutil.which(self._executable) is None)
            or (
                "/" in self._executable
                and (
                    not os.path.isfile(self._executable)
                    or not os.access(self._executable, os.X_OK)
                )
            )
        )
        if executable_missing:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.EXECUTABLE_NOT_FOUND,
                "pw-dump executable was not found",
                RunnerState.FAILED,
            )
            return True

        try:
            process = self._process_factory(self)
            self._process = process
            process.setProcessChannelMode(QProcess.SeparateChannels)
            process.started.connect(lambda: self._on_started(generation, process))
            process.readyReadStandardOutput.connect(
                lambda: self._read_output(generation, process, True)
            )
            process.readyReadStandardError.connect(
                lambda: self._read_output(generation, process, False)
            )
            process.finished.connect(
                lambda code, status: self._on_finished(generation, process, code, status)
            )
            process.errorOccurred.connect(
                lambda error: self._on_error(generation, process, error)
            )
            self._timeout_timer = QTimer(self)
            self._timeout_timer.setSingleShot(True)
            self._timeout_timer.timeout.connect(lambda: self._on_timeout(generation, process))
            self._timeout_timer.start(self._timeout_ms)
            process.start(self._executable, list(self._arguments))
        except Exception as exc:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.INTERNAL_ERROR,
                f"discovery runner failed to start: {type(exc).__name__}",
                RunnerState.FAILED,
            )
        return True

    def shutdown(self) -> bool:
        """Permanently close the runner with bounded process termination."""
        if self._shutdown_requested:
            return False
        self._shutdown_requested = True
        self._clear_timer("_timeout_timer")
        self._clear_timer("_grace_timer")
        self._clear_timer("_kill_watchdog_timer")
        self._clear_timer("_retired_reap_timer")
        process = self._process or self._retired_process
        if process is None:
            self._maybe_release_deferred_delete()
            return True
        if self._process is not None and not self._completed:
            # Shutdown is silent: the public terminal result is not changed.
            self._completed = True
        self._shutdown_process(process)
        return True

    close = shutdown

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.DeferredDelete and not self._allow_deferred_delete:
            self._deferred_delete_requested = True
            self.shutdown()
            return True
        return super().event(event)

    def _shutdown_process(self, process: Any) -> None:
        try:
            if process.state() == QProcess.NotRunning:
                self._finalize_shutdown_process(process)
                return
            process.terminate()
            if self._wait_process(process, self._SHUTDOWN_WAIT_MS):
                self._finalize_shutdown_process(process)
                return
            process.kill()
            if self._wait_process(process, self._SHUTDOWN_WAIT_MS):
                self._finalize_shutdown_process(process)
                return
        except Exception:
            pass
        if self._process is process:
            self._process = None
            self._retired_process = process
        self._retired_kill_sent = True
        self._start_retired_reap(process)

    @staticmethod
    def _wait_process(process: Any, timeout_ms: int) -> bool:
        try:
            if process.state() == QProcess.NotRunning:
                return True
            wait = getattr(process, "waitForFinished", None)
            if wait is not None:
                wait(timeout_ms)
            return process.state() == QProcess.NotRunning
        except Exception:
            return False

    def _finalize_shutdown_process(self, process: Any) -> None:
        if self._process is process:
            self._process = None
        if self._retired_process is process:
            self._finalize_retired_process(process)
        else:
            self._safe_delete_not_running(process)
        self._maybe_release_deferred_delete()

    def _maybe_release_deferred_delete(self) -> None:
        if (
            self._deferred_delete_requested
            and self._process is None
            and self._retired_process is None
            and not self._deferred_delete_release_scheduled
            and not self._allow_deferred_delete
        ):
            self._deferred_delete_release_scheduled = True
            QTimer.singleShot(0, self._release_deferred_delete)

    def _release_deferred_delete(self) -> None:
        self._deferred_delete_release_scheduled = False
        if (
            self._deferred_delete_requested
            and self._process is None
            and self._retired_process is None
            and not self._allow_deferred_delete
        ):
            self._allow_deferred_delete = True
            QCoreApplication.postEvent(self, QEvent(QEvent.DeferredDelete))

    def cancel(self) -> bool:
        if self._state not in {RunnerState.STARTING, RunnerState.RUNNING}:
            return False
        self._begin_abort(DiscoveryFailureCategory.CANCELLED, RunnerState.CANCELLED)
        return True

    def _set_state(self, state: RunnerState) -> None:
        self._state = state
        self.state_changed.emit(state)

    def _is_current(self, generation: int, process: Any) -> bool:
        return generation == self._generation and process is self._process and not self._completed

    def _on_started(self, generation: int, process: Any) -> None:
        if self._is_current(generation, process):
            self._set_state(RunnerState.RUNNING)

    def _read_output(self, generation: int, process: Any, stdout: bool) -> None:
        if not self._is_current(generation, process):
            return
        try:
            data = bytes(
                process.readAllStandardOutput() if stdout else process.readAllStandardError()
            )
        except Exception as exc:
            self._begin_abort(
                DiscoveryFailureCategory.INTERNAL_ERROR,
                RunnerState.FAILED,
            )
            if self._pending_category is None:
                self._emit_failure(
                    generation,
                    DiscoveryFailureCategory.INTERNAL_ERROR,
                    f"discovery output read failed: {type(exc).__name__}",
                    RunnerState.FAILED,
                )
            return
        target = self._stdout if stdout else self._stderr
        limit = self._stdout_limit if stdout else self._stderr_limit
        target.extend(data)
        if len(target) > limit and self._pending_category is None:
            category = (
                DiscoveryFailureCategory.STDOUT_TOO_LARGE
                if stdout
                else DiscoveryFailureCategory.STDERR_TOO_LARGE
            )
            self._begin_abort(category, RunnerState.FAILED)

    def _drain_output(self, process: Any) -> None:
        self._read_output(self._generation, process, True)
        self._read_output(self._generation, process, False)

    def _on_error(self, generation: int, process: Any, error: QProcess.ProcessError) -> None:
        if not self._is_current(generation, process):
            return
        if error == QProcess.FailedToStart:
            self._drain_output(process)
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.FAILED_TO_START,
                "discovery process failed to start",
                RunnerState.FAILED,
            )
        elif error == QProcess.Crashed and self._pending_category is None:
            self._drain_output(process)
            if not self._completed:
                category = self._pending_category or DiscoveryFailureCategory.CRASHED
                self._emit_failure(
                    generation,
                    category,
                    self._failure_message(category)
                    if self._pending_category is not None
                    else "discovery process crashed",
                    self._pending_state or RunnerState.FAILED,
                )
        elif self._pending_category is None:
            handled_errors = {
                QProcess.ReadError,
                QProcess.WriteError,
                QProcess.UnknownError,
            }
            timed_out = getattr(QProcess, "Timedout", None)
            if timed_out is not None:
                handled_errors.add(timed_out)
            if error in handled_errors:
                self._drain_output(process)
                if not self._completed and self._pending_category is None:
                    self._begin_abort(
                        DiscoveryFailureCategory.INTERNAL_ERROR,
                        RunnerState.FAILED,
                    )

    def _on_finished(
        self,
        generation: int,
        process: Any,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        if not self._is_current(generation, process):
            return
        self._drain_output(process)
        if self._completed:
            return
        if self._pending_category is not None:
            self._emit_failure(
                generation,
                self._pending_category,
                self._failure_message(self._pending_category),
                self._pending_state or RunnerState.FAILED,
                exit_code,
            )
            return
        if exit_status == QProcess.CrashExit:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.CRASHED,
                "discovery process crashed",
                RunnerState.FAILED,
                exit_code,
            )
            return
        if exit_code != 0:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.NONZERO_EXIT,
                "discovery process exited with a nonzero status",
                RunnerState.FAILED,
                exit_code,
            )
            return
        if not self._stdout:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.INVALID_OUTPUT,
                "discovery process produced empty stdout",
                RunnerState.FAILED,
                exit_code,
            )
            return
        try:
            nodes = parse_pw_dump(bytes(self._stdout))
            snapshot = PipeWireDiscoverySnapshot(
                profile_id=self._profile_id,
                generation=generation,
                captured_at=datetime.now(timezone.utc),
                nodes=nodes,
                discovery_state=(
                    DiscoveryState.AVAILABLE if nodes else DiscoveryState.EMPTY
                ),
            )
        except PipeWireDumpParseError as exc:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.PARSER_ERROR,
                str(exc),
                RunnerState.FAILED,
                exit_code,
            )
            return
        except Exception as exc:
            self._emit_failure(
                generation,
                DiscoveryFailureCategory.INTERNAL_ERROR,
                f"discovery parser failed: {type(exc).__name__}",
                RunnerState.FAILED,
                exit_code,
            )
            return
        self._emit_success(generation, PipeWireDumpResult(
            self._request_id, snapshot, bytes(self._stderr), exit_code
        ))

    def _on_timeout(self, generation: int, process: Any) -> None:
        if self._is_current(generation, process):
            self._begin_abort(DiscoveryFailureCategory.TIMEOUT, RunnerState.TIMED_OUT)

    def _begin_abort(
        self, category: DiscoveryFailureCategory, terminal_state: RunnerState
    ) -> None:
        if self._pending_category is not None or not self.active:
            return
        self._pending_category = category
        self._pending_state = terminal_state
        self._clear_timer("_timeout_timer")
        self._set_state(RunnerState.CANCELLING)
        process = self._process
        if process is None:
            self._emit_failure(
                self._generation, category, self._failure_message(category), terminal_state
            )
            return
        try:
            process.terminate()
            if self._completed:
                return
            if self._terminate_grace_ms == 0:
                process.kill()
                if not self._completed:
                    self._start_kill_watchdog()
            else:
                self._grace_timer = QTimer(self)
                self._grace_timer.setSingleShot(True)
                self._grace_timer.timeout.connect(self._force_kill)
                self._grace_timer.start(self._terminate_grace_ms)
        except Exception:
            self._emit_failure(
                self._generation,
                category,
                self._failure_message(category),
                terminal_state,
            )

    def _force_kill(self) -> None:
        process = self._process
        if process is not None and not self._completed:
            try:
                process.kill()
                if not self._completed:
                    self._start_kill_watchdog()
            except Exception:
                self._emit_failure(
                    self._generation,
                    self._pending_category or DiscoveryFailureCategory.INTERNAL_ERROR,
                    "discovery process could not be terminated",
                    self._pending_state or RunnerState.FAILED,
                )

    def _start_kill_watchdog(self) -> None:
        self._clear_timer("_kill_watchdog_timer")
        self._kill_watchdog_timer = QTimer(self)
        self._kill_watchdog_timer.setSingleShot(True)
        self._kill_watchdog_timer.timeout.connect(self._finish_after_kill_watchdog)
        self._kill_watchdog_timer.start(self._POST_KILL_WATCHDOG_MS)

    def _finish_after_kill_watchdog(self) -> None:
        if self._completed or self._pending_category is None:
            return
        self._emit_failure(
            self._generation,
            self._pending_category,
            self._failure_message(self._pending_category),
            self._pending_state or RunnerState.FAILED,
        )

    def _emit_success(self, generation: int, result: PipeWireDumpResult) -> None:
        if generation != self._generation or self._completed:
            return
        self._completed = True
        self._clear_timer("_timeout_timer")
        self._clear_timer("_grace_timer")
        self._clear_timer("_kill_watchdog_timer")
        self._result = result
        self._error = None
        self._set_state(RunnerState.SUCCEEDED)
        self._cleanup_process()
        self.succeeded.emit(result)
        self.finished.emit()

    def _emit_failure(
        self,
        generation: int,
        category: DiscoveryFailureCategory,
        message: str,
        terminal_state: RunnerState,
        exit_code: int | None = None,
    ) -> None:
        if generation != self._generation or self._completed:
            return
        self._completed = True
        self._clear_timer("_timeout_timer")
        self._clear_timer("_grace_timer")
        self._clear_timer("_kill_watchdog_timer")
        failure = PipeWireDiscoveryFailure(
            category, message, self._request_id, bytes(self._stderr), exit_code
        )
        self._result = None
        self._error = failure
        self._set_state(terminal_state)
        self._cleanup_process()
        self.failed.emit(failure)
        self.finished.emit()

    def _cleanup_process(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if self._retired_process is not None and self._retired_process is not process:
                try:
                    if self._retired_process.state() != QProcess.NotRunning:
                        self._safe_delete_not_running(process)
                        return
                except Exception:
                    self._safe_delete_not_running(process)
                    return
                self._finalize_retired_process(self._retired_process)
            self._retired_process = process
            self._retired_kill_sent = False
            try:
                process.destroyed.connect(
                    lambda _obj=None, retired=process: self._clear_retired(retired)
                )
            except (AttributeError, RuntimeError):
                pass
            self._start_retired_reap(process)

    def _start_retired_reap(self, process: Any) -> None:
        self._clear_timer("_retired_reap_timer")
        self._retired_reap_timer = QTimer(self)
        self._retired_reap_timer.setInterval(self._REAP_INTERVAL_MS)
        self._retired_reap_timer.timeout.connect(
            lambda retired=process: self._reap_retired_process(retired)
        )
        self._retired_reap_timer.start()
        self._reap_retired_process(process)

    def _reap_retired_process(self, process: Any) -> None:
        if self._retired_process is not process:
            self._clear_timer("_retired_reap_timer")
            return
        try:
            process_state = process.state()
        except (AttributeError, RuntimeError):
            process_state = QProcess.NotRunning
        if process_state == QProcess.NotRunning:
            self._finalize_retired_process(process)
            return
        if not self._retired_kill_sent:
            try:
                process.kill()
            except (AttributeError, RuntimeError):
                pass
            self._retired_kill_sent = True

    def _finalize_retired_process(self, process: Any) -> None:
        if self._retired_process is not process:
            return
        try:
            if process.state() != QProcess.NotRunning:
                return
        except (AttributeError, RuntimeError):
            pass
        self._clear_timer("_retired_reap_timer")
        self._safe_delete_not_running(process)
        self._retired_process = None
        self._retired_kill_sent = False
        self._maybe_release_deferred_delete()

    @staticmethod
    def _safe_delete_not_running(process: Any) -> None:
        try:
            if process.state() != QProcess.NotRunning:
                return
        except Exception:
            return
        try:
            process.setParent(None)
        except Exception:
            pass
        try:
            process.deleteLater()
        except Exception:
            pass

    def _clear_retired(self, process: Any) -> None:
        if self._retired_process is process:
            self._clear_timer("_retired_reap_timer")
            self._retired_process = None
            self._retired_kill_sent = False

    def _clear_timer(self, attribute: str) -> None:
        timer = getattr(self, attribute)
        setattr(self, attribute, None)
        self._stop_timer(timer)

    @staticmethod
    def _stop_timer(timer: QTimer | None) -> None:
        if timer is not None:
            try:
                timer.stop()
                timer.deleteLater()
            except RuntimeError:
                pass

    @staticmethod
    def _failure_message(category: DiscoveryFailureCategory) -> str:
        return {
            DiscoveryFailureCategory.TIMEOUT: "discovery process timed out",
            DiscoveryFailureCategory.CANCELLED: "discovery request was cancelled",
            DiscoveryFailureCategory.STDOUT_TOO_LARGE: "discovery stdout exceeded its limit",
            DiscoveryFailureCategory.STDERR_TOO_LARGE: "discovery stderr exceeded its limit",
        }.get(category, "discovery request failed")
