import copy
from datetime import datetime, timedelta, timezone
import subprocess
import sys
import unittest

from leased_command import execute


class MemoryStore:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.record = dict(account="058114477594", region="us-east-1", environment="staging", status="READY",
                           sessionId="staging-123456", cleanupStartAt=(now + timedelta(hours=1)).isoformat(),
                           deadline=(now + timedelta(hours=2)).isoformat())
        self.version = 0

    def read(self): return copy.deepcopy(self.record), self.version

    def write(self, record, etag=None):
        if etag != self.version: raise ValueError("CAS conflict")
        self.record, self.version = copy.deepcopy(record), self.version + 1
        return self.version


class LeaseCommandTests(unittest.TestCase):
    def test_heartbeat_renews_and_releases_without_changing_epoch(self):
        store = MemoryStore()
        class Child:
            calls = 0
            def wait(self, timeout):
                self.calls += 1
                if self.calls == 1: raise subprocess.TimeoutExpired("trusted", timeout)
                return 0
            def poll(self): return 0
        execute(store, "platform:100", "release", "staging-123456", ["trusted"], popen=lambda *a, **k: Child())
        self.assertEqual(store.version, 3)
        self.assertEqual(store.record["leaseEpoch"], 1)
        self.assertNotIn("lease", store.record)

    def test_real_child_success_and_failure_release_lock(self):
        for exit_code in (0, 4):
            store = MemoryStore()
            args = (store, "platform:100", "release", "staging-123456",
                    [sys.executable, "-c", f"raise SystemExit({exit_code})"])
            if exit_code:
                with self.assertRaises(subprocess.CalledProcessError): execute(*args)
            else: execute(*args)
            self.assertNotIn("lease", store.record)
            self.assertEqual(store.record["leaseEpoch"], 1)

    def test_heartbeat_loss_stops_child_and_preserves_other_owner(self):
        store = MemoryStore()
        class Child:
            stopped = False
            pid = -1
            def __init__(self, *args, **kwargs): pass
            def wait(self, timeout):
                if self.stopped: return 1
                store.record["lease"] = dict(owner="cleanup:101", epoch=99)
                raise subprocess.TimeoutExpired("fake", timeout)
            def poll(self): return 1 if self.stopped else None
            def terminate(self): self.stopped = True
        child = Child()
        from unittest.mock import patch
        with patch("leased_command.terminate", side_effect=lambda p: p.terminate()):
            with self.assertRaisesRegex(ValueError, "Lease lost"):
                execute(store, "platform:100", "release", "staging-123456", ["trusted"], popen=lambda *a, **k: child)
        self.assertTrue(child.stopped)
        self.assertEqual(store.record["lease"]["owner"], "cleanup:101")

    def test_source_session_change_rejects_before_child_start(self):
        store = MemoryStore()
        with self.assertRaises(ValueError):
            execute(store, "platform:100", "release", "staging-other", ["never-run"])
        self.assertEqual(store.version, 0)


if __name__ == "__main__": unittest.main()
