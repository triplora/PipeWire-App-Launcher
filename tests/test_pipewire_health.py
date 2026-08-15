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
    _channel_token,
    _link_outputs_to_playback,
    _missing_playback_candidates,
    _parse_link_listing,
    _playback_targets,
    pipewire_process_running,
    pipewire_running,
    qpwgraph_running,
    restart_qpwgraph,
    restore_default_audio_links,
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
    def coordinator(self, runner, message_box, popen=None, link_restorer=None, qpwgraph_restarter=None):
        kwargs = dict(
            runner=runner,
            resolver=resolver,
            popen=popen or FakePopen(),
            message_box=message_box,
            poll_interval_ms=5,
            start_timeout_ms=50,
        )
        if link_restorer is not None:
            kwargs["link_restorer"] = link_restorer
        if qpwgraph_restarter is not None:
            kwargs["qpwgraph_restarter"] = qpwgraph_restarter
        return PipeWireHealthCheck(**kwargs)

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

    def test_yes_starts_services_restores_links_and_proceeds(self):
        runner = FakeRunner(
            becomes_active_after_start=True,
            process_after_start=True,
        )
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        popen = FakePopen(on_start=lambda: setattr(runner, "started", True))
        restorer = RecordingRestorer()
        check = self.coordinator(runner, message_box, popen, restorer)
        self.assertTrue(check.check())
        self.assertTrue(runner.started)
        self.assertEqual(len(restorer.calls), 1)
        self.assertEqual(message_box.warning_calls, [])

    def test_running_skips_link_restore(self):
        runner = FakeRunner(initially_active=True)
        message_box = FakeMessageBox()
        restorer = RecordingRestorer()
        check = self.coordinator(runner, message_box, link_restorer=restorer)
        self.assertTrue(check.check())
        self.assertEqual(restorer.calls, [])

    def test_declined_start_skips_link_restore(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.No)
        restorer = RecordingRestorer()
        check = self.coordinator(runner, message_box, link_restorer=restorer)
        self.assertFalse(check.check())
        self.assertEqual(restorer.calls, [])

    def test_start_failure_skips_link_restore(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        restorer = RecordingRestorer()
        check = self.coordinator(runner, message_box, link_restorer=restorer)
        self.assertFalse(check.check())
        self.assertEqual(restorer.calls, [])

    def test_yes_restarts_qpwgraph_after_start(self):
        runner = FakeRunner(
            becomes_active_after_start=True,
            process_after_start=True,
        )
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        popen = FakePopen(on_start=lambda: setattr(runner, "started", True))
        restarter = RecordingRestarter()
        check = self.coordinator(runner, message_box, popen, qpwgraph_restarter=restarter)
        self.assertTrue(check.check())
        self.assertEqual(len(restarter.calls), 1)

    def test_running_skips_qpwgraph_restart(self):
        runner = FakeRunner(initially_active=True)
        message_box = FakeMessageBox()
        restarter = RecordingRestarter()
        check = self.coordinator(runner, message_box, qpwgraph_restarter=restarter)
        self.assertTrue(check.check())
        self.assertEqual(restarter.calls, [])

    def test_declined_start_skips_qpwgraph_restart(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.No)
        restarter = RecordingRestarter()
        check = self.coordinator(runner, message_box, qpwgraph_restarter=restarter)
        self.assertFalse(check.check())
        self.assertEqual(restarter.calls, [])

    def test_start_failure_skips_qpwgraph_restart(self):
        runner = FakeRunner()
        message_box = FakeMessageBox(question_answer=QMessageBox.Yes)
        restarter = RecordingRestarter()
        check = self.coordinator(runner, message_box, qpwgraph_restarter=restarter)
        self.assertFalse(check.check())
        self.assertEqual(restarter.calls, [])


INPUT_PORTS = """Midi-Bridge:Midi Through:(playback_0) Midi Through Port-0
alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo:playback_FL
alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo:playback_FR
alsa_output.pci-0000_0e_00.4.analog-stereo:playback_FL
  |<- alsa_playback.speaker-test:output_FL
alsa_output.pci-0000_0e_00.4.analog-stereo:playback_FR
  |<- alsa_playback.speaker-test:output_FR
"""

OUTPUT_PORTS = """Midi-Bridge:Midi Through:(capture_0) Midi Through Port-0
alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo:monitor_FL
alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo:monitor_FR
alsa_input.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.mono-fallback:capture_MONO
alsa_output.pci-0000_0e_00.4.analog-stereo:monitor_FL
alsa_output.pci-0000_0e_00.4.analog-stereo:monitor_FR
alsa_input.pci-0000_0e_00.4.analog-stereo:capture_FL
alsa_input.pci-0000_0e_00.4.analog-stereo:capture_FR
alsa_playback.firefox:output_FL
alsa_playback.firefox:output_FR
alsa_playback.speaker-test:output_FL
  |-> alsa_output.pci-0000_0e_00.4.analog-stereo:playback_FL
alsa_playback.speaker-test:output_FR
  |-> alsa_output.pci-0000_0e_00.4.analog-stereo:playback_FR
"""

PCI_PLAYBACK_FL = "alsa_output.pci-0000_0e_00.4.analog-stereo:playback_FL"
PCI_PLAYBACK_FR = "alsa_output.pci-0000_0e_00.4.analog-stereo:playback_FR"


class RecordingRestorer:
    def __init__(self):
        self.calls = []

    def __call__(self, *, runner, resolver):
        self.calls.append((runner, resolver))
        return 3


class RecordingRestarter:
    def __init__(self):
        self.calls = []

    def __call__(self, *, runner, resolver, popen):
        self.calls.append((runner, resolver, popen))
        return True


class FakeQpwgraphRunner:
    """Serves ``pgrep`` / ``killall`` answers for qpwgraph."""

    def __init__(self, qpwgraph_present=True):
        self.qpwgraph_present = qpwgraph_present
        self.calls = []

    def __call__(self, arguments):
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if list(arguments) == ["pgrep", "-x", "qpwgraph"]:
            return CommandResult(0 if self.qpwgraph_present else 1, "", "")
        if list(arguments) == ["killall", "-9", "qpwgraph"]:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


def qpwgraph_resolver(name):
    if name in {"pgrep", "killall", "qpwgraph"}:
        return f"/usr/bin/{name}"
    return None


class FakeLinkRunner:
    """Serves ``pw-link`` listings and records connect attempts."""

    def __init__(self, inputs=INPUT_PORTS, outputs=OUTPUT_PORTS, link_rc=0):
        self.inputs = inputs
        self.outputs = outputs
        self.link_rc = link_rc
        self.calls = []
        self.link_commands = []

    def __call__(self, arguments):
        arguments = tuple(arguments)
        self.calls.append(arguments)
        if arguments == ("pw-link", "-l", "-i"):
            return CommandResult(0, self.inputs, "")
        if arguments == ("pw-link", "-l", "-o"):
            return CommandResult(0, self.outputs, "")
        if arguments[0] == "pw-link":
            self.link_commands.append(arguments)
            return CommandResult(self.link_rc, "", "")
        return CommandResult(0, "", "")


class DelayedPlaybackRunner:
    """Reports no playback ports for the first few input listings."""

    def __init__(self, outputs=OUTPUT_PORTS, delay_calls=1):
        self.outputs = outputs
        self.delay_calls = delay_calls
        self.input_calls = 0
        self.link_commands = []

    def __call__(self, arguments):
        arguments = tuple(arguments)
        if arguments == ("pw-link", "-l", "-i"):
            self.input_calls += 1
            if self.input_calls <= self.delay_calls:
                return CommandResult(0, "", "")
            return CommandResult(0, INPUT_PORTS, "")
        if arguments == ("pw-link", "-l", "-o"):
            return CommandResult(0, self.outputs, "")
        if arguments[0] == "pw-link":
            self.link_commands.append(arguments)
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")


def link_resolver(name):
    return "/usr/bin/pw-link" if name == "pw-link" else None


class LinkListingParsingTests(unittest.TestCase):
    def test_ports_and_their_links_are_parsed(self):
        ports = _parse_link_listing(OUTPUT_PORTS)
        self.assertEqual(
            ports["alsa_playback.speaker-test:output_FL"],
            (PCI_PLAYBACK_FL,),
        )
        self.assertEqual(
            ports["alsa_playback.firefox:output_FL"],
            (),
        )
        self.assertEqual(
            ports["alsa_input.pci-0000_0e_00.4.analog-stereo:capture_FL"],
            (),
        )

    def test_input_listing_arrow_direction_is_parsed(self):
        ports = _parse_link_listing(INPUT_PORTS)
        self.assertEqual(
            ports[PCI_PLAYBACK_FL],
            ("alsa_playback.speaker-test:output_FL",),
        )

    def test_empty_and_blank_input(self):
        self.assertEqual(_parse_link_listing(""), {})
        self.assertEqual(_parse_link_listing("  \n\n \n"), {})


class LinkTargetSelectionTests(unittest.TestCase):
    def test_channel_token(self):
        self.assertEqual(_channel_token("alsa_playback.x:output_FL"), "FL")
        self.assertEqual(
            _channel_token("alsa_output.usb:mono-fallback:playback_MONO"),
            "MONO",
        )

    def test_candidates_only_include_unlinked_stream_outputs(self):
        ports = _parse_link_listing(OUTPUT_PORTS)
        candidates = _missing_playback_candidates(ports)
        self.assertEqual(candidates, [
            "alsa_playback.firefox:output_FL",
            "alsa_playback.firefox:output_FR",
        ])

    def test_capture_streams_and_midi_are_never_candidates(self):
        ports = _parse_link_listing(OUTPUT_PORTS + (
            "alsa_capture.audacity:input_FL\n"
            "alsa_capture.audacity:input_FR\n"
        ))
        candidates = _missing_playback_candidates(ports)
        self.assertEqual(candidates, [
            "alsa_playback.firefox:output_FL",
            "alsa_playback.firefox:output_FR",
        ])

    def test_midi_playback_port_is_not_a_hardware_target(self):
        targets = _playback_targets(_parse_link_listing(INPUT_PORTS))
        self.assertNotIn("0) Midi Through Port-0", targets)

    def test_targets_prefer_the_sink_with_most_links(self):
        ports = _parse_link_listing(INPUT_PORTS)
        targets = _playback_targets(ports)
        self.assertEqual(targets, {"FL": PCI_PLAYBACK_FL, "FR": PCI_PLAYBACK_FR})

    def test_targets_fall_back_to_first_sink_when_none_linked(self):
        ports = _parse_link_listing(INPUT_PORTS.replace(
            "  |<- alsa_playback.speaker-test:output_FL\n", ""
        ).replace(
            "  |<- alsa_playback.speaker-test:output_FR\n", ""
        ))
        targets = _playback_targets(ports)
        self.assertEqual(targets, {
            "FL": "alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo:playback_FL",
            "FR": "alsa_output.usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-00.analog-stereo:playback_FR",
        })


class RestoreDefaultAudioLinksTests(unittest.TestCase):
    def test_unlinked_streams_are_linked_to_the_active_sink(self):
        runner = FakeLinkRunner()
        created = restore_default_audio_links(
            runner=runner,
            resolver=link_resolver,
            timeout_ms=100,
            poll_interval_ms=5,
        )
        self.assertEqual(created, 2)
        self.assertEqual(runner.link_commands, [
            ("pw-link", "alsa_playback.firefox:output_FL", PCI_PLAYBACK_FL),
            ("pw-link", "alsa_playback.firefox:output_FR", PCI_PLAYBACK_FR),
        ])

    def test_no_links_are_created_without_pw_link(self):
        runner = FakeLinkRunner()
        created = restore_default_audio_links(
            runner=runner,
            resolver=lambda name: None,
            timeout_ms=100,
            poll_interval_ms=5,
        )
        self.assertEqual(created, 0)
        self.assertEqual(runner.link_commands, [])

    def test_waits_for_playback_ports_to_appear(self):
        runner = DelayedPlaybackRunner(delay_calls=1)
        created = restore_default_audio_links(
            runner=runner,
            resolver=link_resolver,
            timeout_ms=2000,
            poll_interval_ms=5,
        )
        self.assertEqual(created, 2)
        self.assertGreaterEqual(runner.input_calls, 2)
        self.assertEqual(len(runner.link_commands), 2)

    def test_gives_up_when_no_playback_ports_appear(self):
        runner = DelayedPlaybackRunner(delay_calls=10**6)
        created = restore_default_audio_links(
            runner=runner,
            resolver=link_resolver,
            timeout_ms=40,
            poll_interval_ms=5,
        )
        self.assertEqual(created, 0)
        self.assertEqual(runner.link_commands, [])

    def test_failed_links_are_not_counted(self):
        runner = FakeLinkRunner(link_rc=1)
        created = restore_default_audio_links(
            runner=runner,
            resolver=link_resolver,
            timeout_ms=100,
            poll_interval_ms=5,
        )
        self.assertEqual(created, 0)
        self.assertEqual(len(runner.link_commands), 2)

    def test_already_linked_streams_are_left_alone(self):
        only_linked = OUTPUT_PORTS.replace(
            "alsa_playback.firefox:output_FL\n", ""
        ).replace("alsa_playback.firefox:output_FR\n", "")
        runner = FakeLinkRunner(outputs=only_linked)
        created = restore_default_audio_links(
            runner=runner,
            resolver=link_resolver,
            timeout_ms=100,
            poll_interval_ms=5,
        )
        self.assertEqual(created, 0)
        self.assertEqual(runner.link_commands, [])

    def test_link_outputs_to_playback_uses_target_selection(self):
        runner = FakeLinkRunner()
        outputs = _parse_link_listing(OUTPUT_PORTS)
        inputs = _parse_link_listing(INPUT_PORTS)
        created = _link_outputs_to_playback(runner, outputs, inputs)
        self.assertEqual(created, 2)
        self.assertEqual(runner.link_commands, [
            ("pw-link", "alsa_playback.firefox:output_FL", PCI_PLAYBACK_FL),
            ("pw-link", "alsa_playback.firefox:output_FR", PCI_PLAYBACK_FR),
        ])


class QpwgraphRunningTests(unittest.TestCase):
    def test_running_instance_is_detected(self):
        runner = FakeQpwgraphRunner(qpwgraph_present=True)
        self.assertTrue(
            qpwgraph_running(runner=runner, resolver=qpwgraph_resolver)
        )

    def test_no_instance_is_not_running(self):
        runner = FakeQpwgraphRunner(qpwgraph_present=False)
        self.assertFalse(
            qpwgraph_running(runner=runner, resolver=qpwgraph_resolver)
        )

    def test_missing_pgrep_is_not_running(self):
        runner = FakeQpwgraphRunner(qpwgraph_present=True)
        self.assertFalse(
            qpwgraph_running(runner=runner, resolver=lambda name: None)
        )


class RestartQpwgraphTests(unittest.TestCase):
    def test_kills_running_instance_and_launches_fresh(self):
        runner = FakeQpwgraphRunner(qpwgraph_present=True)
        popen = FakePopen()
        started = restart_qpwgraph(
            runner=runner,
            resolver=qpwgraph_resolver,
            popen=popen,
        )
        self.assertTrue(started)
        self.assertIn(("killall", "-9", "qpwgraph"), runner.calls)
        self.assertEqual(popen.calls[0][0], ("qpwgraph",))
        self.assertTrue(popen.calls[0][1]["start_new_session"])

    def test_launches_fresh_without_running_instance(self):
        runner = FakeQpwgraphRunner(qpwgraph_present=False)
        popen = FakePopen()
        started = restart_qpwgraph(
            runner=runner,
            resolver=qpwgraph_resolver,
            popen=popen,
        )
        self.assertTrue(started)
        self.assertNotIn(("killall", "-9", "qpwgraph"), runner.calls)
        self.assertEqual(len(popen.calls), 1)

    def test_missing_qpwgraph_binary_returns_false(self):
        runner = FakeQpwgraphRunner()
        popen = FakePopen()
        started = restart_qpwgraph(
            runner=runner,
            resolver=lambda name: None,
            popen=popen,
        )
        self.assertFalse(started)
        self.assertEqual(popen.calls, [])

    def test_popen_failure_returns_false(self):
        runner = FakeQpwgraphRunner(qpwgraph_present=True)

        def failing_popen(*_args, **_kwargs):
            raise OSError("boom")

        started = restart_qpwgraph(
            runner=runner,
            resolver=qpwgraph_resolver,
            popen=failing_popen,
        )
        self.assertFalse(started)
        self.assertIn(("killall", "-9", "qpwgraph"), runner.calls)


if __name__ == "__main__":
    unittest.main()
