from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from botocore.exceptions import ClientError
from deployment_tasks import create_task, validate_result
from private_chart_promotion import MergeUncertain, PrivateChartPromotion

spec = importlib.util.spec_from_file_location("private_promotion_review_support", Path(__file__).with_name("test-promotion-review.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class PrivatePromotionTests(unittest.TestCase):
    def setUp(self):
        self.review = support.PromotionReviewTests()
        self.review.setUp()
        self.addCleanup(self.review.doCleanups)
        self.f, self.p = self.review.f, self.review.p
        f = self.f
        self.approval = self.review.grant()
        f.record["bootstrapPromotionApproval"] = deepcopy(self.approval)
        self.task_id = "9" * 32
        self.task, pointer = create_task(f.clients["s3"], "control-bucket", f.record, "exec-v1", "i-1234567890abcdef0",
            "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:12345678-abcd", "promote", "chart", 1, self.task_id, f.now)
        f.record["deploymentTask"] = dict(pointer, state="RUNNING")
        self.calls, self.executors = [], []
        self.lost_merge = self.lost_marker = self.cleanup_in_merge = self.swap_release_in_marker = False
        old_put = f.clients["s3"].put_object
        def put(**kw):
            if not kw["Key"].startswith("promotions/"): return old_put(**kw)
            self.assertEqual(kw["IfNoneMatch"], "*")
            if kw["Key"] in f.objects: raise ClientError(dict(Error=dict(Code="PreconditionFailed")), "PutObject")
            f.objects[kw["Key"]] = kw["Body"]
            f.writes.append(kw["Key"])
            if self.lost_marker and kw["Key"].endswith("/calling.json"): raise TimeoutError("Lost S3 marker response")
            if self.swap_release_in_marker and kw["Key"].endswith("/calling.json"):
                f.record["bootstrapRelease"]["sha256"] = "0" * 64
            return dict(VersionId="v1")
        f.clients["s3"].put_object = put
        def merge(number, head, title, message):
            self.calls.append((number, head, title, message))
            pull = f.chart_data["pulls/7"]
            pull.update(merged=True, state="closed", merge_commit_sha="b" * 40,
                merged_by=dict(type="Bot", login="techx-promotion[bot]"), merged_at=f.now.isoformat())
            f.chart_data["git/commits/" + "b" * 40].update(parents=[dict(sha="a" * 40), dict(sha="f" * 40)], message=title + "\n\n" + message)
            f.chart_data["git/ref/heads/main"]["object"]["sha"] = "b" * 40
            if self.cleanup_in_merge: f.record["status"] = "DESTROYING"
            if self.lost_merge: raise TimeoutError("Merge succeeded but response lost")
            return dict(merged=True, sha="b" * 40)
        self.chart = SimpleNamespace(get=self.p.chart.get, merge=merge)
        runtime = SimpleNamespace(clients=f.clients, bucket="control-bucket", execution=f.execution, source=f.source,
            private=dict(deploymentExecutor=dict(instanceId="i-1234567890abcdef0")), work=f.source / "work",
            executor=lambda: self.executors.append("verified"))
        self.worker = PrivateChartPromotion(runtime, f.store, "exec-v1", self.task_id, clock=lambda: f.now,
                                           clients=lambda *args: (self.chart, self.review.infra))

    def perform(self): return self.worker.perform(self.review.owner, 2)

    def test_review_to_claimed_task_exact_merge_receipt_and_checkpoint(self):
        result = self.perform()
        self.assertEqual(result["chartSha"], "b" * 40)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][:2], (7, "f" * 40))
        self.assertEqual(len([key for key in self.f.objects if key.startswith("promotions/")]), 3)
        stage = self.f.record["stages"]["chart"]
        self.assertEqual(stage["approvalReference"], self.approval)
        self.assertEqual(stage["taskId"], self.task_id)
        self.assertEqual(stage["state"], "PROMOTED")
        self.assertGreaterEqual(len(self.executors), 2)
        self.f.record.pop("lease")
        validate_result(self.f.record, self.task)

    def test_lost_merge_response_recovers_actual_commit_without_second_put(self):
        self.lost_merge = True
        self.perform()
        self.perform()
        self.assertEqual(len(self.calls), 1)

    def test_lost_marker_never_calls_merge_or_reopens_when_resumed(self):
        self.lost_marker = True
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertFalse(self.calls)
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertFalse(self.calls)

    def test_http_failure_without_real_merge_never_retries(self):
        def failed(*args):
            self.calls.append(args)
            raise TimeoutError("No trustworthy response")
        self.chart.merge = failed
        with self.assertRaises(MergeUncertain): self.perform()
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertEqual(len(self.calls), 1)

    def test_mutable_stage_reset_cannot_reopen_a_protected_merge_call(self):
        self.perform()
        self.f.record["stages"].pop("chart")
        self.perform()
        self.assertEqual(len(self.calls), 1)

    def test_other_task_or_approval_cannot_reuse_the_intent(self):
        self.perform()
        self.worker.task_id = "8" * 32
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_actual_merge_actor_commit_title_or_parent_cannot_complete(self):
        self.perform()
        pull, commit = self.f.chart_data["pulls/7"], self.f.chart_data["git/commits/" + "b" * 40]
        for target, key, value in ((pull, "merged_by", dict(type="User", login="owner")),
                (commit, "message", "unrelated merge"), (commit, "parents", [dict(sha="0" * 40), dict(sha="f" * 40)]),
                (pull, "merged_at", (self.f.now + timedelta(hours=2)).isoformat())):
            old = target[key]
            target[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.perform()
            target[key] = old
        self.assertEqual(len(self.calls), 1)

    def test_lost_checkpoint_cas_can_reconcile_receipt_without_second_merge(self):
        write = self.f.store.write
        lost = [True]
        def uncertain(value, etag):
            write(value, etag)
            if value.get("stages", {}).get("chart", {}).get("state") == "PROMOTED" and lost[0]:
                lost[0] = False
                raise TimeoutError("Lost checkpoint CAS response")
        self.f.store.write = uncertain
        with self.assertRaises(TimeoutError): self.perform()
        self.perform()
        self.assertEqual(len(self.calls), 1)

    def test_expired_grant_or_insufficient_owned_call_margin_does_not_merge(self):
        self.f.now += timedelta(minutes=4, seconds=10)
        with self.assertRaisesRegex(ValueError, "Insufficient owned"): self.perform()
        self.assertFalse(self.calls)

    def test_cleanup_before_or_during_merge_blocks_checkpoint(self):
        self.f.record["status"] = "DESTROYING"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)
        self.f.record["status"] = "PROVISIONING"
        self.cleanup_in_merge = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("chart", self.f.record["stages"])

    def test_wrong_instance_lease_or_task_state_cannot_use_app_clients(self):
        self.worker.clients = lambda *args: self.fail("Cannot mint an App token before task admission")
        self.f.record["deploymentTask"]["state"] = "DISPATCHING"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)

    def test_null_task_version_cannot_fall_back_to_latest(self):
        self.f.record["deploymentTask"]["reference"]["versionId"] = None
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)

    def test_promoted_manager_still_requires_chart_selection_before_gitops(self):
        from lifecycle_dispatch import next_operation, operation
        self.perform()
        self.f.record.pop("lease")
        self.assertEqual(next_operation(self.f.record, operation("promote", "chart")), dict(waiting="CHART_SELECTION"))
        self.f.record["bootstrapChart"] = dict(key=f"chart-selections/staging/{self.f.record['sessionId']}/bootstrap.json", versionId="selection-v1", sha256="1" * 64)
        self.assertEqual(next_operation(self.f.record, operation("promote", "chart")), operation("bootstrap", "gitops"))

    def test_reviewed_grant_advances_only_the_completed_foundation_to_one_promotion(self):
        from lifecycle_dispatch import next_operation, operation
        self.f.record.pop("lease")
        self.f.record["stages"]["foundation"]["attempt"] = 1
        self.assertEqual(next_operation(self.f.record, operation("apply", "foundation")), operation("promote", "chart"))
        with self.assertRaises(ValueError): next_operation(self.f.record, operation("plan", "foundation"))

    def test_release_change_after_the_call_marker_is_caught_before_merge(self):
        self.swap_release_in_marker = True
        with self.assertRaisesRegex(ValueError, "release/proposal was superseded"): self.perform()
        self.assertFalse(self.calls)


if __name__ == "__main__": unittest.main()
