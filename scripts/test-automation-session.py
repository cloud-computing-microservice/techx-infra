from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import unittest

from automation_session import claim, transition, S3SessionStore


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.record = dict(account="058114477594", region="us-east-1", environment="staging", status="READY",
                           sessionId="staging-123456", cleanupStartAt=(self.now + timedelta(hours=1)).isoformat(),
                           deadline=(self.now + timedelta(hours=2)).isoformat())

    def test_cross_repo_owner_cannot_steal_live_lease(self):
        first = claim(self.record, "platform:100", "release", self.now)
        with self.assertRaises(ValueError): claim(first, "infra:101", "cleanup", self.now)
        renewed = claim(first, "platform:100", "release", self.now + timedelta(seconds=60))
        self.assertGreater(renewed["lease"]["expiresAt"], first["lease"]["expiresAt"])

    def test_cleanup_after_deadline_and_expired_lease(self):
        old = claim(self.record, "platform:100", "release", self.now)
        later = self.now + timedelta(hours=3)
        with self.assertRaises(ValueError): claim(old, "cleanup:101", "cleanup", later)
        cleanup = claim(old, "cleanup:101", "cleanup", later, previous_owner_stopped=True)
        self.assertGreater(cleanup["lease"]["epoch"], old["lease"]["epoch"])
        deleting = transition(cleanup, "cleanup:101", "DESTROYING", later)
        with self.assertRaises(ValueError): transition(deleting, "cleanup:101", "DESTROYED", later)
        deleting["cleanupEvidence"] = dict(inventoryEmpty=True, artifactSha256="a" * 64)
        self.assertEqual(transition(deleting, "cleanup:101", "DESTROYED", later)["status"], "DESTROYED")
        with self.assertRaises(ValueError): claim(self.record, "platform:100", "release", later)

    def test_wrong_operation_cannot_finish_teardown(self):
        release = claim(self.record, "platform:100", "release", self.now)
        with self.assertRaises(ValueError): transition(release, "platform:100", "DESTROYING", self.now)
        with self.assertRaises(ValueError): transition(release, "someone-else", "FAILED_NEEDS_CLEANUP", self.now)

    def test_bootstrap_publishing_does_not_relax_the_ready_release_contract(self):
        provisioning = dict(self.record, status="PROVISIONING", stages=dict(foundation=dict(state="APPLIED")))
        cold = claim(provisioning, "platform:100", "bootstrap-publish", self.now)
        self.assertEqual(cold["lease"]["operation"], "bootstrap-publish")
        with self.assertRaises(ValueError): claim(provisioning, "platform:100", "release", self.now)
        with self.assertRaises(ValueError): claim(self.record, "platform:100", "bootstrap-publish", self.now)
        with self.assertRaises(ValueError): transition(cold, "platform:100", "READY", self.now)

    def test_conditional_write_conflict_propagates(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if args[2] == "get-object":
                Path(args[args.index("--expected-bucket-owner") + 2]).write_text(json.dumps(self.record))
                return SimpleNamespace(stdout='{"ETag":"v1"}')
            if "--if-match" in args:
                raise subprocess.CalledProcessError(1, args, stderr="PreconditionFailed")
            self.assertIn("--if-none-match", args)
            return SimpleNamespace(stdout='{"ETag":"v1"}')
        store = S3SessionStore("techx-test-sessions", run=fake_run)
        store.write(self.record)
        record, etag = store.read()
        with self.assertRaises(subprocess.CalledProcessError): store.write(record, etag)
        self.assertEqual(len(calls), 3)  # No blind retry that overwrites another owner.


if __name__ == "__main__":
    unittest.main()
