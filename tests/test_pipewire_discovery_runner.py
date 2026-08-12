import json
import sys
import unittest
import weakref
from unittest.mock import patch

from PySide6.QtCore import QEvent, QEventLoop, QProcess, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from pipewire_launcher.pipewire_discovery import (
    AssociationConfidence,
    DiscoveryState,
    PipeWireDiscoverySnapshot,
    PipeWireDumpParseError,
    PipeWireNode,
    PipeWirePort,
    PortDirection,
)
from pipewire_launcher.pipewire_discovery_runner import (
    DiscoveryFailureCategory,
    PipeWireDiscoveryFailure,
    PipeWireDiscoveryRunner,
    PipeWireDumpResult,
    RunnerState,
)


_TEST_RUNNERS = []


class FakeProcess(QObject):
    started = Signal()
    finished = Signal(int, object)
    errorOccurred = Signal(object)
    readyReadStandardOutput = Signal()
    readyReadStandardError = Signal()

    def __init__(
        self,
        parent=None,
        error=None,
        *,
        emit_finished_on_terminate=True,
        emit_finished_on_kill=True,
        terminate_sets_not_running=True,
        kill_sets_not_running=True,
    ):
        super().__init__(parent)
        self.error = error
        self.emit_finished_on_terminate = emit_finished_on_terminate
        self.terminate_sets_not_running = terminate_sets_not_running
        self.emit_finished_on_kill = emit_finished_on_kill
        self.kill_sets_not_running = kill_sets_not_running
        self.state_value = QProcess.Starting
        self.stdout = b""
        self.stderr = b""
        self.started_args = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.delete_later_calls = 0

    def setProcessChannelMode(self, _mode):
        pass

    def start(self, executable, arguments):
        self.started_args = (executable, arguments)
        self.state_value = QProcess.Starting
        if self.error is not None:
            self.state_value = QProcess.NotRunning
            self.errorOccurred.emit(self.error)

    def state(self):
        return self.state_value

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_sets_not_running:
            self.state_value = QProcess.NotRunning
        if self.emit_finished_on_terminate:
            self.finished.emit(0, QProcess.NormalExit)

    def kill(self):
        self.kill_calls += 1
        if self.kill_sets_not_running:
            self.state_value = QProcess.NotRunning
        if self.emit_finished_on_kill:
            self.finished.emit(-9, QProcess.CrashExit)

    def readAllStandardOutput(self):
        data, self.stdout = self.stdout, b""
        return data

    def readAllStandardError(self):
        data, self.stderr = self.stderr, b""
        return data

    def deleteLater(self):
        self.delete_later_calls += 1
        super().deleteLater()


def valid_payload():
    return json.dumps([{
        "id": 1,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {"node.name": "synthetic"}},
    }]).encode()


def python_runner(code, **kwargs):
    runner = PipeWireDiscoveryRunner(
        executable=sys.executable,
        arguments=("-c", code),
        **kwargs,
    )
    _TEST_RUNNERS.append(runner)
    return runner


def owned_process_factory(process):
    def factory(parent):
        process.setParent(parent)
        return process

    return factory


class RunnerTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_finished(self, runner, timeout=3000):
        loop = QEventLoop()
        runner.finished.connect(loop.quit)
        QTimer.singleShot(timeout, loop.quit)
        loop.exec()
        self.assertFalse(runner.active, "runner did not finish")

    def wait_for_state(self, runner, state, timeout=3000):
        if runner.state == state:
            return
        loop = QEventLoop()

        def on_state_changed(current):
            if current == state:
                loop.quit()

        runner.state_changed.connect(on_state_changed)
        QTimer.singleShot(timeout, loop.quit)
        loop.exec()
        runner.state_changed.disconnect(on_state_changed)
        self.assertEqual(runner.state, state, f"runner did not reach {state}")

    def test_defaults_are_production_command(self):
        runner = PipeWireDiscoveryRunner()
        self.assertEqual(runner._executable, "pw-dump")
        self.assertEqual(runner._arguments, ("-N",))

    def test_models_are_immutable(self):
        failure = PipeWireDiscoveryFailure(DiscoveryFailureCategory.CANCELLED, "x", None)
        self.assertEqual(failure.category.value, "cancelled")
        with self.assertRaises(Exception):
            failure.message = "changed"

        port = PipeWirePort(1, 2, "port")
        node = PipeWireNode(
            2,
            "node",
            ports=[port],
            association_basis=["pid"],
            association_confidence=AssociationConfidence.HIGH,
        )
        snapshot = PipeWireDiscoverySnapshot(
            "profile", 1, __import__("datetime").datetime.now(), nodes=[node]
        )
        for model, field in (
            (port, "name"),
            (node, "name"),
            (snapshot, "profile_id"),
        ):
            with self.assertRaises(Exception):
                setattr(model, field, "changed")
        self.assertEqual(port.direction, PortDirection.UNKNOWN)

    def test_configuration_validation(self):
        with self.assertRaises(ValueError): PipeWireDiscoveryRunner(executable="")
        with self.assertRaises(TypeError): PipeWireDiscoveryRunner(arguments="-N")
        for name in ("timeout_ms", "terminate_grace_ms", "stdout_limit_bytes", "stderr_limit_bytes"):
            with self.assertRaises(TypeError): PipeWireDiscoveryRunner(**{name: True})
            with self.assertRaises(ValueError): PipeWireDiscoveryRunner(**{name: -1})
        with self.assertRaises(ValueError): PipeWireDiscoveryRunner(stdout_limit_bytes=0)

    def test_success_with_valid_json_returns_full_snapshot(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start("profile")
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)
        self.assertIsInstance(runner.result, PipeWireDumpResult)
        self.assertEqual(runner.result.snapshot.discovery_state, DiscoveryState.AVAILABLE)
        self.assertEqual(runner.result.snapshot.profile_id, "profile")

    def test_stdout_can_arrive_in_fragments(self):
        payload = valid_payload()
        code = f"import sys; p={payload!r}; [sys.stdout.buffer.write(bytes([b])) or sys.stdout.flush() for b in p]"
        runner = python_runner(code)
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)

    def test_stderr_is_preserved_on_success(self):
        runner = python_runner(
            f"import sys; sys.stderr.buffer.write(b'diagnostic'); sys.stdout.buffer.write({valid_payload()!r})"
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.result.stderr, b"diagnostic")

    def test_success_emits_one_succeeded_and_one_finished(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        succeeded, failed, finished = [], [], []
        runner.succeeded.connect(succeeded.append)
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(failed), 0)
        self.assertEqual(len(finished), 1)

    def test_failure_preserves_stderr(self):
        runner = python_runner(
            "import sys; sys.stderr.buffer.write(b'failure-diagnostic'); raise SystemExit(4)"
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.NONZERO_EXIT)
        self.assertEqual(runner.error.stderr, b"failure-diagnostic")

    def test_executable_not_found(self):
        runner = PipeWireDiscoveryRunner(executable="definitely-not-a-real-pw-dump")
        failures = []
        runner.failed.connect(failures.append)
        runner.start()
        self.assertEqual(runner.state, RunnerState.FAILED)
        self.assertEqual(failures[0].category, DiscoveryFailureCategory.EXECUTABLE_NOT_FOUND)

    def test_directory_is_rejected_as_explicit_executable(self):
        runner = PipeWireDiscoveryRunner(executable="/")
        runner.start()
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.EXECUTABLE_NOT_FOUND)

    def test_relative_explicit_executable_uses_file_validation(self):
        process = FakeProcess()
        with patch("pipewire_launcher.pipewire_discovery_runner.os.path.isfile", return_value=True), \
             patch("pipewire_launcher.pipewire_discovery_runner.os.access", return_value=True):
            runner = PipeWireDiscoveryRunner(
                executable="relative/synthetic",
                process_factory=lambda parent: process,
            )
            runner.start()
        self.assertEqual(process.started_args[0], "relative/synthetic")
        runner.cancel()

    def test_broken_explicit_path_is_rejected(self):
        runner = PipeWireDiscoveryRunner(executable="broken/link")
        with patch("pipewire_launcher.pipewire_discovery_runner.os.path.isfile", return_value=False):
            runner.start()
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.EXECUTABLE_NOT_FOUND)

    def test_integer_request_id_is_preserved_and_stringified_in_snapshot(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start(42)
        self.wait_for_finished(runner)
        self.assertEqual(runner.result.request_id, 42)
        self.assertEqual(runner.result.snapshot.profile_id, "42")

    def test_request_id_string_failure_is_controlled(self):
        class BadString:
            def __str__(self):
                raise RuntimeError("cannot stringify")

        failures, finished = [], []
        runner = PipeWireDiscoveryRunner()
        runner.failed.connect(failures.append)
        runner.finished.connect(lambda: finished.append(True))
        request_id = BadString()
        runner.start(request_id)
        self.assertEqual(runner.state, RunnerState.FAILED)
        self.assertEqual(failures[0].category, DiscoveryFailureCategory.INTERNAL_ERROR)
        self.assertIs(failures[0].request_id, request_id)
        self.assertEqual(len(finished), 1)

    def test_failed_to_start(self):
        factory = lambda parent: FakeProcess(parent, QProcess.FailedToStart)
        runner = PipeWireDiscoveryRunner(executable=sys.executable, process_factory=factory)
        runner.start()
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.FAILED_TO_START)

    def test_read_write_and_unknown_errors_are_internal_errors(self):
        for process_error in (QProcess.ReadError, QProcess.WriteError, QProcess.UnknownError):
            runner = PipeWireDiscoveryRunner(
                executable=sys.executable,
                process_factory=lambda parent, error=process_error: FakeProcess(parent, error),
            )
            runner.start()
            self.assertEqual(runner.error.category, DiscoveryFailureCategory.INTERNAL_ERROR)

    def test_arguments_are_passed_without_a_shell(self):
        process = FakeProcess()
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            arguments=("-c", "synthetic; echo not-a-shell"),
            process_factory=owned_process_factory(process),
        )
        runner.start()
        self.assertEqual(
            process.started_args,
            (sys.executable, ["-c", "synthetic; echo not-a-shell"]),
        )
        runner.cancel()

    def test_nonzero_exit(self):
        runner = python_runner("raise SystemExit(7)")
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.NONZERO_EXIT)
        self.assertEqual(runner.error.exit_code, 7)

    def test_crash_is_classified(self):
        factory = lambda parent: FakeProcess(parent, QProcess.Crashed)
        runner = PipeWireDiscoveryRunner(executable=sys.executable, process_factory=factory)
        runner.start()
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.CRASHED)

    def test_timeout(self):
        runner = python_runner("import time; time.sleep(2)", timeout_ms=50)
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.TIMED_OUT)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.TIMEOUT)

    def test_cancel_during_starting(self):
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=lambda parent: FakeProcess(parent),
        )
        runner.start()
        self.assertTrue(runner.cancel())
        self.assertEqual(runner.state, RunnerState.CANCELLED)

    def test_cancel_during_running(self):
        runner = python_runner("import time; time.sleep(2)")
        runner.start()
        self.wait_for_state(runner, RunnerState.RUNNING)
        self.assertTrue(runner.cancel())
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.CANCELLED)

    def test_cancel_is_idempotent(self):
        runner = python_runner("import time; time.sleep(2)")
        runner.start()
        self.wait_for_state(runner, RunnerState.RUNNING)
        self.assertTrue(runner.cancel())
        self.assertFalse(runner.cancel())
        self.wait_for_finished(runner)

    def test_concurrent_request_is_rejected_without_interference(self):
        runner = python_runner("import time; time.sleep(1)")
        rejected = []
        runner.request_rejected.connect(rejected.append)
        self.assertTrue(runner.start("first"))
        self.wait_for_state(runner, RunnerState.RUNNING)
        self.assertFalse(runner.start("second"))
        self.assertEqual(rejected[0].category, DiscoveryFailureCategory.ALREADY_RUNNING)
        self.assertEqual(runner._request_id, "first")
        runner.cancel()
        self.wait_for_finished(runner)

    def test_stdout_exact_limit_is_allowed(self):
        payload = valid_payload()
        runner = python_runner(
            f"import sys; sys.stdout.buffer.write({payload!r})",
            stdout_limit_bytes=len(payload),
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)

    def test_stdout_one_byte_over_limit_fails(self):
        payload = valid_payload()
        runner = python_runner(
            f"import sys; sys.stdout.buffer.write({payload + b'x'!r})",
            stdout_limit_bytes=len(payload),
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.STDOUT_TOO_LARGE)

    def test_stderr_exact_limit_is_allowed(self):
        payload = valid_payload()
        runner = python_runner(
            f"import sys; sys.stderr.buffer.write(b'x'); sys.stdout.buffer.write({payload!r})",
            stderr_limit_bytes=1,
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)

    def test_stderr_one_byte_over_limit_fails(self):
        payload = valid_payload()
        runner = python_runner(
            f"import sys; sys.stderr.buffer.write(b'xx'); sys.stdout.buffer.write({payload!r})",
            stderr_limit_bytes=1,
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.STDERR_TOO_LARGE)

    def test_stdout_overflow_is_detected_during_final_drain(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
        )
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            stdout_limit_bytes=1,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        process.stdout = b"xx"
        runner._on_finished(1, process, 0, QProcess.NormalExit)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.STDOUT_TOO_LARGE)
        self.assertEqual(runner.state, RunnerState.FAILED)

    def test_empty_stdout_is_invalid_output(self):
        runner = python_runner("pass")
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.INVALID_OUTPUT)

    def test_invalid_json_is_parser_error(self):
        runner = python_runner("import sys; sys.stdout.write('{}')")
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.PARSER_ERROR)

    def test_unexpected_parser_exception_is_internal_error(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        with patch("pipewire_launcher.pipewire_discovery_runner.parse_pw_dump", side_effect=RuntimeError("boom")):
            runner.start()
            self.wait_for_finished(runner)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.INTERNAL_ERROR)

    def test_timeout_has_one_failure_and_one_finished(self):
        runner = python_runner("import time; time.sleep(2)", timeout_ms=30)
        failed, finished = [], []
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        self.wait_for_finished(runner)
        self.app.processEvents()
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(finished), 1)

    def test_timeout_escalates_from_terminate_to_kill(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
        )
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=20,
            terminate_grace_ms=10,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(runner.error.category, DiscoveryFailureCategory.TIMEOUT)

    def test_missing_finished_after_kill_uses_terminal_watchdog(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
        )
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=20,
            terminate_grace_ms=10,
            process_factory=owned_process_factory(process),
        )
        failed, finished = [], []
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.TIMED_OUT)
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(finished), 1)

    def test_late_finished_after_watchdog_does_not_duplicate(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
        )
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=20,
            terminate_grace_ms=10,
            process_factory=owned_process_factory(process),
        )
        failed, finished = [], []
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        self.wait_for_finished(runner)
        process.finished.emit(0, QProcess.NormalExit)
        self.app.processEvents()
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(finished), 1)

    def test_started_late_after_starting_cancel_does_not_change_state(self):
        process = FakeProcess()
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        failed, finished = [], []
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        runner.cancel()
        process.started.emit()
        self.assertEqual(runner.state, RunnerState.CANCELLED)
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(finished), 1)

    def test_new_execution_after_success(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start("one")
        self.wait_for_finished(runner)
        runner.start("two")
        self.wait_for_finished(runner)
        self.assertEqual(runner.result.request_id, "two")

    def test_new_execution_after_failure(self):
        runner = python_runner("raise SystemExit(3)")
        runner.start()
        self.wait_for_finished(runner)
        runner._arguments = ("-c", f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)

    def test_new_execution_after_timeout(self):
        runner = python_runner("import time; time.sleep(2)", timeout_ms=20)
        runner.start()
        self.wait_for_finished(runner)
        runner._arguments = ("-c", f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)

    def test_new_execution_after_cancelled(self):
        runner = python_runner("import time; time.sleep(2)")
        runner.start()
        self.wait_for_state(runner, RunnerState.RUNNING)
        runner.cancel()
        self.wait_for_finished(runner)
        runner._arguments = ("-c", f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start()
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.SUCCEEDED)

    def test_no_real_pipewire_command_is_used(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        self.assertNotEqual(runner._executable, "pw-dump")
        runner.start()
        self.wait_for_finished(runner)

    def test_completed_processes_are_not_accumulated(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        for _ in range(3):
            runner.start()
            self.wait_for_finished(runner)
            self.app.processEvents()
            self.assertIsNone(runner._retired_process)
        self.assertFalse(hasattr(runner, "_retired_processes"))

    def test_cleanup_leaves_no_active_process(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start()
        self.wait_for_finished(runner)
        self.app.processEvents()
        self.assertFalse(runner.active)
        self.assertIsNone(runner._process)
        self.assertIsNone(runner._retired_process)

    def test_watchdog_retains_active_process_and_terminal_result(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=owned_process_factory(process),
        )
        runner.start("first")
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        self.assertEqual(runner.state, RunnerState.TIMED_OUT)
        self.assertIs(runner._retired_process, process)
        self.assertIs(process.parent(), runner)

    def test_start_rejected_while_retired_process_active(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        created = []
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=lambda parent: (created.append(process) or process),
        )
        runner.start("first")
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        generation = runner._generation
        self.assertFalse(runner.start("second"))
        self.assertEqual(runner._generation, generation)
        self.assertEqual(len(created), 1)
        self.assertIs(runner._retired_process, process)

    def test_multiple_starts_are_rejected_during_reap(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        rejected = []
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=owned_process_factory(process),
        )
        runner.request_rejected.connect(rejected.append)
        runner.start("first")
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        self.assertFalse(runner.start("second"))
        self.assertFalse(runner.start("third"))
        self.assertEqual(len(rejected), 2)
        self.assertIs(runner._retired_process, process)

    def test_reap_cleans_when_retired_becomes_not_running(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        process.state_value = QProcess.NotRunning
        runner._reap_retired_process(process)
        self.assertIsNone(runner._retired_process)
        self.assertIsNone(runner._retired_reap_timer)
        self.assertEqual(process.delete_later_calls, 1)

    def test_only_one_reap_timer_and_retired_process_exist(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        first_timer = runner._retired_reap_timer
        runner._start_retired_reap(process)
        self.assertIsNotNone(runner._retired_reap_timer)
        self.assertIsNot(first_timer, runner._retired_reap_timer)
        self.assertIs(runner._retired_process, process)

    def test_active_retired_process_keeps_parent_during_reap(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        runner._reap_retired_process(process)
        self.assertIs(process.parent(), runner)
        self.assertEqual(process.delete_later_calls, 0)

    def test_reap_finished_callback_does_not_duplicate_terminal_signals(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        failed, finished = [], []
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=lambda parent: process,
        )
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        process.finished.emit(0, QProcess.NormalExit)
        self.app.processEvents()
        self.assertEqual(len(failed), 1)
        self.assertEqual(len(finished), 1)

    def test_synchronous_kill_finished_has_no_residual_reap_timer(self):
        process = FakeProcess(emit_finished_on_terminate=False, emit_finished_on_kill=True)
        process.state_value = QProcess.Running
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=lambda parent: process,
        )
        runner.start()
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        self.assertIsNone(runner._kill_watchdog_timer)
        self.assertIsNone(runner._retired_reap_timer)

    def test_runner_reusable_after_reap_complete(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        processes = [process]
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            timeout_ms=1,
            terminate_grace_ms=1,
            process_factory=lambda parent: (processes[-1].setParent(parent) or processes[-1]),
        )
        runner.start("first")
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        self.assertFalse(runner.start("blocked"))
        process.state_value = QProcess.NotRunning
        runner._reap_retired_process(process)
        replacement = FakeProcess()
        replacement.state_value = QProcess.NotRunning
        processes.append(replacement)
        self.assertTrue(runner.start("second"))
        self.assertIs(runner._process, replacement)

    def test_not_running_reap_removes_parent_and_requests_destruction(self):
        process = FakeProcess()
        runner = PipeWireDiscoveryRunner(executable=sys.executable)
        process.setParent(runner)
        process.state_value = QProcess.NotRunning
        runner._process = process
        runner._cleanup_process()
        self.assertIsNone(process.parent())
        self.assertEqual(process.delete_later_calls, 1)
        self.app.processEvents()
        self.assertIsNone(runner._retired_process)

    def test_shutdown_without_process_is_idempotent_and_permanent(self):
        runner = PipeWireDiscoveryRunner(executable=sys.executable)
        self.assertTrue(runner.shutdown())
        self.assertFalse(runner.shutdown())
        self.assertFalse(runner.start())
        self.assertIsNone(runner._timeout_timer)
        self.assertIsNone(runner._grace_timer)
        self.assertIsNone(runner._kill_watchdog_timer)
        self.assertIsNone(runner._retired_reap_timer)

    def test_shutdown_active_process_terminates_and_cleans_once(self):
        process = FakeProcess()
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        self.assertTrue(runner.shutdown())
        self.assertFalse(runner.shutdown())
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertEqual(process.delete_later_calls, 1)
        self.assertIsNone(runner._process)
        self.assertIsNone(runner._retired_process)
        self.assertFalse(runner.start())

    def test_shutdown_kills_when_terminate_is_ignored(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            terminate_sets_not_running=False,
        )
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        self.assertTrue(runner.shutdown())
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertIsNone(runner._process)
        self.assertIsNone(runner._retired_process)

    def test_shutdown_retired_running_process_preserves_identity_until_reap(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process.state_value = QProcess.Running
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        runner._on_timeout(runner._generation, process)
        self.wait_for_finished(runner)
        failed = []
        runner.failed.connect(failed.append)
        self.assertTrue(runner.shutdown())
        self.assertIs(runner._retired_process, process)
        self.assertIs(process.parent(), runner)
        self.assertEqual(len(failed), 0)
        self.assertFalse(runner.start())
        process.state_value = QProcess.NotRunning
        runner._reap_retired_process(process)
        self.assertIsNone(runner._retired_process)
        self.assertEqual(process.delete_later_calls, 1)

    def test_shutdown_callbacks_are_silent_and_generation_stable(self):
        process = FakeProcess()
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        failed, finished = [], []
        runner.failed.connect(failed.append)
        runner.finished.connect(lambda: finished.append(True))
        runner.start("one")
        generation = runner._generation
        runner.shutdown()
        process.finished.emit(0, QProcess.NormalExit)
        process.errorOccurred.emit(QProcess.UnknownError)
        self.app.processEvents()
        self.assertEqual(runner._generation, generation)
        self.assertEqual(failed, [])
        self.assertEqual(finished, [])

    def test_shutdown_with_synchronous_terminate_has_no_residual_timers(self):
        process = FakeProcess()
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        runner.start()
        runner.shutdown()
        self.assertIsNone(runner._timeout_timer)
        self.assertIsNone(runner._grace_timer)
        self.assertIsNone(runner._kill_watchdog_timer)
        self.assertIsNone(runner._retired_reap_timer)

    def test_deferred_delete_during_active_process_is_intercepted(self):
        process = FakeProcess(
            emit_finished_on_terminate=False,
            emit_finished_on_kill=False,
            terminate_sets_not_running=False,
            kill_sets_not_running=False,
        )
        process_destroyed = []
        runner_destroyed = []
        failed = []
        finished = []
        runner = PipeWireDiscoveryRunner(
            executable=sys.executable,
            process_factory=owned_process_factory(process),
        )
        process.destroyed.connect(lambda: process_destroyed.append(True))
        runner.destroyed.connect(lambda: runner_destroyed.append(True))
        runner.failed.connect(lambda failure: failed.append(failure))
        runner.finished.connect(lambda: finished.append(True))
        runner.start()
        process.state_value = QProcess.Running
        generation = runner._generation
        runner.deleteLater()
        before_reap = []
        def inspect_before_reap():
            before_reap.append(
                (
                    runner._retired_process is process,
                    process.parent() is runner,
                    runner._deferred_delete_requested,
                    not runner._allow_deferred_delete,
                    not runner._deferred_delete_release_scheduled,
                    process.delete_later_calls == 0,
                    runner._generation == generation,
                    not runner.start(),
                    not failed,
                    not finished,
                )
            )

        loop = QEventLoop()
        timeout = []
        runner.destroyed.connect(loop.quit)
        QTimer.singleShot(10, inspect_before_reap)
        def finish_process_and_reap():
            process.state_value = QProcess.NotRunning
            runner._reap_retired_process(process)

        QTimer.singleShot(50, finish_process_and_reap)
        QTimer.singleShot(500, lambda: (timeout.append(True), loop.quit()))
        loop.exec()

        self.assertEqual(
            before_reap,
            [(True, True, True, True, True, True, True, True, True, True)],
        )
        self.assertEqual(timeout, [])
        self.assertEqual(process_destroyed, [True])
        self.assertEqual(runner_destroyed, [True])
        self.assertEqual(failed, [])
        self.assertEqual(finished, [])

    def test_shutdown_after_terminal_states_is_idempotent(self):
        runner = python_runner(f"import sys; sys.stdout.buffer.write({valid_payload()!r})")
        runner.start()
        self.wait_for_finished(runner)
        self.assertTrue(runner.shutdown())
        self.assertFalse(runner.shutdown())
        self.assertFalse(runner.start())

    def test_shutdown_real_qprocess_cycle_has_no_active_child(self):
        runner = python_runner("import time; time.sleep(2)")
        runner.start()
        runner.shutdown()
        self.app.processEvents()
        self.assertIsNone(runner._process)
        self.assertIsNone(runner._retired_process)
        self.assertEqual(len(runner.findChildren(QProcess)), 0)

    def test_close_alias_uses_permanent_shutdown(self):
        runner = PipeWireDiscoveryRunner(executable=sys.executable)
        self.assertTrue(runner.close())
        self.assertFalse(runner.start())

    def test_qprocess_real_active_runner_deletion_is_safe(self):
        runner = python_runner("import time; time.sleep(2)")
        runner.start()
        process = runner._process
        destroyed, finished = [], []
        process.destroyed.connect(lambda: destroyed.append(True))
        process.finished.connect(lambda *_args: finished.append(True))
        runner.deleteLater()
        loop = QEventLoop()
        QTimer.singleShot(500, loop.quit)
        process.destroyed.connect(loop.quit)
        loop.exec()
        self.assertEqual(len(destroyed), 1)
        self.assertGreaterEqual(len(finished), 1)


if __name__ == "__main__":
    unittest.main()
