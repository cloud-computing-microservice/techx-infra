"""Automatic protected review consumption and one private rollback restore."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

from automation_session import release
from deployment_tasks import finish_task

spec = importlib.util.spec_from_file_location("rollback_release_worker_fixture", Path(__file__).with_name("test-private-rollback-rollout.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ReleaseControllerTests(unittest.TestCase):
    def setUp(self):
        self.p = support.RollbackRolloutTests()
        self.p.setup_rollout(controller_dispatch=True); self.addCleanup(self.p.doCleanups)
        self.f, self.controller = self.p.f, self.p.release_controller
        self.dispatcher_identity = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"

    def observe(self):
        self.f.identity["Arn"] = self.dispatcher_identity
        return self.controller.observe()

    def complete(self):
        self.p.perform()
        self.f.record = release(self.f.record, self.p.owner, self.p.epoch)
        finish_task(self.f.store, self.p.task, True, self.f.now)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/codebuild-rollback-rollout"
        self.p.manager.wait(self.p.pointer)
        self.p.build.update(buildStatus="SUCCEEDED", buildComplete=True)

    def test_review_consumption_dispatch_restore_and_terminal_readback_wait_for_public_acceptance(self):
        self.assertEqual(self.observe()["waiting"], "ROLLBACK_ROLLOUT_RUNNING")
        self.assertEqual(len(self.p.starts), 1)
        self.complete()
        result = self.observe()
        self.assertEqual(result["waiting"], "ROLLBACK_PUBLIC_ACCEPTANCE")
        self.assertEqual(self.observe(), result)
        self.assertEqual(len(self.p.starts), 1)
        self.assertEqual(len(self.p.merge_calls), 1)
        self.assertEqual(len(self.p.patch_calls), 1)
        self.assertEqual(self.f.record["acceptedRelease"], self.p.baseline)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertFalse(self.f.cleanup)

    def test_unknown_manager_handle_is_not_restarted(self):
        self.f.record["lifecycleDispatch"]["phase"] = "CALLING"
        with self.assertRaises(ValueError): self.observe()
        self.assertEqual(len(self.p.starts), 1)
        self.assertTrue(self.f.cleanup)

    def test_failed_manager_or_missing_terminal_task_requests_cleanup(self):
        self.p.build.update(buildStatus="FAILED", buildComplete=True)
        with self.assertRaises(ValueError): self.observe()
        self.assertTrue(self.f.cleanup)
        self.assertEqual(self.f.record["acceptedRelease"], self.p.baseline)

    def test_green_manager_without_restored_task_does_not_authorize_acceptance(self):
        self.p.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        with self.assertRaises(ValueError): self.observe()
        self.assertTrue(self.f.cleanup)

    def test_changed_versioned_receipt_is_not_consumed(self):
        self.complete()
        ref = self.f.record["rollbackAttempt"]["rolloutReference"]
        self.f.objects[ref["key"]] = b'{}', ref["versionId"]
        with self.assertRaises(ValueError): self.observe()
        self.assertTrue(self.f.cleanup)

    def test_stale_session_callback_cannot_clean_up_a_new_execution(self):
        self.f.record["executionVersion"] = "different-execution"
        with self.assertRaises(ValueError): self.observe()
        self.assertFalse(self.f.cleanup)


if __name__ == "__main__": unittest.main()
