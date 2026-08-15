import enum
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from pipewire_launcher.application_detection import ApplicationCandidate
from pipewire_launcher.audio_applications import (
    AudioApplicationManager,
    AudioApplicationStore,
)
from pipewire_launcher.audio_panel import AudioApplicationsPanel
from pipewire_launcher.process_supervision import ProcessRegistry, ProcessState


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


class FakeMessageBox:
    @classmethod
    def question(cls, *_args, **_kwargs):
        return QMessageBox.No

    @staticmethod
    def warning(*_args, **_kwargs):
        pass

    @staticmethod
    def critical(*_args, **_kwargs):
        pass

    @staticmethod
    def information(*_args, **_kwargs):
        pass


class AudioPanelTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        store_path = Path(self.temporary.name) / "audio_applications.json"
        self.candidates = (
            ApplicationCandidate(
                desktop_id="audacity.desktop",
                name="Audacity",
                executable="/usr/bin/audacity",
                categories=("AudioVideo", "Audio"),
            ),
            ApplicationCandidate(
                desktop_id="carla.desktop",
                name="Carla",
                executable="/usr/bin/carla",
            ),
        )

        def detect(_directories, *, resolver):
            del resolver
            return self.candidates

        self.manager = AudioApplicationManager(
            store=AudioApplicationStore(store_path),
            detect=detect,
        )
        self.registry = ProcessRegistry()
        self.panel = AudioApplicationsPanel(
            self.manager,
            self.registry,
            process_factory=lambda _parent: FakeProcess(),
            timer_factory=lambda _parent: FakeTimer(),
            message_box=FakeMessageBox,
        )
        self.processes = []

    def tearDown(self):
        self.panel.shutdown()
        self.panel.deleteLater()
        self.app.processEvents()

    def refresh_and_select(self, row):
        self.assertTrue(self.panel.refresh())
        item = self.panel.tree.topLevelItem(row)
        self.panel.tree.setCurrentItem(item)
        self.app.processEvents()
        return item

    def start_selected(self, row=0):
        with patch("pipewire_launcher.audio_panel.validate_profile", lambda _p: []):
            started = self.panel.start_selected()
        record = self.registry.get(self.candidates[row].desktop_id)
        if record is not None:
            self.processes.append(record.process)
        return started, record

    def test_refresh_populates_tree(self):
        item = self.refresh_and_select(0)
        self.assertEqual(self.panel.tree.topLevelItemCount(), 2)
        self.assertEqual(item.text(1), "Audacity")
        self.assertIn("pw-jack", item.text(3))
        self.assertEqual(item.text(4), "Stopped")
        self.assertIn("2 audio application(s)", self.panel.summary.text())

    def test_start_and_stop_selected_via_pw_jack(self):
        self.refresh_and_select(0)
        started, record = self.start_selected()
        self.assertTrue(started)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, ProcessState.STARTING)
        self.assertEqual(record.process.start_args[0], "pw-jack")
        self.assertEqual(
            record.process.start_args[1],
            ["--", "/usr/bin/audacity"],
        )
        self.assertIn("$ pw-jack", record.stdout_log.text())
        record.process.pid = 1234
        record.process.started.emit()
        self.assertEqual(record.state, ProcessState.RUNNING)
        self.assertEqual(
            self.panel.tree.topLevelItem(0).text(4),
            "Running",
        )
        self.panel.stop_selected()
        self.assertEqual(record.process.terminate_calls, 1)
        self.assertEqual(record.state, ProcessState.STOPPING)

    def test_stop_escalates_to_force_kill_after_timeout(self):
        self.refresh_and_select(0)
        _started, record = self.start_selected()
        record.process.started.emit()
        self.panel.stop_selected()
        timer = self.panel.stop_timers[(record.profile_id, record.generation)]
        timer.timeout.emit()
        self.assertEqual(record.process.kill_calls, 1)

    def test_finished_updates_status_column(self):
        self.refresh_and_select(0)
        _started, record = self.start_selected()
        record.process.finished.emit(0, None)
        self.assertEqual(record.state, ProcessState.EXITED)
        self.assertEqual(self.panel.tree.topLevelItem(0).text(4), "Exited")

    def test_disabled_application_cannot_start(self):
        self.manager.set_enabled(self.candidates[0], False)
        item = self.refresh_and_select(0)
        self.assertEqual(item.checkState(0), Qt.Unchecked)
        started, record = self.start_selected()
        self.assertFalse(started)
        self.assertIsNone(record)
        self.assertFalse(self.panel.start_button.isEnabled())

    def test_checkbox_toggle_is_persisted(self):
        item = self.refresh_and_select(0)
        item.setCheckState(0, Qt.Unchecked)
        self.assertFalse(self.manager.is_enabled(self.candidates[0]))
        self.assertFalse(self.panel.start_button.isEnabled())
        item.setCheckState(0, Qt.Checked)
        self.assertTrue(self.manager.is_enabled(self.candidates[0]))

    def test_shutdown_stops_timers(self):
        self.refresh_and_select(0)
        _started, record = self.start_selected()
        record.process.started.emit()
        self.panel.stop_selected()
        timer = self.panel.stop_timers[(record.profile_id, record.generation)]
        self.panel.shutdown()
        self.assertTrue(timer.deleted)
        self.assertEqual(self.panel.stop_timers, {})


if __name__ == "__main__":
    unittest.main()
