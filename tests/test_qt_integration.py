import enum
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox

import pipewire_launcher.__main__ as launcher
from pipewire_launcher.core import Profile
from pipewire_launcher.process_supervision import ProcessState


class Signal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self._slots):
            slot(*args)


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


class FakeCloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


class MainWindowQtTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patches = [
            patch.object(launcher, "ProfileStore", FakeStore),
            patch.object(launcher, "QProcess", FakeProcess),
            patch.object(launcher, "QTimer", FakeTimer),
            patch.object(launcher, "QMessageBox", FakeMessageBox),
            patch.object(launcher, "validate_profile", lambda _profile: []),
        ]
        for item in self.patches:
            item.start()
        self.window = launcher.MainWindow()
        self.processes = []

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()

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


if __name__ == "__main__":
    unittest.main()
