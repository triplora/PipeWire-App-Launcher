from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


DEFAULT_LOG_LIMIT = 512 * 1024


class ProcessState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    FAILED = "failed"

    @property
    def active(self) -> bool:
        return self in {self.STARTING, self.RUNNING, self.STOPPING}


class LimitedLog:
    """A byte-limited stream buffer that is safe for arbitrary process output."""

    def __init__(self, limit: int = DEFAULT_LOG_LIMIT):
        if limit <= 0:
            raise ValueError("log limit must be positive")
        self.limit = limit
        self._data = bytearray()

    def append(self, data: bytes | bytearray) -> None:
        self._data.extend(bytes(data))
        if len(self._data) > self.limit:
            del self._data[:-self.limit]

    def clear(self) -> None:
        self._data.clear()

    def text(self) -> str:
        return bytes(self._data).decode("utf-8", errors="replace")

    def __len__(self) -> int:
        return len(self._data)


class ProcessTerminator:
    """Safe QProcess-level termination; process-group signaling is not enabled."""

    group_support = False

    def graceful(self, process: Any) -> None:
        process.terminate()

    def force(self, process: Any) -> None:
        process.kill()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProcessExecution:
    profile_id: str
    process: Any
    generation: int
    state: ProcessState = ProcessState.STARTING
    pid: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    stdout_log: LimitedLog = field(default_factory=LimitedLog)
    stderr_log: LimitedLog = field(default_factory=LimitedLog)
    event_log: LimitedLog = field(default_factory=LimitedLog)
    error_message: str | None = None
    stop_requested: bool = False
    force_kill_requested: bool = False
    finished_signal_seen: bool = False
    final_message_emitted: bool = False

    @property
    def active(self) -> bool:
        return self.state.active


class ProcessRegistry:
    """Runtime-only process records, deliberately separate from profile storage."""

    def __init__(self, log_limit: int = DEFAULT_LOG_LIMIT):
        self.log_limit = log_limit
        self._records: dict[str, ProcessExecution] = {}
        self._next_generation = 0

    def get(self, profile_id: str) -> ProcessExecution | None:
        return self._records.get(profile_id)

    def records(self) -> tuple[ProcessExecution, ...]:
        return tuple(self._records.values())

    def active(self, profile_id: str) -> ProcessExecution | None:
        record = self.get(profile_id)
        return record if record and record.active else None

    def start(self, profile_id: str, process: Any) -> ProcessExecution:
        if self.active(profile_id):
            raise RuntimeError(f"profile {profile_id!r} already has an active process")
        self._next_generation += 1
        record = ProcessExecution(
            profile_id=profile_id,
            process=process,
            generation=self._next_generation,
            stdout_log=LimitedLog(self.log_limit),
            stderr_log=LimitedLog(self.log_limit),
            event_log=LimitedLog(self.log_limit),
        )
        self._records[profile_id] = record
        return record

    def is_current(self, profile_id: str, generation: int, process: Any) -> bool:
        record = self.get(profile_id)
        return bool(record and record.generation == generation and record.process is process)

    def set_running(self, profile_id: str, generation: int, process: Any, pid: int | None) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        if not record.active:
            return False
        record.pid = pid or None
        record.started_at = utc_now()
        record.state = ProcessState.RUNNING
        return True

    def append_output(self, profile_id: str, generation: int, process: Any, *, stdout: bytes = b"", stderr: bytes = b"") -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        record.stdout_log.append(stdout)
        record.stderr_log.append(stderr)
        return True

    def request_stop(self, profile_id: str, generation: int, process: Any) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        if not record.active or record.stop_requested:
            return False
        record.stop_requested = True
        record.state = ProcessState.STOPPING
        return True

    def request_force_kill(self, profile_id: str, generation: int, process: Any) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        if not record.active or record.force_kill_requested:
            return False
        record.force_kill_requested = True
        return True

    def fail(self, profile_id: str, generation: int, process: Any, message: str) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        if record.state in {ProcessState.EXITED, ProcessState.FAILED, ProcessState.STOPPED}:
            return False
        if record.stop_requested:
            record.error_message = None
            record.state = ProcessState.STOPPED if record.force_kill_requested else ProcessState.STOPPING
            if record.finished_at is None and record.state is ProcessState.STOPPED:
                record.finished_at = utc_now()
            return True
        record.error_message = message
        record.state = ProcessState.FAILED
        if record.finished_at is None:
            record.finished_at = utc_now()
        return True

    def note_error(self, profile_id: str, generation: int, process: Any, message: str) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        self._records[profile_id].error_message = message
        return True

    def finish(self, profile_id: str, generation: int, process: Any, exit_code: int | None) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        if record.state in {ProcessState.EXITED, ProcessState.STOPPED} or record.finished_signal_seen:
            return False
        record.finished_signal_seen = True
        record.exit_code = exit_code
        if record.finished_at is None:
            record.finished_at = utc_now()
        if record.stop_requested:
            record.error_message = None
            record.state = ProcessState.STOPPED
        elif record.state != ProcessState.FAILED:
            record.state = ProcessState.EXITED
        return True

    def append_event(self, profile_id: str, generation: int, process: Any, message: str) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        self._records[profile_id].event_log.append(message.encode("utf-8", errors="replace"))
        return True

    def emit_final_message(self, profile_id: str, generation: int, process: Any, message: str) -> bool:
        if not self.is_current(profile_id, generation, process):
            return False
        record = self._records[profile_id]
        if record.final_message_emitted:
            return False
        record.final_message_emitted = True
        record.event_log.append(message.encode("utf-8", errors="replace"))
        return True

    def clear_logs(self, profile_id: str) -> bool:
        record = self.get(profile_id)
        if not record:
            return False
        record.stdout_log.clear()
        record.stderr_log.clear()
        return True
