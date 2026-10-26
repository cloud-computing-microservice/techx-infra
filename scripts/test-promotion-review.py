from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from plan_review_store import checksum, encoded
from review_chart_promotion import authorize_promotion, ENVIRONMENT, PromotionReview, promotion_index_key, WORKFLOW
from workflow_handoff import intent_for, intent_key, receipt_key, target_for

spec = importlib.util.spec_from_file_location("promotion_review_support", Path(__file__).with_name("test-chart-promotion.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class PromotionReviewTests(unittest.TestCase):
    def setUp(self):
        self.p = support.PromotionTests()
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)
        f = self.p.f
        self.f = f
        f.infra_run["path"] = WORKFLOW
        self.approvals = [dict(state="approved", environments=[dict(id=10, name=ENVIRONMENT)],
                               user=dict(id=123, type="User", login="reviewer"))]
        def infra_get(endpoint):
            if endpoint == "actions/runs/100": return deepcopy(f.infra_run)
            if endpoint == "actions/runs/100/approvals": return deepcopy(self.approvals)
            raise AssertionError(endpoint)
        self.infra = SimpleNamespace(get=infra_get)
        self.reader = PromotionReview(f.clients, f.store, "control-bucket", f.execution, "exec-v1", run_id=100, clock=lambda: f.now)
        self.writer = PromotionReview(f.clients, f.store, "control-bucket", f.execution, "exec-v1", run_id=100, read_only=False, clock=lambda: f.now)

    def identity(self, role, name):
        self.f.identity["Arn"] = f"arn:aws:sts::058114477594:assumed-role/techx-automation-{role}/techx-{name}-100"

    def preview(self):
        self.identity("chart-reader", "chart-read")
        return self.reader.preview(self.f.source, self.p.chart, self.infra, app_id=12345, app_slug="techx-promotion")

    def approve(self, request):
        self.identity("promotion-approver", "promotion-approve")
        return self.writer.approve(self.f.source, self.p.chart, self.infra, request, checksum(request), app_id=12345, app_slug="techx-promotion")

    def test_preview_then_real_human_review_writes_only_one_private_short_lived_grant(self):
        request = self.preview()
        self.assertFalse(self.f.writes)
        self.assertEqual(request["workflowRun"], "https://github.com/cloud-computing-microservice/techx-infra/actions/runs/100")
        ref = self.approve(request)
        self.assertEqual(self.f.writes, [f"promotion-approvals/staging/{self.f.record['sessionId']}/{checksum(request)}.json"])
        grant = json.loads(self.f.objects[ref["key"]])
        self.assertEqual(checksum(grant), ref["sha256"])
        self.assertEqual(grant["request"], request)
        self.assertEqual(grant["requestSha256"], checksum(request))
        self.assertEqual(grant["reviewer"], "reviewer")
        self.assertEqual(grant["expiresAt"], (self.f.now + timedelta(minutes=15)).isoformat())
        self.assertEqual(self.f.etag, 1)  # No lease or session mutation by the review role.

    def test_preview_identity_cannot_publish_even_with_a_valid_review(self):
        request = self.preview()
        with self.assertRaisesRegex(ValueError, "Preview identity"):
            self.reader.approve(self.f.source, self.p.chart, self.infra, request, checksum(request), app_id=12345, app_slug="techx-promotion")
        self.assertFalse(self.f.writes)

    def automatic_review(self):
        target = target_for("review-promotion", self.f.record, self.f.execution)
        intent = intent_for(self.f.record, "exec-v1", target)
        dispatch_id = intent["dispatchId"]
        key = intent_key(self.f.record["sessionId"], dispatch_id)
        ref = dict(key=key, versionId="v1", sha256=checksum(intent))
        self.f.objects[key] = encoded(intent)
        self.f.record["workflowHandoff"] = dict(id=dispatch_id, phase="OBSERVED", intentReference=ref)
        self.receipt_key = receipt_key(self.f.record["sessionId"], dispatch_id)
        self.f.objects[self.receipt_key] = encoded(dict(schemaVersion=1, dispatchId=dispatch_id, intentReference=ref,
            response=dict(workflow_run_id=100, run_url="https://api.github.com/repos/cloud-computing-microservice/techx-infra/actions/runs/100",
                          html_url="https://github.com/cloud-computing-microservice/techx-infra/actions/runs/100")))
        get = self.f.clients["s3"].get_object
        self.f.clients["s3"].get_object = lambda **kw: dict(VersionId="session-v1", Body=io.BytesIO(encoded(self.f.record))) \
            if kw["Key"] == "automation/staging/session.json" else get(**kw)
        self.reader.dispatch_id = self.writer.dispatch_id = dispatch_id

    def test_automatic_review_requires_actual_receipt_and_publishes_exact_run_index(self):
        self.automatic_review()
        request = self.preview()
        ref = self.approve(request)
        key = promotion_index_key(self.f.record["sessionId"], 100)
        index = json.loads(self.f.objects[key])
        self.assertEqual(index["approvalReference"], ref)
        self.assertEqual(index["workflowRun"], request["workflowRun"])
        self.assertEqual(index["dispatchId"], self.reader.dispatch_id)
        self.assertEqual(self.f.writes, [ref["key"], key])
        self.assertEqual(self.f.etag, 1)

    def test_automatic_child_cannot_preview_or_publish_under_another_run_receipt(self):
        self.automatic_review()
        receipt = json.loads(self.f.objects[self.receipt_key])
        receipt["response"] = dict(workflow_run_id=101,
            run_url="https://api.github.com/repos/cloud-computing-microservice/techx-infra/actions/runs/101",
            html_url="https://github.com/cloud-computing-microservice/techx-infra/actions/runs/101")
        self.f.objects[self.receipt_key] = encoded(receipt)
        with self.assertRaisesRegex(ValueError, "protected dispatched run"): self.preview()
        self.assertFalse(self.f.writes)

    def test_cleanup_after_grant_write_cannot_publish_a_discoverable_index(self):
        self.automatic_review()
        request = self.preview()
        put = self.f.clients["s3"].put_object
        def cleanup(**kw):
            result = put(**kw)
            self.f.record["status"] = "DESTROYING"
            return result
        self.f.clients["s3"].put_object = cleanup
        with self.assertRaises(ValueError): self.approve(request)
        self.assertEqual(len(self.f.writes), 1)
        self.assertNotIn(promotion_index_key(self.f.record["sessionId"], 100), self.f.objects)

    def test_wrong_role_session_run_or_source_is_rejected(self):
        self.identity("promotion-approver", "promotion-approve")
        with self.assertRaisesRegex(ValueError, "exact read-only"): self.reader.preview(self.f.source, self.p.chart, self.infra, app_id=12345, app_slug="techx-promotion")
        self.identity("chart-reader", "chart-read")
        for key, value in (("run_attempt", 2), ("head_sha", "0" * 40), ("head_branch", "feature"), ("path", ".github/workflows/other.yml")):
            old = self.f.infra_run[key]
            self.f.infra_run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.preview()
            self.f.infra_run[key] = old

    def test_changed_preview_or_wrong_checksum_cannot_issue_approval(self):
        request = self.preview()
        self.identity("promotion-approver", "promotion-approve")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.writer.approve(self.f.source, self.p.chart, self.infra, request, "0" * 64, app_id=12345, app_slug="techx-promotion")
        request["manifest"]["images"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "changed after preview"): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_missing_rejected_or_ambiguous_environment_review_cannot_issue_approval(self):
        request = self.preview()
        original = deepcopy(self.approvals)
        for rows in ([], [dict(original[0], state="rejected")], original + original):
            self.approvals[:] = rows
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "real staging-chart-promote"): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_withdrawn_environment_review_after_revalidation_cannot_issue_approval(self):
        request = self.preview()
        get = self.infra.get
        count = 0
        def revoked(endpoint):
            nonlocal count
            if endpoint.endswith("/approvals"):
                count += 1
                if count == 2: return []
            return get(endpoint)
        self.infra.get = revoked
        with self.assertRaises(ValueError): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_human_pr_review_withdrawal_between_preview_and_approval_is_rejected(self):
        request = self.preview()
        self.f.chart_data["pulls/7/reviews?per_page=100"][0]["state"] = "DISMISSED"
        with self.assertRaises(ValueError): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_current_cleanup_or_lease_blocks_grant_without_mutating_evidence(self):
        request = self.preview()
        self.f.record["status"] = "DESTROYING"
        with self.assertRaises(ValueError): self.approve(request)
        self.f.record["status"] = "PROVISIONING"
        self.f.record["lease"] = dict(owner="other")
        with self.assertRaisesRegex(ValueError, "idle session"): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_review_near_cleanup_cannot_extend_the_window(self):
        request = self.preview()
        self.f.now = self.writer.execution["cleanupStartAt"]
        from automation_session import utc
        self.f.now = utc(self.f.now) - timedelta(minutes=2)
        with self.assertRaisesRegex(ValueError, "insufficient time"): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_grant_is_capped_before_cleanup_even_if_review_occurs_late(self):
        request = self.preview()
        from automation_session import utc
        self.f.now = utc(self.writer.execution["cleanupStartAt"]) - timedelta(minutes=10)
        ref = self.approve(request)
        grant = json.loads(self.f.objects[ref["key"]])
        self.assertEqual(utc(grant["expiresAt"]), utc(request["cleanupStartAt"]) - timedelta(minutes=3))

    def grant(self):
        request = self.preview()
        reference = self.approve(request)
        self.f.infra_run.update(status="completed", conclusion="success", created_at=self.f.now.isoformat(), updated_at=self.f.now.isoformat())
        self.owner = "arn:aws:ec2:us-east-1:058114477594:instance/i-1234567890abcdef0"
        self.f.record["lease"] = dict(owner=self.owner, operation="deploy", epoch=2, expiresAt=(self.f.now + timedelta(minutes=5)).isoformat())
        return reference

    def authorize(self, reference, **fields):
        options = dict(owner=self.owner, epoch=2, app_id=12345, app_slug="techx-promotion")
        options.update(fields)
        return authorize_promotion(self.p.checks, self.f.source, self.p.chart, self.infra, reference, **options)

    def test_private_owned_consumer_revalidates_bytes_candidate_and_successful_review_run(self):
        ref = self.grant()
        result = self.authorize(ref)
        self.assertEqual(result["reference"], ref)
        self.assertEqual(result["approval"]["reviewer"], "reviewer")
        self.assertEqual(len(self.f.writes), 1)
        with self.assertRaises(ValueError): self.authorize(ref, owner=None)
        with self.assertRaises(ValueError): self.authorize(ref, epoch=3)

    def test_wrong_grant_checksum_version_or_scope_cannot_authorize(self):
        ref = self.grant()
        for changes in (dict(sha256="0" * 64), dict(versionId=None), dict(key="promotion-approvals/staging/other/" + "a" * 64 + ".json")):
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.authorize(dict(ref, **changes))

    def test_expired_or_extended_grant_cannot_be_consumed(self):
        ref = self.grant()
        grant = json.loads(self.f.objects[ref["key"]])
        original = deepcopy(grant)
        for changes in (dict(expiresAt=self.f.now.isoformat()), dict(expiresAt=(self.f.now + timedelta(minutes=16)).isoformat()),
                        dict(approvedAt=(self.f.now + timedelta(minutes=1)).isoformat())):
            modified = dict(original, **changes)
            self.f.objects[ref["key"]] = json.dumps(modified).encode()
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.authorize(dict(ref, sha256=checksum(modified)))

    def test_failed_rerun_wrong_source_or_withdrawn_review_cannot_authorize(self):
        ref = self.grant()
        for key, value in (("conclusion", "failure"), ("run_attempt", 2), ("head_sha", "0" * 40), ("path", ".github/workflows/other.yml")):
            old = self.f.infra_run[key]
            self.f.infra_run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.authorize(ref)
            self.f.infra_run[key] = old
        self.approvals[:] = []
        with self.assertRaises(ValueError): self.authorize(ref)

    def test_new_ci_run_even_if_green_requires_a_new_review(self):
        ref = self.grant()
        self.p.run["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "differs from its reviewed"): self.authorize(ref)

    def test_cleanup_fence_after_grant_prevents_consumption(self):
        ref = self.grant()
        self.f.record["status"] = "DESTROYING"
        with self.assertRaises(ValueError): self.authorize(ref)

    def test_second_precision_run_completion_still_binds_a_fractional_grant_timestamp(self):
        self.f.now = (self.f.now + timedelta(seconds=1)).replace(microsecond=500000)
        ref = self.grant()
        self.f.infra_run["updated_at"] = self.f.now.replace(microsecond=0).isoformat()
        self.authorize(ref)


if __name__ == "__main__": unittest.main()
