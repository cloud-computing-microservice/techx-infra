"""Real private probe failures and positive terminal admission, with SDK fakes."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from active_private_failure import failure_key, read_failure
from automation_session import release
from deployment_tasks import finish_task
from plan_review_store import encoded, read_json
from private_http import PrivateFailure
from rollback_admission import preview_public_rollback, preview_rollback

spec = importlib.util.spec_from_file_location("private_failure_fixture", Path(__file__).with_name("test-active-private-acceptance.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class PrivateFailureTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ActivePrivateTests()
        self.p.setUp(action="deploy-window-rollback"); self.addCleanup(self.p.doCleanups)
        self.f, self.d = self.p.f, self.p.d
        self.execution = self.d.execution
        self.p.p.job.update(status="completed", conclusion="success")
        self.execution_key = f"executions/{self.f.record['sessionId']}.json"
        self.f.objects[self.execution_key] = encoded(self.execution), self.d.version
        self.before = deepcopy(self.f.record)

    def fail(self, *, disconnect=True):
        self.failed_command = "22222222-2222-2222-2222-222222222222"
        self.f.record["deploymentTask"]["commandId"] = self.failed_command
        self.p.persist, self.p.private.disconnect = False, disconnect
        with self.assertRaisesRegex(PrivateFailure, "order-persistence"): self.p.perform()
        self.f.record = release(self.f.record, self.p.owner, self.p.epoch)
        finish_task(self.f.store, self.p.task, False, self.f.now)
        self.p.p.private_build.update(buildStatus="FAILED", buildComplete=True)
        original = self.f.clients["ssm"].get_command_invocation
        def invocation(**kw):
            result = original(**kw)
            if kw["CommandId"] == self.failed_command:
                result.update(CommandId=self.failed_command, Status="Failed", ResponseCode=1)
            return result
        self.f.clients["ssm"].get_command_invocation = invocation

    def preview(self):
        return preview_rollback(self.f.clients, self.d.bucket, self.f.store, self.execution, self.d.version,
            self.p.p.r.h.infra, clock=lambda: self.f.now)

    def diagnostic(self):
        key = failure_key(self.f.record, self.p.task_id)
        return read_json(self.f.clients["s3"], self.d.bucket, key)[0]

    def test_private_failure_has_sanitized_immutable_task_receipt_and_old_baseline(self):
        self.fail()
        proof = self.diagnostic()
        self.assertEqual(proof["classification"], "order-persistence")
        self.assertTrue(proof["vpn"]["disconnected"])
        self.assertEqual(self.f.record["acceptedRelease"], self.before["acceptedRelease"])
        self.assertEqual(self.f.record["stages"]["private"], self.before["stages"]["private"])
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertNotIn("header.payload.signature", json.dumps(proof))
        self.assertNotIn("fixture-private-password", json.dumps(proof))
        self.assertEqual(read_failure(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version, self.f.now),
            self.f.record["releaseCandidate"]["privateAcceptance"]["failureReference"])

    def test_actual_private_failure_admits_readonly_intent_without_a_new_attempt(self):
        self.fail()
        before = deepcopy(self.f.record)
        with patch.object(self.f.store, "write", side_effect=AssertionError("Read-only preview")):
            intent = self.preview()
        self.assertEqual(intent["failureReference"], before["releaseCandidate"]["privateAcceptance"]["failureReference"])
        self.assertEqual(intent["acceptedBaseline"], before["acceptedRelease"])
        self.assertEqual(self.f.record, before)
        with self.assertRaises(ValueError):
            preview_public_rollback(self.f.clients, self.d.bucket, self.f.store, self.execution, self.d.version,
                self.p.p.r.h.infra, clock=lambda: self.f.now)

    def test_private_auth_and_unknown_exception_are_sanitized_and_cannot_authorize_rollback(self):
        self.p.auth = False
        with self.assertRaisesRegex(PrivateFailure, "argocd-authentication"): self.p.perform()
        self.assertEqual(self.diagnostic()["classification"], "unclassified")
        self.f.record = release(self.f.record, self.p.owner, self.p.epoch)
        finish_task(self.f.store, self.p.task, False, self.f.now)
        with self.assertRaises(ValueError): self.preview()

    def test_raw_error_is_never_copied_to_private_diagnostic(self):
        secret = "customer-or-token-must-not-be-published"
        with patch.object(self.p.worker, "artifacts", side_effect=RuntimeError(secret)), self.assertRaises(RuntimeError):
            self.p.perform()
        proof = self.diagnostic()
        self.assertEqual(proof["classification"], "unclassified")
        self.assertNotIn(secret, json.dumps(proof))
        self.assertEqual(proof["vpn"], dict(state="NOT_STARTED"))

    def test_readiness_failure_before_vpn_requires_positive_terminal_observations(self):
        with patch.object(self.p.worker, "wait_ready", side_effect=PrivateFailure("runtime-readiness")), self.assertRaises(PrivateFailure):
            self.p.perform()
        self.assertEqual(self.diagnostic()["vpn"], dict(state="NOT_STARTED"))
        # Loader has not finished and manager still runs: the diagnostic alone
        # cannot grant rollback, even for an admitted application failure.
        with self.assertRaises(ValueError): self.preview()

    def test_unproven_disconnect_retains_diagnostic_but_never_authorizes_rollback(self):
        self.fail(disconnect=False)
        self.assertEqual(self.diagnostic()["vpn"], dict(state="UNPROVEN"))
        with self.assertRaises(ValueError): self.preview()

    def test_running_cancelled_or_unknown_manager_cannot_authorize(self):
        self.fail()
        build = self.p.p.private_build
        original = deepcopy(build)
        for changes in (dict(buildStatus="IN_PROGRESS", buildComplete=False), dict(buildStatus="STOPPED"), dict(buildComplete=False)):
            build.clear(); build.update(original, **changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.preview()

    def test_running_cancelled_successful_or_unknown_remote_task_cannot_authorize(self):
        self.fail()
        original = self.f.clients["ssm"].get_command_invocation
        for status, code in (("InProgress", -1), ("Cancelled", 1), ("Success", 0), ("Failed", 2), ("Failed", True)):
            def invocation(**kw):
                result = original(**kw)
                if kw["CommandId"] == self.failed_command: result.update(Status=status, ResponseCode=code)
                return result
            self.f.clients["ssm"].get_command_invocation = invocation
            with self.subTest(status=status, code=code), self.assertRaises(ValueError): self.preview()

    def test_wrong_job_identity_or_non_success_public_predecessor_cannot_authorize(self):
        self.fail()
        job = self.p.p.job
        original = deepcopy(job)
        for field, value in (("id", 999), ("conclusion", "failure"), ("runner_group_name", "Self-hosted")):
            job.clear(); job.update(original); job[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.preview()

    def test_cleanup_takeover_cannot_be_overwritten_with_failure_checkpoint(self):
        self.p.cleanup = True
        with self.assertRaises(ValueError): self.p.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertNotIn("failureReference", self.f.record["releaseCandidate"]["privateAcceptance"])

    def test_failure_receipt_cannot_be_replaced_by_duplicate_worker(self):
        self.fail()
        before = deepcopy(self.f.record)
        with self.assertRaises(ValueError): self.p.perform()
        self.assertEqual(self.f.record, before)

    def test_tampered_failure_version_context_and_baseline_never_authorize(self):
        self.fail()
        before = deepcopy(self.f.record)
        for field, value in (("sha256", "0" * 64), ("versionId", "wrong-version"), ("key", "private/other-session.json")):
            self.f.record = deepcopy(before)
            self.f.record["releaseCandidate"]["privateAcceptance"]["failureReference"][field] = value
            with self.subTest(field=field), self.assertRaises(Exception): self.preview()
        self.f.record = deepcopy(before)
        self.f.record["releaseCandidate"]["privateAcceptance"]["contextSha256"] = "0" * 64
        with self.assertRaises(ValueError): self.preview()
        self.f.record = deepcopy(before)
        self.f.record["acceptedRelease"]["chartSha"] = "0" * 40
        with self.assertRaises(ValueError): self.preview()

    def test_cleanup_race_during_proof_reads_fences_admission(self):
        self.fail()
        get = self.p.p.r.h.infra.get
        def changed(endpoint):
            value = get(endpoint)
            self.f.record["status"] = "DESTROYING"
            return value
        with patch.object(self.p.p.r.h.infra, "get", side_effect=changed), self.assertRaises(ValueError): self.preview()
        self.assertEqual(self.f.record["status"], "DESTROYING")

    def test_lost_diagnostic_response_cannot_fabricate_checkpoint_or_retry(self):
        self.p.persist = False
        put = self.f.clients["s3"].put_object
        def lost(**kw):
            result = put(**kw)
            if kw["Key"].endswith(".failure.json"): raise TimeoutError("Stored; response lost")
            return result
        with patch.object(self.f.clients["s3"], "put_object", side_effect=lost), self.assertRaises(PrivateFailure): self.p.perform()
        self.assertNotIn("failureReference", self.f.record["releaseCandidate"]["privateAcceptance"])
        self.assertEqual(self.diagnostic()["classification"], "order-persistence")
        with patch.object(self.f.clients["s3"], "put_object", side_effect=AssertionError("Duplicate worker cannot republish failure")), self.assertRaises(ValueError):
            self.p.perform()
        self.assertNotIn("failureReference", self.f.record["releaseCandidate"]["privateAcceptance"])

    def rollback_fixture(self, filename, class_name):
        self.fail()
        self.p.persist = True
        case = self
        class Admission(unittest.TestCase):
            def setUp(self):
                self.f, self.d, self.p = case.f, case.d, case.p.p
                self.execution, self.now = case.execution, case.f.now
        spec = importlib.util.spec_from_file_location("private_failure_rollback_chain", Path(__file__).with_name(filename))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        preparation = module
        while not hasattr(preparation, "reservation"): preparation = preparation.support
        with patch.object(preparation.reservation.support, "AdmissionTests", Admission):
            result = getattr(module, class_name)()
            self.addCleanup(result.doCleanups)
            result.setUp()
        return result

    def test_private_failure_runs_real_reservation_manager_and_preparation_guards(self):
        p = self.rollback_fixture("test-private-rollback-prepare.py", "PrivatePrepareTests")
        p.test_private_proposal_completes_one_terminal_task_without_ready_merge_or_baseline_change()
        self.assertEqual(self.f.record["rollbackAttempt"]["origin"]["deploymentTask"]["taskId"], self.p.task_id)

    def test_private_failure_runs_protected_review_restore_and_fresh_private_acceptance(self):
        p = self.rollback_fixture("test-rollback-private-acceptance.py", "RollbackPrivateTests")
        # All predecessor guards use the original private failure. The full
        # restore/public/private fixture runs actual production controllers.
        p.test_atomic_ready_history_and_actual_terminal_manager()
        self.assertEqual(self.f.record["status"], "READY")
        self.assertEqual(self.f.record["rollbackAttempt"]["origin"]["deploymentTask"]["taskId"], self.p.task_id)


if __name__ == "__main__": unittest.main()
