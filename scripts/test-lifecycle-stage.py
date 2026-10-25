from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from automation_session import claim
from lifecycle_coordinator import initial_record
from lifecycle_stage import StageController, apply_saved_plan
from plan_approval import issue_approval
from plan_review_store import approval_key, encoded, immutable_json, read_json


class Conflict(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class StageTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        sources = {key: "a" * 40 for key in ("techx-infra", "techx-chart", "techx-platform")}
        record = initial_record("staging-stage123", sources, 4, "owner@example.invalid", "deploy-window",
                                self.now, approved_budget_usd=60)
        self.record = claim(record, "infra:100", "deploy", now=self.now)
        self.etag = 0
        def read(): return deepcopy(self.record), self.etag
        def write(value, etag):
            if etag != self.etag: raise ValueError("CAS conflict")
            self.record, self.etag = deepcopy(value), self.etag + 1
        self.objects = {}
        def put_object(**kwargs):
            key = kwargs["Key"]
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            if key in self.objects: raise Conflict()
            self.objects[key] = kwargs["Body"], f"version-{len(self.objects) + 1}"
            return dict(VersionId=self.objects[key][1])
        def get_object(**kwargs):
            body, version = self.objects[kwargs["Key"]]
            return dict(Body=io.BytesIO(body), VersionId=version)
        self.s3 = SimpleNamespace(put_object=put_object, get_object=get_object)
        self.store = SimpleNamespace(read=read, write=write)
        self.controller = StageController(self.s3, self.store, "test-control-bucket", clock=lambda: self.now)
        self.context = dict(sourceRevisions=sources, approvedBudgetUsd=60, deadline=record["deadline"],
            cleanupStartAt=record["cleanupStartAt"], configSha256="b" * 64, providerLockSha256="c" * 64,
            terraformVersion="1.15.5", backendBucket="test-control-bucket", backendKey="staging/terraform.tfstate",
            stateLineage="12345678-1234-1234-1234-123456789abc", stateSerial=0, callerPublicCidr="8.8.8.8/32",
            executorRoleArn="arn:aws:iam::058114477594:role/techx-automation-lifecycle-executor")
        self.estimate = dict(approvedBudgetUsd=60, upperBoundUsd=17.7, maximumHours=4, includesAutomation=True)
        self.calls = []

    def propose(self, stage="foundation", attempt=1, payload=b"test plan"):
        return self.controller.propose("staging-stage123", "infra:100", 1, stage, attempt, payload,
            deepcopy(self.context), [dict(address="module.eks.aws_eks_cluster.this", actions=["create"])], self.estimate,
            (self.now + timedelta(hours=1)).isoformat())

    def approve(self, ref):
        envelope, _ = read_json(self.s3, "test-control-bucket", ref["key"], ref["versionId"])
        approval = issue_approval(envelope["request"], reviewer="operator",
            review_run="https://github.com/cloud-computing-microservice/techx-infra/actions/runs/123", now=self.now)
        approval["reviewReference"] = ref
        obj = immutable_json(self.s3, "test-control-bucket", approval_key(envelope["request"], ref["sha256"]), approval)
        return dict(obj, sessionId=ref["sessionId"], stage=ref["stage"], attempt=ref["attempt"], reviewSha256=ref["sha256"])

    def apply(self, approval, runner=None, observe=None):
        def successful(path, stage):
            self.assertEqual(Path(path).read_bytes(), b"test plan")
            self.assertEqual(self.record["stages"][stage]["state"], "APPLYING")
            self.calls.append(stage)
            self.context["stateSerial"] += 1
        return self.controller.apply("staging-stage123", "infra:100", 1, approval,
            observe or (lambda: deepcopy(self.context)), runner or successful)

    def test_plan_is_durable_then_applied_exactly_once(self):
        ref = self.propose()
        self.assertEqual(self.record["status"], "PROVISIONING")
        self.assertEqual(self.record["pendingPlan"], ref)
        result = self.apply(self.approve(ref))
        self.assertEqual(result["state"], "APPLIED")
        self.assertEqual(result["stateSerial"], 1)
        self.assertNotIn("pendingPlan", self.record)
        with self.assertRaises(ValueError): self.apply(self.approve(ref))
        with self.assertRaises(ValueError): self.propose(attempt=2)
        self.assertEqual(self.calls, ["foundation"])

    def test_edge_cannot_precede_foundation_and_replans_supersede_approval(self):
        with self.assertRaises(ValueError): self.propose("edge")
        self.assertFalse(self.objects)
        first = self.propose()
        old_approval = self.approve(first)
        second = self.propose(attempt=2)
        self.assertNotEqual(first, second)
        with self.assertRaises(ValueError): self.apply(old_approval)
        self.assertFalse(self.calls)
        self.apply(self.approve(second))
        edge = self.propose("edge")
        self.assertEqual(edge["stage"], "edge")

    def test_uncertain_apply_never_replays_even_if_terraform_may_have_committed(self):
        approval = self.approve(self.propose())
        def lost_response(path, stage):
            self.calls.append(stage)
            self.context["stateSerial"] += 1
            raise RuntimeError("Lost Terraform response")
        with self.assertRaises(RuntimeError): self.apply(approval, runner=lost_response)
        self.assertEqual(self.record["stages"]["foundation"]["state"], "UNCERTAIN")
        self.assertEqual(self.record["status"], "FAILED_NEEDS_CLEANUP")
        with self.assertRaises(ValueError): self.apply(approval)
        self.assertEqual(self.calls, ["foundation"])

    def test_changed_context_before_consumption_never_runs_apply(self):
        approval = self.approve(self.propose())
        self.context["callerPublicCidr"] = "1.1.1.1/32"
        with self.assertRaises(ValueError): self.apply(approval)
        self.assertFalse(self.calls)
        self.assertEqual(self.record["stages"]["foundation"]["state"], "AWAITING_APPROVAL")

    def test_cleanup_takeover_cannot_be_overwritten_by_old_worker(self):
        approval = self.approve(self.propose())
        def cleanup_takes_over(path, stage):
            self.record["status"] = "DESTROYING"
            self.record["lease"] = dict(owner="cleanup:101", operation="cleanup", epoch=99)
        with self.assertRaises(ValueError): self.apply(approval, runner=cleanup_takes_over)
        self.assertEqual(self.record["status"], "DESTROYING")
        self.assertEqual(self.record["lease"]["owner"], "cleanup:101")

    def test_apply_success_without_observed_state_advancement_is_not_accepted(self):
        approval = self.approve(self.propose())
        with self.assertRaises(ValueError): self.apply(approval, runner=lambda *args: None)
        self.assertEqual(self.record["stages"]["foundation"]["state"], "UNCERTAIN")

    def test_state_advancement_in_different_backend_is_not_apply_success(self):
        approval = self.approve(self.propose())
        def wrong_backend(path, stage):
            self.context["stateSerial"] += 1
            self.context["backendBucket"] = "other-backend-bucket"
        with self.assertRaises(ValueError): self.apply(approval, runner=wrong_backend)
        self.assertEqual(self.record["stages"]["foundation"]["state"], "UNCERTAIN")

    def test_wrong_bucket_or_expired_lease_does_not_upload(self):
        self.context["backendBucket"] = "different-state-bucket"
        with self.assertRaises(ValueError): self.propose()
        self.assertFalse(self.objects)
        self.context["backendBucket"] = "test-control-bucket"
        self.now += timedelta(minutes=6)
        with self.assertRaises(ValueError): self.propose()
        self.assertFalse(self.objects)

    def test_crash_after_upload_before_session_cas_reuses_original_review_without_extension(self):
        write = self.store.write
        def lost_cas(*args): raise ValueError("CAS conflict")
        self.store.write = lost_cas
        expiry = (self.now + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError): self.propose()
        self.assertEqual(len(self.objects), 2)
        original = deepcopy(self.objects)
        self.assertNotIn("pendingPlan", self.record)
        self.store.write = write
        self.now += timedelta(seconds=1)
        ref = self.controller.propose("staging-stage123", "infra:100", 1, "foundation", 1, b"test plan", self.context,
            [dict(address="module.eks.aws_eks_cluster.this", actions=["create"])], self.estimate, expiry)
        self.assertEqual(self.objects, original)
        self.assertEqual(self.record["pendingPlan"], ref)

    def test_fixed_runner_reviews_binary_plan_before_apply(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(stdout=b'{"resource_changes":[]}')
        apply_saved_plan(Path(__file__).parents[1] / "environments/staging", Path("test.tfplan"), "foundation", run=run)
        self.assertEqual(calls[0][0][2:4], ["show", "-json"])
        self.assertTrue(calls[1][0][1].endswith("review-plan.py"))
        self.assertEqual(calls[1][1]["input"], b'{"resource_changes":[]}')
        self.assertEqual(calls[2][0][2], "apply")
        self.assertNotIn("-auto-approve", calls[2][0])


if __name__ == "__main__": unittest.main()
