"""Known unready Application proof is distinct from a completed runtime rollout."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from active_release_contract import read_rollout_receipt
from automation_session import release
from deployment_manager import RollbackCandidateFailed
from deployment_tasks import finish_task
from partial_rollout import PartialRolloutFailed, failed_partial_terminal, read_partial_rollout, result_key
from plan_review_store import checksum, encoded
from private_chart_promotion import MergeUncertain
from rollback_admission import preview_rollback

spec = importlib.util.spec_from_file_location("partial_rollout_fixture", Path(__file__).with_name("test-active-rollout.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class PartialRolloutTests(unittest.TestCase):
    def setUp(self, action=None):
        self.r = support.RolloutTests()
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        self.f, self.d = self.r.f, self.r.d
        if action is not None:
            for value in (self.f.record, self.d.execution, self.r.runtime.execution, self.r.worker.checks.execution, self.r.manager.execution):
                value["action"] = action
        self.baseline = deepcopy(self.f.record["acceptedRelease"])
        self.r.g.objects[("deployment", "frontend", "techx-staging")]["status"]["readyReplicas"] = 0

    def fail(self):
        with self.assertRaises(PartialRolloutFailed): self.r.perform()
        return read_partial_rollout(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_positive_application_and_120_unready_observations_preserve_the_accepted_baseline(self):
        proof = self.fail()
        self.assertEqual(proof["kind"], "active-partial-rollout")
        self.assertEqual(proof["readiness"]["observations"], 120)
        self.assertEqual(self.f.record["releaseCandidate"]["partialRollout"]["state"], "FAILED")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

        self.assertNotIn("rollout", self.f.record["releaseCandidate"])
        self.assertNotIn("images", proof)  # Intended images are not running-image evidence.
        with self.assertRaises(ValueError): read_rollout_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        with self.assertRaises(ValueError): self.r.perform()
        self.assertEqual(len(self.r.merge_calls), 1)
        self.assertEqual(len(self.r.patch_calls), 1)


    def test_replaced_application_cannot_publish_partial_failure_authority(self):
        observations = 0
        def sleep(_):
            nonlocal observations
            observations += 1
            if observations == 120: self.r.app["metadata"]["uid"] = "another-application"
        self.r.worker.sleep = sleep
        with self.assertRaises(MergeUncertain): self.r.perform()
        self.assertNotIn("partialRollout", self.f.record["releaseCandidate"])
        self.assertNotIn(result_key(self.f.record), self.r.artifacts)

    def test_unknown_runtime_api_exception_cannot_be_reclassified_as_readiness_failure(self):
        with patch("gitops_bootstrap.GitOpsBootstrap.observe_ready", side_effect=TimeoutError("Unknown API result")), self.assertRaises(TimeoutError):
            self.r.perform()
        self.assertNotIn("partialRollout", self.f.record["releaseCandidate"])

    def test_cleanup_takeover_at_final_observation_fences_the_receipt(self):
        observations = 0
        def sleep(_):
            nonlocal observations
            observations += 1
            if observations == 120: self.f.record["status"] = "DESTROYING"
        self.r.worker.sleep = sleep
        with self.assertRaises(ValueError): self.r.perform()
        self.assertNotIn("partialRollout", self.f.record["releaseCandidate"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_lost_s3_write_does_not_guess_a_partial_checkpoint(self):
        put = self.f.clients["s3"].put_object
        def lost(**kw):
            result = put(**kw)
            if kw["Key"].endswith("/partial-rollout.json"): raise TimeoutError("Object stored, response lost")
            return result
        with patch.object(self.f.clients["s3"], "put_object", side_effect=lost), self.assertRaises(TimeoutError): self.r.perform()
        self.assertNotIn("partialRollout", self.f.record["releaseCandidate"])
        self.assertIn(result_key(self.f.record), self.r.artifacts)
        with self.assertRaises(ValueError): read_partial_rollout(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_cleanup_after_receipt_write_still_wins_over_failure_checkpoint(self):
        put = self.f.clients["s3"].put_object
        def takeover(**kw):
            result = put(**kw)
            if kw["Key"].endswith("/partial-rollout.json"): self.f.record["status"] = "DESTROYING"
            return result
        with patch.object(self.f.clients["s3"], "put_object", side_effect=takeover), self.assertRaises(ValueError): self.r.perform()
        self.assertNotIn("partialRollout", self.f.record["releaseCandidate"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)



class PartialAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.p = PartialRolloutTests()
        self.p.setUp(action="deploy-window-rollback"); self.addCleanup(self.p.doCleanups)
        self.f, self.d, self.r = self.p.f, self.p.d, self.p.r
        self.execution = self.d.execution
        self.command_id = "33333333-3333-3333-3333-333333333333"
        self.f.record["deploymentTask"]["commandId"] = self.command_id
        self.proof = self.p.fail()
        self.f.record = release(self.f.record, self.r.owner, self.r.epoch)
        finish_task(self.f.store, self.r.task, False, self.f.now)
        self.r.build.update(buildStatus="FAILED", buildComplete=True)
        invoke = self.f.clients["ssm"].get_command_invocation
        def failed(**kw):
            result = invoke(**kw)
            if kw["CommandId"] == self.command_id: result.update(CommandId=self.command_id, Status="Failed", ResponseCode=1)
            return result
        self.f.clients["ssm"].get_command_invocation = failed
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.execution), self.d.version

    def preview(self):
        return preview_rollback(self.f.clients, self.d.bucket, self.f.store, self.execution, self.d.version,
            self.r.h.infra, clock=lambda: self.f.now)

    def test_exact_failed_task_and_partial_application_proof_admit_only_the_old_accepted_baseline(self):
        before = deepcopy(self.f.record)
        with patch.object(self.f.store, "write", side_effect=AssertionError("A preview cannot mutate")):
            intent = self.preview()
        self.assertEqual(intent["failureReference"], intent["rolloutReference"])
        self.assertEqual(intent["failedChartSha"], self.r.selected)
        self.assertEqual(intent["acceptedBaseline"], before["acceptedRelease"])
        self.assertEqual(self.f.record, before)

    def test_partial_failure_manager_defers_cleanup_but_keeps_failure_exit(self):
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/partial"
        with self.assertRaises(RollbackCandidateFailed): self.r.manager.wait(deepcopy(self.f.record["deploymentTask"]), max_observations=1)
        self.assertFalse(self.f.cleanup)

    def test_actual_terminal_ssm_and_manager_cannot_be_replaced_by_a_failed_checkpoint(self):
        self.r.build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        with self.assertRaises(ValueError): self.preview()
        self.r.build.update(buildStatus="STOPPED", buildComplete=True)
        with self.assertRaises(ValueError): self.preview()

    def test_tampered_partial_observation_cannot_authorize_revert(self):
        ref = self.f.record["releaseCandidate"]["partialRollout"]["receiptReference"]
        body, version = self.r.artifacts[ref["key"]]
        proof = json.loads(body)
        proof["readiness"]["observations"] = 1
        self.r.artifacts[ref["key"]] = encoded(proof), version
        ref["sha256"] = checksum(proof)  # Exercise semantic bounds, not just checksum rejection.
        with self.assertRaises(ValueError): self.preview()

    def test_ssm_must_be_positively_failed_even_when_the_manager_is_terminal(self):
        original = self.f.clients["ssm"].get_command_invocation
        for status, code in (("InProgress", -1), ("Cancelled", 1), ("Success", 0), ("Failed", 2)):
            def invoke(**kw):
                result = original(**kw)
                if kw["CommandId"] == self.command_id: result.update(Status=status, ResponseCode=code)
                return result
            with self.subTest(status=status), patch.object(self.f.clients["ssm"], "get_command_invocation", side_effect=invoke), self.assertRaises(ValueError):
                self.preview()

    def test_short_remaining_window_still_requests_cleanup_for_a_partial_failure(self):
        from automation_session import utc
        self.f.now = utc(self.f.record["cleanupStartAt"]) - timedelta(minutes=29)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/partial"
        with self.assertRaises(ValueError): self.r.manager.wait(deepcopy(self.f.record["deploymentTask"]), max_observations=1)
        self.assertTrue(self.f.cleanup)

    def test_cleanup_race_during_partial_proof_reads_cannot_reserve_an_attempt(self):
        get = self.f.clients["s3"].get_object
        def takeover(**kw):
            result = get(**kw)
            if kw["Key"].endswith("/partial-rollout.json"): self.f.record["status"] = "DESTROYING"
            return result
        with patch.object(self.f.clients["s3"], "get_object", side_effect=takeover), self.assertRaises(ValueError): self.preview()
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_partial_failure_can_prepare_restore_and_pass_fresh_acceptance_through_real_controllers(self):
        case = self
        class Admission(unittest.TestCase):
            def setUp(self):
                self.f, self.d = case.f, case.d
                self.execution, self.now = case.execution, case.f.now
                self.p = SimpleNamespace(r=case.r)
        spec = importlib.util.spec_from_file_location("partial_rollback_private_fixture", Path(__file__).with_name("test-rollback-private-acceptance.py"))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        preparation = module
        while not hasattr(preparation, "reservation"): preparation = preparation.support
        run = self.r.runtime.run
        def reconciled(command, **kwargs):
            if self.r.app["spec"]["source"]["targetRevision"] != self.r.selected:
                self.r.g.objects[("deployment", "frontend", "techx-staging")]["status"]["readyReplicas"] = 1
            return run(command, **kwargs)
        self.r.runtime.run = reconciled
        with patch.object(preparation.reservation.support, "AdmissionTests", Admission):
            restored = module.RollbackPrivateTests()
            self.addCleanup(restored.doCleanups)
            restored.setUp()
        restored.test_atomic_ready_history_and_actual_terminal_manager()
        self.assertEqual(self.f.record["status"], "READY")
        self.assertEqual(self.f.record["acceptedRelease"]["images"], self.p.baseline["images"])
if __name__ == "__main__": unittest.main()
