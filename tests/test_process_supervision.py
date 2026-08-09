import unittest

from pipewire_launcher.process_supervision import (
    LimitedLog,
    ProcessRegistry,
    ProcessState,
    ProcessTerminator,
)


class FakeProcess:
    pass


class ProcessRegistryTests(unittest.TestCase):
    def test_initial_state_and_generation(self):
        registry = ProcessRegistry()
        self.assertIsNone(registry.get("profile"))
        process = FakeProcess()
        record = registry.start("profile", process)
        self.assertEqual(record.state, ProcessState.STARTING)
        self.assertGreater(record.generation, 0)
        with self.assertRaises(RuntimeError):
            registry.start("profile", FakeProcess())

    def test_profiles_are_independent_and_records_are_retained(self):
        registry = ProcessRegistry()
        first = registry.start("one", FakeProcess())
        second = registry.start("two", FakeProcess())
        registry.set_running("one", first.generation, first.process, 101)
        registry.set_running("two", second.generation, second.process, 202)
        self.assertEqual(registry.get("one").pid, 101)
        self.assertEqual(registry.get("two").pid, 202)
        registry.finish("one", first.generation, first.process, 0)
        self.assertEqual(registry.get("one").state, ProcessState.EXITED)
        self.assertFalse(registry.get("one").active)

    def test_output_is_separate_and_bounded(self):
        registry = ProcessRegistry(log_limit=8)
        process = FakeProcess()
        record = registry.start("profile", process)
        registry.append_output("profile", record.generation, process, stdout=b"out", stderr=b"err")
        self.assertEqual(record.stdout_log.text(), "out")
        self.assertEqual(record.stderr_log.text(), "err")
        registry.append_output("profile", record.generation, process, stdout=b"123456789")
        self.assertEqual(len(record.stdout_log), 8)
        self.assertEqual(record.stdout_log.text(), "23456789")

    def test_invalid_utf8_does_not_raise_and_clear_is_scoped(self):
        registry = ProcessRegistry()
        one = registry.start("one", FakeProcess())
        two = registry.start("two", FakeProcess())
        registry.append_output("one", one.generation, one.process, stdout=b"\xff")
        registry.append_output("two", two.generation, two.process, stdout=b"keep")
        self.assertEqual(one.stdout_log.text(), "\ufffd")
        registry.clear_logs("one")
        self.assertEqual(one.stdout_log.text(), "")
        self.assertEqual(two.stdout_log.text(), "keep")

    def test_stale_generation_is_ignored(self):
        registry = ProcessRegistry()
        old_process = FakeProcess()
        old = registry.start("profile", old_process)
        registry.finish("profile", old.generation, old_process, 0)
        new_process = FakeProcess()
        new = registry.start("profile", new_process)
        self.assertFalse(registry.is_current("profile", old.generation, old_process))
        self.assertFalse(registry.append_output("profile", old.generation, old_process, stdout=b"old"))
        self.assertTrue(registry.is_current("profile", new.generation, new_process))

    def test_stop_and_force_kill_flags(self):
        registry = ProcessRegistry()
        process = FakeProcess()
        record = registry.start("profile", process)
        registry.set_running("profile", record.generation, process, 7)
        self.assertTrue(registry.request_stop("profile", record.generation, process))
        self.assertEqual(record.state, ProcessState.STOPPING)
        self.assertTrue(record.stop_requested)
        self.assertFalse(registry.request_stop("profile", record.generation, process))
        self.assertTrue(registry.request_force_kill("profile", record.generation, process))
        self.assertTrue(record.force_kill_requested)
        self.assertFalse(registry.request_force_kill("profile", record.generation, process))

    def test_failed_process_keeps_exit_details(self):
        registry = ProcessRegistry()
        process = FakeProcess()
        record = registry.start("profile", process)
        registry.fail("profile", record.generation, process, "could not start")
        self.assertEqual(record.state, ProcessState.FAILED)
        self.assertIsNotNone(record.finished_at)
        self.assertTrue(registry.finish("profile", record.generation, process, 127))
        self.assertEqual(record.exit_code, 127)
        self.assertIsNotNone(record.finished_at)

    def test_finish_after_error_complements_failure_without_second_finish(self):
        registry = ProcessRegistry()
        process = FakeProcess()
        record = registry.start("profile", process)
        self.assertTrue(registry.fail("profile", record.generation, process, "failed to start"))
        first_finished_at = record.finished_at
        self.assertTrue(registry.finish("profile", record.generation, process, 127))
        self.assertEqual(record.state, ProcessState.FAILED)
        self.assertEqual(record.finished_at, first_finished_at)
        self.assertFalse(registry.finish("profile", record.generation, process, 127))

    def test_final_message_is_emitted_once_and_is_not_stdout(self):
        registry = ProcessRegistry()
        process = FakeProcess()
        record = registry.start("profile", process)
        self.assertTrue(registry.emit_final_message("profile", record.generation, process, "done\n"))
        self.assertFalse(registry.emit_final_message("profile", record.generation, process, "done again\n"))
        self.assertEqual(record.stdout_log.text(), "")
        self.assertEqual(record.event_log.text(), "done\n")


class LimitedLogTests(unittest.TestCase):
    def test_rejects_invalid_limit(self):
        with self.assertRaises(ValueError):
            LimitedLog(0)

    def test_terminator_delegates_without_using_pid(self):
        class Terminable:
            def __init__(self):
                self.calls = []

            def terminate(self):
                self.calls.append("terminate")

            def kill(self):
                self.calls.append("kill")

        process = Terminable()
        terminator = ProcessTerminator()
        terminator.graceful(process)
        terminator.force(process)
        self.assertEqual(process.calls, ["terminate", "kill"])
        self.assertFalse(terminator.group_support)


if __name__ == "__main__":
    unittest.main()
