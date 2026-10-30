"""Rollback preview reads actual rollout/public-failure fixture receipts and runs."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from active_public_contract import active_result_key
from automation_session import release, utc
from plan_review_store import checksum, encoded, immutable_json, read_json
from rollback_admission import preview_public_rollback
from workflow_handoff import intent_for, intent_key, receipt_key, target_for, title

spec = importlib.util.spec_from_file_location("rollback_public_fixture", Path(__file__).with_name("test-active-public-acceptance.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ActivePublicTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        p, f = self.p, self.p.f
        self.f, self.d = f, p.d
        p.service.mutate = lambda response, method, path: response.update(status=503) if path == "/" else None
        with self.assertRaises(ValueError): p.perform()
        f.record = release(f.record, p.worker.owner, f.record["lease"]["epoch"])
        p.run.update(status="completed", conclusion="failure", updated_at=f.now.isoformat())
        p.job.update(status="completed", conclusion="failure")
        s3, bucket, record = f.clients["s3"], p.d.bucket, f.record
        old = deepcopy(record["workflowHandoff"])
        old_receipt, _ = read_json(s3, bucket, old["receiptReference"]["key"], old["receiptReference"]["versionId"])
        key = active_result_key(record["sessionId"], p.release_id, old["id"]).replace("result.json", "failure.json")
        failure, _ = read_json(s3, bucket, key)
        # Model a newly admitted opt-in execution and its protected public
        # intent. The normal opening UI intentionally does not expose it yet.
        self.execution = deepcopy(p.d.execution); self.execution["action"] = record["action"] = "deploy-window-rollback"
        before = deepcopy(record); before["status"] = "TESTING"
        before["releaseCandidate"].pop("publicAcceptance")
        intent = intent_for(before, p.d.version, target_for("accept-active-public", before, self.execution))
        dispatch = intent["dispatchId"]
        intent_ref = immutable_json(s3, bucket, intent_key(record["sessionId"], dispatch), intent)
        intent_ref["sha256"] = checksum(intent)
        receipt = dict(schemaVersion=1, dispatchId=dispatch, intentReference=intent_ref, response=old_receipt["response"])
        receipt_ref = immutable_json(s3, bucket, receipt_key(record["sessionId"], dispatch), receipt)
        receipt_ref["sha256"] = checksum(receipt)
        record["workflowHandoff"] = dict(id=dispatch, operation="accept-active-public", phase="OBSERVED",
            intentReference=intent_ref, receiptReference=receipt_ref)
        record["releaseCandidate"]["publicAcceptance"]["dispatchId"] = failure["dispatchId"] = dispatch
        p.run["display_title"] = title(dispatch)
        self.failure_key = active_result_key(record["sessionId"], p.release_id, dispatch).replace("result.json", "failure.json")
        immutable_json(s3, bucket, self.failure_key, failure)
        self.execution_key = f"executions/{record['sessionId']}.json"
        f.objects[self.execution_key] = encoded(self.execution), p.d.version
        self.now = f.now

    def preview(self):
        return preview_public_rollback(self.f.clients, self.d.bucket, self.f.store, self.execution, self.d.version,
            self.p.r.h.infra, clock=lambda: self.now)

    def test_exact_failed_public_run_and_rollout_build_produce_read_only_bounded_intent(self):
        original = deepcopy(self.f.record)
        with patch.object(self.f.store, "write", side_effect=AssertionError("A preview cannot mutate session")):
            result = self.preview()
        self.assertEqual(result["acceptedBaseline"], original["acceptedRelease"])
        self.assertEqual(result["failedChartSha"], self.p.r.selected)
        self.assertEqual(result["failureReference"]["key"], self.failure_key)
        self.assertEqual(utc(result["expiresAt"]) - utc(result["createdAt"]), timedelta(minutes=30))
        self.assertEqual(self.f.record, original)

    def test_opt_out_changed_immutable_execution_used_attempt_or_short_window_rejects(self):
        original = deepcopy(self.f.record)
        for field, value in (("action", "deploy-window"), ("rollbackAttempt", dict(attempt=1)), ("status", "DESTROYING"), ("lease", dict(owner="other"))):
            self.f.record = dict(deepcopy(original), **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): self.preview()
        self.f.record = original
        self.now = utc(original["cleanupStartAt"]) - timedelta(minutes=15)
        with self.assertRaises(ValueError): self.preview()
        self.now = self.f.now
        self.execution["maximumHours"] += 1
        with self.assertRaises(ValueError): self.preview()

    def test_running_cancelled_or_wrong_first_attempt_workflow_is_not_failure_authority(self):
        original = deepcopy(self.p.run)
        for field, value in (("status", "in_progress"), ("conclusion", "cancelled"), ("run_attempt", 2), ("head_sha", "0" * 40)):
            self.p.run = dict(deepcopy(original), **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): self.preview()

    def test_self_hosted_or_other_job_cannot_authorize_rollback(self):
        self.p.job["runner_group_name"] = "Self-hosted"
        with self.assertRaises(ValueError): self.preview()

    def test_unknown_or_changed_failure_diagnostic_cannot_authorize_rollback(self):
        body, version = self.f.objects[self.failure_key]
        original = json.loads(body)
        for field, value in (("classification", "TimeoutError"), ("contextSha256", "0" * 64), ("dispatchId", "0" * 64)):
            changed = dict(original, **{field: value})
            self.f.objects[self.failure_key] = encoded(changed), version
            with self.subTest(field=field), self.assertRaises(ValueError): self.preview()

    def test_known_application_failure_is_distinct_from_network_uncertainty(self):
        body, version = self.f.objects[self.failure_key]
        original = json.loads(body)
        for classification in ("store-config", "api-json", "health-json", "static-asset-discovery"):
            self.f.objects[self.failure_key] = encoded(dict(original, classification=classification)), version
            with self.subTest(classification=classification): self.assertEqual(self.preview()["attempt"], 1)
        for classification in ("public-peer-address", "tls-proof", "cloudfront-headers", "TimeoutError"):
            self.f.objects[self.failure_key] = encoded(dict(original, classification=classification)), version
            with self.subTest(classification=classification), self.assertRaises(ValueError): self.preview()

    def test_rollout_manager_must_be_positively_terminal_even_with_receipt(self):
        self.p.r.build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        with self.assertRaises(ValueError): self.preview()

    def test_changed_accepted_baseline_or_rollout_is_not_used_for_revert(self):
        self.f.record["acceptedRelease"]["chartSha"] = "0" * 40
        with self.assertRaises(ValueError): self.preview()

    def test_cleanup_takeover_during_external_reads_fences_the_preview(self):
        original = self.p.r.h.infra.get
        def get(endpoint):
            result = original(endpoint)
            self.f.record.update(status="DESTROYING")
            return result
        self.p.r.h.infra.get = get
        with self.assertRaises(ValueError): self.preview()
        self.assertEqual(self.f.record["status"], "DESTROYING")


if __name__ == "__main__": unittest.main()
