import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QMessageBox

from pipewire_launcher.pipewire_health import (
    CommandResult,
    PipeWireHealthCheck,
    pipewire_process_running,
    pipewire_running,
    run_command,
    start_pipewire_services,
    systemd_unit_active,
)


class FakeRunner:
    """Turns active when a ``systemctl --user start`` command is observed."""

    def __init__(self, initially_active=False, becomes_active_after_start=False, process_after_start=False):
        self.calls = []
        self.started = False
        self.initially_active = initially_active
        self.becomes_active_after_start = becomes_active_after_start
        self.process_after_start = process_after_start

    def __call__(self, arguments):
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if list(arguments) == [
            "systemctl", "--user", "start",
            "pipewire", "pipewire-pulse", "wireplumber",
        ]:
            self.started = True
            return CommandResult(0, "", "")
        if list(arguments) == ["systemctl", "--user", "is-active", "pipewire"]:
            active = self.initially_active or (
                self.started and self.becomes_active_after_start
            )
            return CommandResult(0 if active else 3, "active\n" if active else "inactive\n", "")
        if list(arguments) == ["pgrep", "-x", "pipewire"]:
            if self.started and self.process_after_start:
                return CommandResult(0, "42\n", "")
            return CommandResult(1, "", "")
        return CommandResult(0, "", "")


class FakePopen:
    def __init__(self, on_start=None):
        self.calls = []
        self.on_start = on_start

    def __call__(self, arguments, **kwargs):
        self.calls.append((tuple(arguments), kwargs))
        if self.on_start is not None:
            self.on_start()


class FakeMessageBox:
    def __init__(self, question_answer=QMessageBox.No):
        self.question_answer = question_answer
        self.question_calls = []
        self.warning_calls = []

    def question(self, parent, title, text, buttons, default_button):
        self.question_calls.append((title, text, buttons, default_button))
        return self.question_answer

    def warning(self, parent, title, text):
        self.warning_calls.append((title, text))


def resolver(name):
    return f"/usr/bin/{name}" if name in {"systemctl", "pgrep"} else None


class SystemdUnitActiveTests(unittest.TestCase):
    def test_active_unit_returns_true(self):
        runner = FakeRunner(initially_active=True)
        self.assertTrue(
            systemd_unit_active(runner=runner, resolver=resolver)
        )

    def test_inactive_unit_is_not_definitive(self):
        runner = FakeRunner(initially_active=False)
        self.assertIsNone(
            systemd_unit_active(runner=runner, resolver=resolver)
        )

    def test_missing_systemctl_returns_none(self):
        runner = FakeRunner()
        self.assertIsNone(
            systemd_unit_active(
                runner=runner,
                resolver=lambda name: None,
            )
        )


class PipeWireRunningTests(unittest.TestCase):
    def test_active_unit_is_running(self):
        runner = FakeRunner(initially_active=True)
        self.assertTrue(pipewire_running(runner=runner, resolver=resolver))

    def test_no_unit_no_process_is_not_running(self):
        runner = FakeRunner(initially_active=False)
        self.assertFalse(pipewire_running(runner=runner, resolver=resolver))

    def test_inactive_unit_with_process_is_running(self):
        runner = FakeRunner(initially_active=False, process_after_start=True)
        runner.started = True
        self.assertTrue(pipewire_running(runner=runner, resolver=resolver))

    def test_process_probe_is_used_when_systemctl_is_missing(self):
        runner = FakeRunner(initially_active=False, process_after_start=True)
        runner.started = True
        self.assertTrue(
            pipewire_running(
                runner=runner,
                resolver=lambda name: "/usr/bin/pgrep" if name == "pgrep" else None,
            )
        )

    def test_socket_probe_is_used_without_pgrep(self):
        def no_resolver(_name):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipewire-0"
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(str(path))
            try:
                with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                    self.assertTrue(
                        pipewire_process_running(resolver=no_resolver)
                    )
            finally:
                sock.close()

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                self.assertFalse(
                    pipewire_process_running(resolver=no_resolver)
                )


class RunCommandTests(unittest.TestCase):
    def test_run_command_captures_success(self):
        result = run_command(["true"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_run_command_captures_failure(self):
        result = run_command(["false"])
        self.assertEqual(result.returncode, 1)

    def test_run_command_returns_result_for_missing_executable(self):
        result = run_command(["definitely-not-a-real-executable-xyz"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No such file", result.stderr)


class StartPipeWireServicesTests(unittest.TestCase):
    def test_start_command_is_constructed(self):
        popen = FakePopen()
        self.assertTrue(
            start_pipewire_services(resolver=resolver, popen=popen)
        )
        self.assertEqual(popen.calls[0][0], (
            "systemctl", "--user", "start",
            "pipewire", "pipewire-pulse", "wireplumber",
        ))
        self.assertTrue(popen.calls[0][1]["start_new_session"])

    def test_no_systemctl_returns_false(self):
        self.assertFalse(
            start_pipewire_services(resolver=lambda name: None, popen=FakePopen())
        )


class PipeWireHealthCheckTests(unittest.TestCase):
    def coordinator(self, runner, message_box, popen=None):
        return PipeWireHealthCheck(
            runner=runner,
            resolver=resolver,
            popen=popen or FakePopen(),
            message_box=message_box,
            poll_interval_ms=5,
            start_timeout_ms=50,
        )

    def test_returns_true_without_dialog_when_running(self):
        runner = FakeRunner(initially_active=True)
        message_box = FakeMessageBox()
        self.assertTrue(self.coordinator(runner, message_box).check())
        self.assertEqual(message_box.question_calls, [])

    def test_yes_starts_services_and_proceeds(self):
        runner = FakeRunner(
            becomes_active_after_start=True,
            process_after_start=True,
        )
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        popen = FakePopen(on_start=lambda: setattr(runner, "started", True))
        self.assertTrue(self.coordinator(runner, message_box, popen).check())
        self.assertTrue(runner.started)
        self.assertEqual(message_box.warning_calls, [])

    def test_no_aborts_launcher(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.No)
        self.assertFalse(self.coordinator(runner, message_box).check())
        self.assertFalse(runner.started)
        self.assertEqual(len(message_box.warning_calls), 1)

    def test_start_failure_aborts_launcher(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        popen = FakePopen(on_start=lambda: setattr(runner, "started", True))
        self.assertFalse(self.coordinator(runner, message_box, popen).check())
        self.assertTrue(runner.started)
        self.assertEqual(len(message_box.warning_calls), 1)

    def test_question_uses_expected_text(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        self.coordinator(runner, message_box).check()
        self.assertEqual(
            message_box.question_calls[0][1],
            "O servidor de áudio PipeWire não está rodando. Deseja iniciá-lo agora?",
        )


if __name__ == "__main__":
    unittest.main()
