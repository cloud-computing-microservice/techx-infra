"""Actual POSIX subprocess regressions plus composed injected-SDK ownership.

No cloud calls, Terraform, AMI installation or business-stage authorization.
"""
from datetime import timedelta
import os
from pathlib import Path
import runpy
import stat
import sys
import tempfile
import threading
import time
import unittest

from worker_process_v2 import BoundProcess, ProcessRejected

Helpers = runpy.run_path(str(Path(__file__).with_name("test-worker-task-v2.py")))


class ProcessValidationTests(unittest.TestCase):
    def test_sender_snapshot_is_not_an_active_lease(self):
        with self.assertRaises(ValueError):
            BoundProcess({"phase":"OWNED"})

    def test_intervals_must_be_finite_bounded_numbers(self):
        fixture = Helpers["WorkerTaskTests"](); fixture.setUp(); active = fixture.claim()
        for value in (True, 0, -1, float("nan"), float("inf"), 11, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BoundProcess(active, check_timeout=value)


@unittest.skipUnless(os.name == "posix", "Real Linux process groups are verified on Ubuntu CI")
class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Helpers["WorkerTaskTests"](); self.fixture.setUp()
        self.active = self.fixture.claim()
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.log = self.root / "task.private.log"
        self.runner = BoundProcess(self.active, check_interval=.04, check_timeout=.15, poll_interval=.005, terminate_grace=.03)
        self.release = threading.Event(); self.addCleanup(self.release.set)

    def run_code(self, code):
        return self.runner.run([sys.executable, "-c", code], cwd=str(self.root), log_path=str(self.log), environment=dict(os.environ))

    def assert_rejected(self, reason, code="import time; time.sleep(20)"):
        started = time.monotonic()
        with self.assertRaisesRegex(ProcessRejected, reason):
            self.run_code(code)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.fixture.tasks.current_task("deployment")["phase"], "OWNED")

    def test_actual_process_success_private_log_and_no_completion_publication(self):
        result = self.run_code("print('private tool output')")
        self.assertEqual(result, dict(returnCode=0, businessAcceptance="NOT_ESTABLISHED"))
        self.assertEqual(self.log.read_text().strip(), "private tool output")
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        self.assertEqual(self.fixture.tasks.current_task("deployment")["phase"], "OWNED")
        with self.assertRaisesRegex(ProcessRejected, "ALREADY_CONSUMED"):
            self.run_code("raise SystemExit(0)")
        with self.assertRaisesRegex(ProcessRejected, "ALREADY_CONSUMED"):
            BoundProcess(self.active).run([sys.executable], cwd=str(self.root), log_path=str(self.root / "other.log"), environment={})

    def test_nonzero_exit_is_not_stage_success(self):
        self.assert_rejected("STAGE_PROCESS_FAILED", "raise SystemExit(7)")

    def test_expired_initial_task_never_starts_child(self):
        self.fixture.d.aws.now += timedelta(hours=1)
        self.assert_rejected("DEADLINE")
        self.assertFalse(self.log.exists())

    def test_initial_sdk_read_hang_never_starts_child(self):
        self.active._runtime._guard = lambda *args:self.release.wait(5)
        self.assert_rejected("OBSERVATION_TIMEOUT")
        self.assertFalse(self.log.exists())

    def test_mid_stage_sdk_read_hang_stops_process(self):
        original = self.active._runtime._guard
        count = 0
        def guard(*args):
            nonlocal count
            count += 1
            if count > 1:
                self.release.wait(5)
            return original(*args)
        self.active._runtime._guard = guard
        self.assert_rejected("OBSERVATION_TIMEOUT")
        self.assertTrue(self.log.exists())

    def test_boot_change_during_stage_stops_process(self):
        original = self.active._runtime._guard
        count = 0
        def guard(*args):
            nonlocal count
            count += 1
            if count > 1:
                self.fixture.boot = "99999999-1234-1234-1234-123456789abc"
            return original(*args)
        self.active._runtime._guard = guard
        self.assert_rejected("OWNERSHIP_OR_IDENTITY_REJECTED")

    def test_local_deadline_stops_child_even_with_frozen_wall_clock(self):
        self.active._observed._envelope["task"]["expiresAt"] = (self.fixture.d.aws.now+timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assert_rejected("DEADLINE")
        self.assertTrue(self.log.exists())

    def test_hung_check_cannot_extend_earlier_local_deadline(self):
        self.active._observed._envelope["task"]["expiresAt"] = (self.fixture.d.aws.now+timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.runner = BoundProcess(self.active, check_interval=.04, check_timeout=5, poll_interval=.005, terminate_grace=.03)
        original = self.active._runtime._guard
        count = 0
        def guard(*args):
            nonlocal count
            count += 1
            if count > 1:
                self.release.wait(5)
            return original(*args)
        self.active._runtime._guard = guard
        self.assert_rejected("DEADLINE")
        self.assertTrue(self.log.exists())

    def test_term_ignoring_descendant_is_killed_after_leader_exits(self):
        ready, escaped = self.root / "ready", self.root / "escaped"
        original = self.active._runtime._guard
        def guard(*args):
            if ready.exists():
                raise ValueError("Lease lost after descendant starts")
            return original(*args)
        self.active._runtime._guard = guard
        child = ("import signal,time; from pathlib import Path; "
                 "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 f"Path({str(ready)!r}).write_text('ready'); time.sleep(.3); "
                 f"Path({str(escaped)!r}).write_text('still running'); time.sleep(20)")
        self.assert_rejected("OWNERSHIP_OR_IDENTITY_REJECTED",
            f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)")
        self.assertTrue(ready.exists())
        time.sleep(.4)
        self.assertFalse(escaped.exists(), "Grandchild must not survive its leader's TERM exit")

    def test_successful_leader_cannot_leave_descendants(self):
        self.assert_rejected("UNEXPECTED_STAGE_DESCENDANTS", "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])")

    def test_log_symlink_is_not_followed(self):
        target = self.root / "target"; target.write_text("keep")
        self.log.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.run_code("print('overwrite')")
        self.assertEqual(target.read_text(), "keep")

    def test_api_failure_after_zero_exit_still_rejects_success(self):
        original = self.active._runtime._guard
        count = 0
        def guard(*args):
            nonlocal count
            count += 1
            if count > 1:
                raise RuntimeError("private API diagnostic not logged")
            return original(*args)
        self.active._runtime._guard = guard
        self.assert_rejected("OWNERSHIP_OR_IDENTITY_REJECTED", "pass")


if __name__ == "__main__":
    unittest.main()
