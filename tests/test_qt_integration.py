import enum
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

import pipewire_launcher.__main__ as launcher
from pipewire_launcher.application_detection import ApplicationCandidate
from pipewire_launcher.core import Profile
from pipewire_launcher.pipewire_discovery import (
    DiscoveryState,
    PipeWireDiscoverySnapshot,
    PipeWireNode,
    PipeWirePort,
    PortDirection,
)
from pipewire_launcher.pipewire_discovery_runner import (
    DiscoveryFailureCategory,
    PipeWireDiscoveryFailure,
    PipeWireDumpResult,
    RunnerState,
)
from pipewire_launcher.process_supervision import ProcessState


class Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self._slots):
            slot(*args)


class FakeAudioStackController:
    def __init__(self, state):
        self.state_changed = Signal()
        self.operation_failed = Signal()
        self.state = state
        self.snapshot = SimpleNamespace(
            pipewire=state in {
                launcher.AudioStackState.PIPEWIRE_ONLY,
                launcher.AudioStackState.RUNNING,
            },
            qpwgraph=state == launcher.AudioStackState.RUNNING,
        )
        self.busy = False
        self.trigger_calls = 0
        self.shutdown_calls = 0

    def trigger(self):
        self.trigger_calls += 1
        return True

    def shutdown(self):
        self.shutdown_calls += 1


class FakeProcess:
    SeparateChannels = object()

    class ProcessError(enum.Enum):
        FailedToStart = 0
        Crashed = 1
        Timedout = 2
        ReadError = 3
        WriteError = 4
        UnknownError = 5

    FailedToStart = ProcessError.FailedToStart
    Crashed = ProcessError.Crashed
    Timedout = ProcessError.Timedout
    ReadError = ProcessError.ReadError
    WriteError = ProcessError.WriteError
    UnknownError = ProcessError.UnknownError

    def __init__(self, _parent=None):
        self.started = Signal()
        self.finished = Signal()
        self.errorOccurred = Signal()
        self.readyReadStandardOutput = Signal()
        self.readyReadStandardError = Signal()
        self.stdout = b""
        self.stderr = b""
        self.pid = 0
        self.start_args = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.deleted = False

    def setProcessEnvironment(self, _environment):
        pass

    def setWorkingDirectory(self, _directory):
        pass

    def setProcessChannelMode(self, _mode):
        pass

    def start(self, program, args):
        self.start_args = (program, args)

    def processId(self):
        return self.pid

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def errorString(self):
        return "fake process error"

    def readAllStandardOutput(self):
        data, self.stdout = self.stdout, b""
        return data

    def readAllStandardError(self):
        data, self.stderr = self.stderr, b""
        return data


class FakeTimer:
    def __init__(self, _parent=None):
        self.timeout = Signal()
        self.single_shot = False
        self.interval = None
        self.stop_calls = 0
        self.deleted = False

    def setSingleShot(self, value):
        self.single_shot = value

    def start(self, interval):
        self.interval = interval

    def stop(self):
        self.stop_calls += 1

    def deleteLater(self):
        self.deleted = True


class FakeStore:
    def __init__(self):
        self.profiles = [
            Profile("One", "one", id="profile-one"),
            Profile("Two", "two", id="profile-two"),
        ]

    def load(self):
        return list(self.profiles)

    def save(self, profiles):
        self.profiles = list(profiles)


class FakeMessageBox:
    Yes = QMessageBox.Yes
    No = QMessageBox.No
    answer = No

    @classmethod
    def question(cls, *_args, **_kwargs):
        return cls.answer

    @staticmethod
    def warning(*_args, **_kwargs):
        pass

    @staticmethod
    def critical(*_args, **_kwargs):
        pass

    @staticmethod
    def information(*_args, **_kwargs):
        pass


class FakeCloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class FakeDiscoveryRunner:
    instances = []

    def __init__(self, _parent=None):
        self.state_changed = Signal()
        self.succeeded = Signal()
        self.failed = Signal()
        self.request_rejected = Signal()
        self.finished = Signal()
        self.state = RunnerState.IDLE
        self.start_calls = []
        self.cancel_calls = 0
        self.shutdown_calls = 0
        self.result = None
        self.error = None
        type(self).instances.append(self)

    def start(self, request_id=None):
        self.start_calls.append(request_id)
        if self.state.active:
            self.request_rejected.emit(PipeWireDiscoveryFailure(DiscoveryFailureCategory.ALREADY_RUNNING, "busy", request_id))
            return False
        self.state = RunnerState.STARTING
        self.state_changed.emit(self.state)
        return True

    def cancel(self):
        if self.state not in {RunnerState.STARTING, RunnerState.RUNNING}:
            return False
        self.cancel_calls += 1
        self.state = RunnerState.CANCELLING
        self.state_changed.emit(self.state)
        return True

    def shutdown(self):
        self.shutdown_calls += 1
        return self.shutdown_calls == 1

    def started(self):
        self.state = RunnerState.RUNNING
        self.state_changed.emit(self.state)

    def succeed(self, result):
        self.result = result
        self.state = RunnerState.SUCCEEDED
        self.state_changed.emit(self.state)
        self.succeeded.emit(result)
        self.finished.emit()

    def fail(self, request_id, category=DiscoveryFailureCategory.INVALID_OUTPUT, message="safe failure", stderr=b"raw stderr"):
        self.error = PipeWireDiscoveryFailure(category, message, request_id, stderr)
        self.state = RunnerState.TIMED_OUT if category is DiscoveryFailureCategory.TIMEOUT else (
            RunnerState.CANCELLED if category is DiscoveryFailureCategory.CANCELLED else RunnerState.FAILED
        )
        self.state_changed.emit(self.state)
        self.failed.emit(self.error)
        self.finished.emit()

    def complete_cancel(self, request_id):
        self.fail(request_id, DiscoveryFailureCategory.CANCELLED, "cancelled", b"raw stderr")

    def emit_late_state(self, state):
        self.state_changed.emit(state)

    def reject(self, request_id, message="discovery request already running"):
        self.request_rejected.emit(PipeWireDiscoveryFailure(DiscoveryFailureCategory.ALREADY_RUNNING, message, request_id, b"raw stderr"))


class MainWindowQtTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patches = [
            patch.object(launcher, "ProfileStore", FakeStore),
            patch.object(launcher, "PipeWireDiscoveryRunner", FakeDiscoveryRunner),
            patch.object(launcher, "QProcess", FakeProcess),
            patch.object(launcher, "QTimer", FakeTimer),
            patch.object(launcher, "QMessageBox", FakeMessageBox),
            patch.object(launcher, "validate_profile", lambda _profile: []),
        ]
        for item in self.patches:
            item.start()
        self.window = launcher.MainWindow()
        self.discovery = self.window.discovery_runner
        self.processes = []

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        FakeDiscoveryRunner.instances.clear()

    def run_selected(self):
        self.window.run_current()
        record = self.window.registry.get(self.window.current_id)
        self.processes.append(record.process)
        return record

    def select_row(self, row):
        self.window.list.setCurrentRow(row)
        self.app.processEvents()

    def test_import_and_headless_construction_selects_profile(self):
        self.assertEqual(self.window.current_id, "profile-one")
        self.assertEqual(self.window.selected_profile().name, "One")
        self.assertIn("State: stopped", self.window.process_info.text())

    def test_audio_stack_button_maps_states_to_text_and_light(self):
        expected = {
            launcher.AudioStackState.RUNNING: ("Stop", True),
            launcher.AudioStackState.PIPEWIRE_ONLY: ("Restart", True),
            launcher.AudioStackState.STOPPED: ("Start", True),
            launcher.AudioStackState.ORPHANED_QPWGRAPH: ("Start", True),
            launcher.AudioStackState.STARTING: ("Starting…", False),
        }
        for state, (text, enabled) in expected.items():
            with self.subTest(state=state):
                self.window._audio_stack_state_changed(state)
                self.assertEqual(self.window.audio_stack_button.text(), text)
                self.assertEqual(self.window.audio_stack_button.isEnabled(), enabled)
                self.assertFalse(self.window.audio_stack_button.icon().isNull())

    def test_running_stack_requires_confirmation_before_stop(self):
        controller = FakeAudioStackController(launcher.AudioStackState.RUNNING)
        window = launcher.MainWindow(controller)
        window._audio_stack_state_changed(controller.state)
        FakeMessageBox.answer = QMessageBox.No
        window.audio_stack_button.click()
        self.assertEqual(controller.trigger_calls, 0)
        FakeMessageBox.answer = QMessageBox.Yes
        window.audio_stack_button.click()
        self.assertEqual(controller.trigger_calls, 1)
        window.close()
        window.deleteLater()
        FakeMessageBox.answer = QMessageBox.No

    def test_detection_dialog_selects_nothing_by_default(self):
        candidate = ApplicationCandidate(
            desktop_id="ardour.desktop",
            name="Ardour",
            executable="/usr/bin/ardour",
        )
        with patch.object(
            launcher.QDialog,
            "exec",
            return_value=launcher.QDialog.Accepted,
        ):
            selected = launcher.select_application_candidates(
                self.window,
                (candidate,),
            )
        self.assertEqual(selected, ())

    def test_detect_apps_adds_only_explicitly_selected_candidate(self):
        candidate = ApplicationCandidate(
            desktop_id="audacity.desktop",
            name="Audacity",
            executable="/usr/bin/audacity",
            environment=(("GDK_BACKEND", "x11"),),
        )
        with (
            patch.object(
                launcher,
                "detect_jack_applications",
                return_value=(candidate,),
            ),
            patch.object(
                launcher,
                "select_application_candidates",
                return_value=(candidate,),
            ),
        ):
            added = self.window.detect_applications()

        self.assertEqual(added, 1)
        self.assertEqual(len(self.window.profiles), 3)
        self.assertEqual(self.window.selected_profile().name, "Audacity")
        self.assertEqual(
            self.window.selected_profile().environment,
            {"GDK_BACKEND": "x11"},
        )
        self.assertEqual(len(self.window.store.profiles), 3)

    def test_run_started_pid_and_duplicate_guard(self):
        record = self.run_selected()
        self.assertEqual(record.state, ProcessState.STARTING)
        self.window.run_current()
        self.assertIs(self.window.registry.get("profile-one"), record)
        self.assertEqual(record.process.start_args[0], "pw-jack")
        record.process.pid = 1234
        record.process.started.emit()
        self.assertEqual(record.state, ProcessState.RUNNING)
        self.assertEqual(record.pid, 1234)
        self.assertIsNotNone(record.started_at)

    def test_two_profiles_and_selection_show_independent_logs(self):
        first = self.run_selected()
        first.process.stdout = b"first stdout"
        first.process.stderr = b"first stderr"
        first.process.readyReadStandardOutput.emit()
        first.process.readyReadStandardError.emit()
        self.select_row(1)
        second = self.run_selected()
        second.process.stdout = b"second stdout"
        second.process.readyReadStandardOutput.emit()
        self.assertTrue(self.window.registry.get("profile-one").stdout_log.text().endswith("first stdout"))
        self.assertEqual(self.window.registry.get("profile-one").stderr_log.text(), "first stderr")
        self.select_row(0)
        self.assertIn("first stdout", self.window.log.toPlainText())
        self.assertNotIn("second stdout", self.window.log.toPlainText())
        self.select_row(1)
        self.assertIn("second stdout", self.window.log.toPlainText())

    def test_clear_log_only_clears_selected_profile(self):
        first = self.run_selected()
        first.process.stdout = b"keep one"
        first.process.readyReadStandardOutput.emit()
        self.select_row(1)
        second = self.run_selected()
        second.process.stdout = b"clear two"
        second.process.readyReadStandardOutput.emit()
        self.window.clear_current_log()
        self.assertEqual(second.stdout_log.text(), "")
        self.assertTrue(first.stdout_log.text().endswith("keep one"))

    def test_finished_is_idempotent_and_records_exit_code(self):
        record = self.run_selected()
        record.process.finished.emit(7, None)
        finished_at = record.finished_at
        record.process.finished.emit(9, None)
        self.assertEqual(record.state, ProcessState.EXITED)
        self.assertEqual(record.exit_code, 7)
        self.assertEqual(record.finished_at, finished_at)
        self.assertEqual(record.event_log.text().count("[finished:"), 1)

    def test_failed_to_start_is_terminal_and_crash_complements_exit_code(self):
        failed = self.run_selected()
        failed.process.errorOccurred.emit(FakeProcess.FailedToStart)
        self.assertEqual(failed.state, ProcessState.FAILED)
        self.assertIsNotNone(failed.finished_at)
        self.assertEqual(failed.event_log.text().count("[finished:"), 1)

        self.select_row(1)
        crashed = self.run_selected()
        crashed.process.errorOccurred.emit(FakeProcess.Crashed)
        crashed.process.finished.emit(139, None)
        self.assertEqual(crashed.state, ProcessState.FAILED)
        self.assertEqual(crashed.exit_code, 139)
        self.assertEqual(crashed.event_log.text().count("[finished:"), 1)

    def test_non_terminal_error_does_not_end_active_process(self):
        record = self.run_selected()
        record.process.started.emit()
        record.process.errorOccurred.emit(FakeProcess.ReadError)
        self.assertEqual(record.state, ProcessState.RUNNING)
        self.assertIsNotNone(record.error_message)
        self.assertEqual(record.event_log.text().count("[process error:"), 1)

    def test_stop_is_selected_target_and_kill_is_single_after_timeout(self):
        first = self.run_selected()
        first.process.started.emit()
        self.select_row(1)
        second = self.run_selected()
        second.process.started.emit()
        self.window.stop_current()
        self.assertEqual(first.process.terminate_calls, 0)
        self.assertEqual(second.process.terminate_calls, 1)
        self.window.stop_current()
        self.assertEqual(second.process.terminate_calls, 1)
        timer = self.window.stop_timers[("profile-two", second.generation)]
        timer.timeout.emit()
        timer.timeout.emit()
        self.assertEqual(second.process.kill_calls, 1)

    def test_requested_stop_crash_exit_is_stopped_without_error(self):
        record = self.run_selected()
        record.process.started.emit()
        self.window.stop_current()
        record.process.errorOccurred.emit(FakeProcess.Crashed)
        self.assertEqual(record.state, ProcessState.STOPPING)
        self.assertNotIn("Process crashed", self.window.process_info.text())
        record.process.finished.emit(15, None)
        self.assertEqual(record.state, ProcessState.STOPPED)
        self.assertIn("State: stopped", self.window.process_info.text())
        self.assertNotIn("Error: Process crashed", self.window.process_info.text())
        self.assertEqual(record.event_log.text().count("[finished: stopped"), 1)

    def test_requested_stop_force_kill_finishes_stopped(self):
        record = self.run_selected()
        record.process.started.emit()
        self.window.stop_current()
        timer = self.window.stop_timers[("profile-one", record.generation)]
        timer.timeout.emit()
        record.process.errorOccurred.emit(FakeProcess.Crashed)
        record.process.finished.emit(9, None)
        self.assertEqual(record.state, ProcessState.STOPPED)
        self.assertEqual(record.process.kill_calls, 1)
        self.assertNotIn("Process crashed", self.window.process_info.text())

    def test_spontaneous_crash_and_external_sigterm_remain_failed(self):
        crashed = self.run_selected()
        crashed.process.started.emit()
        crashed.process.errorOccurred.emit(FakeProcess.Crashed)
        crashed.process.finished.emit(15, None)
        self.assertEqual(crashed.state, ProcessState.FAILED)
        self.assertIn("State: failed", self.window.process_info.text())

        self.select_row(1)
        external = self.run_selected()
        external.process.started.emit()
        external.process.errorOccurred.emit(FakeProcess.Crashed)
        external.process.finished.emit(15, None)
        self.assertEqual(external.state, ProcessState.FAILED)

    def test_stop_intent_is_reset_for_new_execution_and_stale_callback_is_ignored(self):
        old = self.run_selected()
        old.process.started.emit()
        self.window.stop_current()
        old.process.finished.emit(15, None)
        self.assertEqual(old.state, ProcessState.STOPPED)
        self.window.run_current()
        new = self.window.registry.get("profile-one")
        self.assertIsNot(new, old)
        self.assertFalse(new.stop_requested)
        old.process.errorOccurred.emit(FakeProcess.Crashed)
        self.assertEqual(new.state, ProcessState.STARTING)
        new.process.started.emit()
        self.assertEqual(new.state, ProcessState.RUNNING)

    def test_finished_cleans_stop_timer_and_old_timer_cannot_kill_new_run(self):
        old = self.run_selected()
        old.process.started.emit()
        self.window.stop_current()
        old_timer = self.window.stop_timers[("profile-one", old.generation)]
        old.process.finished.emit(0, None)
        self.assertNotIn(("profile-one", old.generation), self.window.stop_timers)
        self.window.run_current()
        new = self.window.registry.get("profile-one")
        self.assertGreater(new.generation, old.generation)
        old_timer.timeout.emit()
        self.assertEqual(old.process.kill_calls, 0)
        self.assertEqual(new.process.kill_calls, 0)

    def test_close_cancel_does_not_touch_processes(self):
        record = self.run_selected()
        FakeMessageBox.answer = FakeMessageBox.No
        event = FakeCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.ignored)
        self.assertEqual(record.process.terminate_calls, 0)
        FakeMessageBox.answer = FakeMessageBox.Yes

    def test_close_confirmed_stops_all_and_final_deadline_closes_once(self):
        first = self.run_selected()
        self.select_row(1)
        second = self.run_selected()
        FakeMessageBox.answer = FakeMessageBox.Yes
        event = FakeCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.ignored)
        self.assertEqual(first.process.terminate_calls, 1)
        self.assertEqual(second.process.terminate_calls, 1)

        self.window.close_deadline = 0
        self.window._finish_close()
        self.assertEqual(first.process.kill_calls, 1)
        self.assertEqual(second.process.kill_calls, 1)
        self.assertIsNotNone(self.window.close_force_deadline)
        self.window.close_force_deadline = 0
        self.window.close = lambda: setattr(self, "closed", True)
        self.window._finish_close()
        self.window._finish_close()
        self.assertTrue(self.closed)
        self.assertEqual(first.process.kill_calls, 1)
        self.assertEqual(second.process.kill_calls, 1)
        self.assertFalse(first.active)
        self.assertFalse(second.active)

    def test_close_without_active_process_accepts(self):
        event = FakeCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.accepted)
        self.assertEqual(self.discovery.shutdown_calls, 1)

    def test_discovery_section_starts_idle_and_has_columns(self):
        self.assertEqual(self.window.discovery_state.text(), "Not queried")
        self.assertEqual([self.window.discovery_tree.headerItem().text(i) for i in range(6)], ["Name", "Type", "Application", "PID", "Media class", "ID"])
        self.assertTrue(self.window.discovery_refresh_button.isEnabled())
        self.assertFalse(self.window.discovery_cancel_button.isEnabled())

    def test_refresh_uses_selected_profile_id_and_selection_does_not_start(self):
        self.select_row(1)
        self.assertEqual(self.discovery.start_calls, [])
        self.window.refresh_discovery()
        self.assertEqual(self.discovery.start_calls, ["profile-two"])

    def test_refresh_without_profile_does_not_start(self):
        self.window.current_id = None
        self.window._update_discovery_controls()
        self.window.refresh_discovery()
        self.assertEqual(self.discovery.start_calls, [])

    def test_simultaneous_refresh_is_blocked_and_active_states_update_controls(self):
        self.window.refresh_discovery()
        self.assertEqual(self.discovery.start_calls, ["profile-one"])
        self.assertFalse(self.window.discovery_refresh_button.isEnabled())
        self.assertTrue(self.window.discovery_cancel_button.isEnabled())
        self.discovery.started()
        self.window.refresh_discovery()
        self.assertEqual(self.discovery.start_calls, ["profile-one"])
        self.assertEqual(self.window.discovery_state.text(), "Discovering PipeWire nodes…")

    def test_success_renders_node_and_ports(self):
        self.window.refresh_discovery()
        node = PipeWireNode(10, "node", application_name="app", process_id=42, media_class="Audio/Source", ports=(PipeWirePort(11, 10, "in", direction=PortDirection.INPUT),))
        snapshot = PipeWireDiscoverySnapshot("profile-one", 1, datetime.now(timezone.utc), (node,), DiscoveryState.AVAILABLE)
        self.discovery.succeed(PipeWireDumpResult("profile-one", snapshot, b"", 0))
        self.assertEqual(self.window.discovery_state.text(), "1 nodes discovered")
        self.assertEqual(self.window.discovery_tree.topLevelItemCount(), 1)
        self.assertEqual(self.window.discovery_tree.topLevelItem(0).child(0).text(1), "Input")

    def test_empty_success_shows_empty_state(self):
        self.window.refresh_discovery()
        snapshot = PipeWireDiscoverySnapshot("profile-one", 1, datetime.now(timezone.utc), (), DiscoveryState.EMPTY)
        self.discovery.succeed(PipeWireDumpResult("profile-one", snapshot, b"", 0))
        self.assertEqual(self.window.discovery_state.text(), "No PipeWire nodes found")

    def test_late_result_for_other_profile_is_ignored(self):
        self.window.refresh_discovery()
        node = PipeWireNode(10, "kept")
        snapshot = PipeWireDiscoverySnapshot("profile-one", 1, datetime.now(timezone.utc), (node,), DiscoveryState.AVAILABLE)
        self.discovery.succeed(PipeWireDumpResult("profile-one", snapshot, b"", 0))
        self.select_row(1)
        self.discovery.succeed(PipeWireDumpResult("profile-one", snapshot, b"", 0))
        self.assertEqual(self.window.discovery_tree.topLevelItem(0).text(0), "kept")
        self.assertNotEqual(self.window.discovery_state.text(), "2 nodes discovered")

    def test_failure_preserves_tree_and_hides_diagnostics(self):
        self.window.refresh_discovery()
        snapshot = PipeWireDiscoverySnapshot("profile-one", 1, datetime.now(timezone.utc), (PipeWireNode(1, "kept"),), DiscoveryState.AVAILABLE)
        self.discovery.succeed(PipeWireDumpResult("profile-one", snapshot, b"", 0))
        self.window.refresh_discovery()
        self.discovery.fail("profile-one")
        self.assertEqual(self.window.discovery_tree.topLevelItemCount(), 1)
        self.assertEqual(self.window.discovery_state.text(), "Discovery unavailable — invalid_output: safe failure")
        self.assertNotIn("raw stderr", self.window.discovery_state.text())

    def test_cancel_is_idempotent(self):
        self.window.refresh_discovery()
        self.assertTrue(self.window.cancel_discovery())
        self.assertFalse(self.window.cancel_discovery())
        self.assertEqual(self.discovery.cancel_calls, 1)
        self.discovery.complete_cancel("profile-one")
        self.assertEqual(self.window.discovery_state.text(), "Discovery cancelled")

    def test_old_profile_terminal_states_do_not_change_selected_profile_ui(self):
        self.window.refresh_discovery()
        self.select_row(1)
        for state, category in (
            (RunnerState.TIMED_OUT, DiscoveryFailureCategory.TIMEOUT),
            (RunnerState.CANCELLED, DiscoveryFailureCategory.CANCELLED),
            (RunnerState.FAILED, DiscoveryFailureCategory.INVALID_OUTPUT),
        ):
            before_label = self.window.discovery_state.text()
            before_items = self.window.discovery_tree.topLevelItemCount()
            self.discovery.emit_late_state(state)
            self.discovery.fail("profile-one", category, "old profile failure", b"old stderr")
            self.assertEqual(self.window.discovery_state.text(), before_label)
            self.assertEqual(self.window.discovery_tree.topLevelItemCount(), before_items)

    def test_late_result_cannot_replace_snapshot_for_new_profile(self):
        self.window.current_id = "profile-two"
        self.window._discovery_request_id = "profile-two"
        kept = PipeWireDiscoverySnapshot("profile-two", 2, datetime.now(timezone.utc), (PipeWireNode(20, "B node"),), DiscoveryState.AVAILABLE)
        self.discovery.succeed(PipeWireDumpResult("profile-two", kept, b"", 0))
        self.discovery.succeed(PipeWireDumpResult("profile-one", PipeWireDiscoverySnapshot("profile-one", 1, datetime.now(timezone.utc), (PipeWireNode(10, "A node"),), DiscoveryState.AVAILABLE), b"", 0))
        self.assertEqual(self.window.discovery_tree.topLevelItem(0).text(0), "B node")

    def test_request_rejected_valid_message_is_safe_and_non_modal(self):
        self.window._discovery_request_id = "profile-one"
        self.discovery.reject("profile-one", "discovery request already running")
        self.assertEqual(self.window.statusBar().currentMessage(), "PipeWire discovery request rejected: discovery request already running")
        self.assertNotIn("raw stderr", self.window.statusBar().currentMessage())

    def test_old_request_rejected_is_ignored(self):
        self.window._discovery_request_id = "profile-two"
        self.window.statusBar().showMessage("unchanged")
        self.discovery.reject("profile-one", "old rejection")
        self.assertEqual(self.window.statusBar().currentMessage(), "unchanged")

    def test_generic_failure_sanitizes_message_and_never_shows_stderr(self):
        self.window.refresh_discovery()
        message = "bad\noutput\t" + "x" * 300
        self.discovery.fail("profile-one", DiscoveryFailureCategory.PARSER_ERROR, message, b"SECRET STDERR")
        label = self.window.discovery_state.text()
        self.assertTrue(label.startswith("Discovery unavailable — parser_error: bad output x"))
        self.assertLessEqual(len(label.split(" — ", 1)[1]), 240 + len("parser_error: "))
        self.assertNotIn("SECRET STDERR", label)
        self.assertNotIn("SECRET STDERR", self.window.statusBar().currentMessage())

    def test_close_rejection_preserves_runner(self):
        self.run_selected()
        FakeMessageBox.answer = FakeMessageBox.No
        event = FakeCloseEvent()
        self.window.closeEvent(event)
        self.assertTrue(event.ignored)
        self.assertEqual(self.discovery.shutdown_calls, 0)
        FakeMessageBox.answer = FakeMessageBox.Yes

    def test_confirmed_close_with_active_process_shuts_runner_once(self):
        self.run_selected()
        FakeMessageBox.answer = FakeMessageBox.Yes
        event = FakeCloseEvent()
        self.window.closeEvent(event)
        self.window.closeEvent(event)
        self.assertEqual(self.discovery.shutdown_calls, 1)

    def test_discovery_callbacks_after_close_do_not_change_ui(self):
        self.window.refresh_discovery()
        self.window._closing_started = True
        before = self.window.discovery_state.text()
        snapshot = PipeWireDiscoverySnapshot("profile-one", 1, datetime.now(timezone.utc), (PipeWireNode(1, "late"),), DiscoveryState.AVAILABLE)
        self.discovery.succeed(PipeWireDumpResult("profile-one", snapshot, b"", 0))
        self.assertEqual(self.window.discovery_state.text(), before)
        self.assertEqual(self.window.discovery_tree.topLevelItemCount(), 0)

    def test_discovery_tests_never_construct_real_pw_dump_process(self):
        self.assertIsInstance(self.window.discovery_runner, FakeDiscoveryRunner)
        self.assertEqual(len(FakeDiscoveryRunner.instances), 1)


if __name__ == "__main__":
    unittest.main()
