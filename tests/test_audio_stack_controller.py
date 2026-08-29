import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from pipewire_launcher.audio_stack_controller import (
    AudioStackController,
    AudioStackSnapshot,
    AudioStackState,
)


APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    APP.processEvents()
    return predicate()


class MutableStack:
    def __init__(self, pipewire=False, qpwgraph=False):
        self.pipewire = pipewire
        self.qpwgraph = qpwgraph
        self.calls = []

    def detect(self):
        return AudioStackSnapshot(self.pipewire, self.qpwgraph)

    def start_pipewire(self):
        self.calls.append("start_pipewire")
        self.pipewire = True
        return True

    def stop_pipewire(self):
        self.calls.append("stop_pipewire")
        self.pipewire = False
        return True

    def restart_pipewire(self):
        self.calls.append("restart_pipewire")
        self.pipewire = True
        return True

    def start_qpwgraph(self):
        self.calls.append("start_qpwgraph")
        self.qpwgraph = True
        return True

    def stop_qpwgraph(self):
        self.calls.append("stop_qpwgraph")
        self.qpwgraph = False
        return True

    def restore(self):
        self.calls.append("restore")
        return 2

    def watcher_start(self):
        self.calls.append("watcher_start")

    def watcher_stop(self):
        self.calls.append("watcher_stop")


def controller_for(stack):
    return AudioStackController(
        detector=stack.detect,
        pipewire_starter=stack.start_pipewire,
        pipewire_stopper=stack.stop_pipewire,
        pipewire_restarter=stack.restart_pipewire,
        qpwgraph_starter=stack.start_qpwgraph,
        qpwgraph_stopper=stack.stop_qpwgraph,
        link_restorer=stack.restore,
        watcher_start=stack.watcher_start,
        watcher_stop=stack.watcher_stop,
        poll_interval_ms=60_000,
        operation_poll_ms=1,
        operation_timeout_ms=100,
    )


class AudioStackSnapshotTests(unittest.TestCase):
    def test_four_observed_combinations_have_explicit_states(self):
        expected = {
            (False, False): AudioStackState.STOPPED,
            (True, False): AudioStackState.PIPEWIRE_ONLY,
            (True, True): AudioStackState.RUNNING,
            (False, True): AudioStackState.ORPHANED_QPWGRAPH,
        }
        for values, state in expected.items():
            with self.subTest(values=values):
                self.assertEqual(AudioStackSnapshot(*values).state, state)


class AudioStackControllerTests(unittest.TestCase):
    def tearDown(self):
        controller = getattr(self, "controller", None)
        if controller is not None:
            controller.shutdown()

    def make_controller(self, pipewire=False, qpwgraph=False):
        self.stack = MutableStack(pipewire, qpwgraph)
        self.controller = controller_for(self.stack)
        return self.controller

    def wait_idle(self, state):
        self.assertTrue(
            wait_until(lambda: not self.controller.busy and self.controller.state == state)
        )

    def test_refresh_publishes_real_state(self):
        controller = self.make_controller(True, False)
        self.assertTrue(controller.refresh())
        self.wait_idle(AudioStackState.PIPEWIRE_ONLY)
        self.assertEqual(controller.snapshot, AudioStackSnapshot(True, False))

    def test_monitoring_detects_external_state_change(self):
        controller = self.make_controller(True, True)
        controller._poll_timer.setInterval(10)
        self.assertTrue(controller.start_monitoring())
        self.wait_idle(AudioStackState.RUNNING)
        self.stack.qpwgraph = False
        self.assertTrue(wait_until(
            lambda: controller.state == AudioStackState.PIPEWIRE_ONLY
        ))

    def test_start_from_stopped_starts_both_and_restores_monitoring(self):
        controller = self.make_controller()
        self.assertTrue(controller.start_stack())
        self.wait_idle(AudioStackState.RUNNING)
        self.assertEqual(self.stack.calls, [
            "start_pipewire", "start_qpwgraph", "restore", "watcher_start"
        ])

    def test_start_cleans_orphaned_qpwgraph_first(self):
        controller = self.make_controller(False, True)
        self.assertTrue(controller.start_stack())
        self.wait_idle(AudioStackState.RUNNING)
        self.assertEqual(self.stack.calls[:3], [
            "stop_qpwgraph", "start_pipewire", "start_qpwgraph"
        ])

    def test_stop_stops_watcher_qpwgraph_and_pipewire(self):
        controller = self.make_controller(True, True)
        self.assertTrue(controller.stop_stack())
        self.wait_idle(AudioStackState.STOPPED)
        self.assertEqual(self.stack.calls, [
            "watcher_stop", "stop_qpwgraph", "stop_pipewire"
        ])

    def test_restart_pipewire_only_restores_full_stack(self):
        controller = self.make_controller(True, False)
        self.assertTrue(controller.restart_stack())
        self.wait_idle(AudioStackState.RUNNING)
        self.assertEqual(self.stack.calls, [
            "watcher_stop", "restart_pipewire", "start_qpwgraph",
            "restore", "watcher_start",
        ])

    def test_trigger_maps_stable_states_to_expected_action(self):
        cases = [
            (AudioStackState.STOPPED, "start_pipewire"),
            (AudioStackState.PIPEWIRE_ONLY, "restart_pipewire"),
            (AudioStackState.RUNNING, "stop_pipewire"),
        ]
        for state, expected_call in cases:
            with self.subTest(state=state):
                stack = MutableStack(
                    pipewire=state != AudioStackState.STOPPED,
                    qpwgraph=state == AudioStackState.RUNNING,
                )
                controller = controller_for(stack)
                controller._state = state
                self.assertTrue(controller.trigger())
                self.assertTrue(wait_until(lambda: not controller.busy))
                self.assertIn(expected_call, stack.calls)
                controller.shutdown()

    def test_duplicate_operation_is_rejected(self):
        controller = self.make_controller()
        self.assertTrue(controller.start_stack())
        self.assertFalse(controller.stop_stack())
        self.wait_idle(AudioStackState.RUNNING)

    def test_failure_is_sanitized_then_refreshes_real_state(self):
        controller = self.make_controller()
        messages = []
        controller.operation_failed.connect(messages.append)
        controller._pipewire_starter = lambda: False
        self.assertTrue(controller.start_stack())
        self.assertTrue(wait_until(lambda: bool(messages)))
        self.assertIn("Could not start PipeWire", messages[0])
        self.assertTrue(wait_until(lambda: controller.state == AudioStackState.STOPPED))

    def test_shutdown_is_idempotent_and_rejects_new_work(self):
        controller = self.make_controller()
        controller.shutdown()
        controller.shutdown()
        self.assertFalse(controller.refresh())


if __name__ == "__main__":
    unittest.main()
