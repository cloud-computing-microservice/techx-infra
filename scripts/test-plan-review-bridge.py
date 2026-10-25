from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from types import SimpleNamespace
import unittest

from approve_plan import ENVIRONMENT, REPO, WORKFLOW, process
from cleanup_schedule import definition
from lifecycle_coordinator import initial_record
from plan_approval import create_request
from plan_review_store import approval_key, encoded, load_review, publish_review, read_authorized_plan, summarize_changes


class Conflict(Exception):
    response = {"Error": {"Code": "PreconditionFailed"}}


class ReviewBridgeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        sources = {name: "a" * 40 for name in ("techx-infra", "techx-chart", "techx-platform")}
        self.session = initial_record("staging-review123", sources, 4, "owner@example.invalid", "deploy-window",
                                      self.now, approved_budget_usd=60)
        self.context = dict(sourceRevisions=sources, approvedBudgetUsd=60, deadline=self.session["deadline"],
            cleanupStartAt=self.session["cleanupStartAt"], configSha256="b" * 64, providerLockSha256="c" * 64,
            terraformVersion="1.15.5", backendBucket="test-control-bucket", backendKey="staging/terraform.tfstate",
            stateLineage="12345678-1234-1234-1234-123456789abc", stateSerial=0, callerPublicCidr="8.8.8.8/32",
            executorRoleArn="arn:aws:iam::058114477594:role/techx-automation-lifecycle-executor")
        self.request = create_request(self.session, "foundation", 1, b"test plan", "plan-version", self.context,
            expires_at=(self.now + timedelta(hours=1)).isoformat(), now=self.now)
        self.objects, self.writes, self.reads = {}, [], []
        self.fail_put, self.wrong_version, self.close_after_read = False, False, False
        self.session_reads = 0
        def put_object(**kwargs):
            key = kwargs["Key"]
            self.assertEqual(kwargs["ExpectedBucketOwner"], "058114477594")
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            if self.fail_put: raise RuntimeError("Access denied")
            if key in self.objects: raise Conflict()
            self.writes.append(key)
            self.objects[key] = (kwargs["Body"], f"version-{len(self.objects) + 1}")
            return dict(VersionId=self.objects[key][1])
        def get_object(**kwargs):
            self.assertEqual(kwargs["ExpectedBucketOwner"], "058114477594")
            self.reads.append(kwargs)
            if kwargs["Key"] == "automation/staging/session.json":
                self.session_reads += 1
                if self.close_after_read and self.session_reads > 1:
                    return dict(Body=io.BytesIO(encoded(dict(self.session, status="DESTROYING"))), VersionId="session-new")
                return dict(Body=io.BytesIO(encoded(self.session)), VersionId="session-version")
            payload, version = self.objects[kwargs["Key"]]
            return dict(Body=io.BytesIO(payload), VersionId="wrong-version" if self.wrong_version else version)
        self.role = "techx-automation-plan-approver"
        self.clients = dict(
            s3=SimpleNamespace(put_object=put_object, get_object=get_object),
            sts=SimpleNamespace(get_caller_identity=lambda: dict(Account="058114477594",
                Arn=f"arn:aws:sts::058114477594:assumed-role/{self.role}/test-run")))
        self.estimate = dict(approvedBudgetUsd=60, upperBoundUsd=17.7, maximumHours=4, includesAutomation=True)
        self.ref = publish_review(self.clients["s3"], "test-control-bucket", self.request,
                                  [dict(address="module.eks.aws_eks_cluster.this", actions=["create"])], self.now,
                                  estimate=self.estimate)
        self.session["pendingPlan"] = deepcopy(self.ref)
        self.session["executionVersion"] = "execution-v1"
        self.schedule = definition(self.session, "execution-v1",
            "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher",
            "arn:aws:iam::058114477594:role/techx-automation-scheduler",
            "arn:aws:sqs:us-east-1:058114477594:techx-automation-cleanup-dlq", self.now)
        self.schedule["Arn"] = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/" + self.schedule["Name"]
        self.session["cleanupScheduleArn"] = self.schedule["Arn"]
        self.clients["scheduler"] = SimpleNamespace(get_schedule=lambda **kwargs: deepcopy(self.schedule))
        self.run = dict(id=123, repository=dict(full_name=REPO), head_repository=dict(full_name=REPO),
            event="workflow_dispatch", head_branch="main", path=WORKFLOW, head_sha="a" * 40, run_attempt=1,
            status="in_progress", created_at=(self.now + timedelta(seconds=30)).isoformat())
        self.history = [dict(state="approved", environments=[dict(id=45, name=ENVIRONMENT)],
                             user=dict(id=46, login="human-reviewer", type="User"))]
        self.github = SimpleNamespace(get=lambda endpoint: deepcopy(self.history if endpoint.endswith("/approvals") else self.run))

    def work(self, **kwargs):
        values = dict(approve=True, now=self.now + timedelta(minutes=1))
        values.update(kwargs)
        return process(self.clients, self.github, "test-control-bucket", self.ref, "123", "a" * 40, **values)

    def test_real_review_persists_immutable_scoped_approval(self):
        result = self.work()
        self.assertEqual(result["key"], approval_key(self.request, self.ref["sha256"]))
        approval = json.loads(self.objects[result["key"]][0])
        self.assertEqual(approval["reviewer"], "human-reviewer")
        self.assertEqual(approval["reviewReference"], self.ref)
        self.assertEqual(self.writes, [self.ref["key"], result["key"]])
        self.assertEqual(self.session_reads, 2)

    def test_reader_can_preview_but_cannot_approve(self):
        self.role = "techx-automation-plan-reader"
        result = self.work(approve=False)
        self.assertEqual(result["request"], self.request)
        with self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_no_approval_history_or_wrong_environment_never_writes(self):
        for history in ([], [dict(state="rejected", environments=[dict(id=45, name=ENVIRONMENT)])],
                        [dict(self.history[0], environments=[dict(id=45, name="unrelated")])],
                        self.history * 2, [dict(self.history[0], user=dict(id=46, login="bot", type="Bot"))]):
            self.history = history
            with self.subTest(history=history), self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_wrong_workflow_commit_ref_fork_or_rerun_cannot_approve(self):
        original = deepcopy(self.run)
        for key, value in (("run_attempt", 2), ("head_sha", "b" * 40), ("head_branch", "feature"),
                           ("event", "pull_request_target"), ("path", ".github/workflows/other.yml"),
                           ("head_repository", dict(full_name="attacker/techx-infra")), ("status", "completed"),
                           ("created_at", (self.now - timedelta(minutes=1)).isoformat())):
            self.run = dict(original, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_closed_expired_superseded_or_missing_schedule_blocks_approval(self):
        original = deepcopy(self.session)
        for change in (dict(status="DESTROYING"), dict(pendingPlan=dict(self.ref, attempt=2)),
                       dict(approvedBudgetUsd=80), dict(executionVersion="different")):
            self.session = dict(original, **change)
            with self.subTest(change=change), self.assertRaises(ValueError): self.work()
        self.session = original
        with self.assertRaises(ValueError): self.work(now=self.now + timedelta(hours=1))
        self.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_teardown_during_github_calls_is_rechecked(self):
        self.close_after_read = True
        with self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_retry_after_write_reuses_version_and_original_audit_timestamp(self):
        result = self.work()
        again = self.work(now=self.now + timedelta(minutes=2))
        self.assertEqual(again, result)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(json.loads(self.objects[result["key"]][0])["approvedAt"],
                         (self.now + timedelta(minutes=1)).isoformat())

    def test_different_reviewer_cannot_overwrite_existing_audit(self):
        self.work()
        self.history[0]["user"]["login"] = "another-reviewer"
        with self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 2)

    def test_api_errors_and_wrong_role_do_not_write(self):
        self.fail_put = True
        with self.assertRaises(RuntimeError): self.work()
        self.role = "techx-automation-publisher"
        with self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_request_version_checksum_and_prefix_are_validated(self):
        original = deepcopy(self.ref)
        for key, value in (("sha256", "e" * 64), ("key", "private/other/request.json"), ("attempt", True)):
            ref = dict(original, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_review(self.clients["s3"], "test-control-bucket", ref, self.now)
        self.wrong_version = True
        with self.assertRaises(ValueError): load_review(self.clients["s3"], "test-control-bucket", original, self.now)

    def test_summary_omits_sensitive_plan_values_and_rejects_deletes(self):
        plan = dict(resource_changes=[dict(mode="managed", address="module.eks.aws_eks_cluster.this",
            change=dict(actions=["create"], after=dict(password="must-not-leak"), after_sensitive=True))])
        result = summarize_changes(plan)
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertEqual(set(result[0]), {"address", "actions"})
        plan["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
        with self.assertRaises(ValueError): summarize_changes(plan)

    def test_republish_same_request_is_idempotent_but_different_summary_is_rejected(self):
        envelope = load_review(self.clients["s3"], "test-control-bucket", self.ref, self.now)
        self.assertEqual(publish_review(self.clients["s3"], "test-control-bucket", self.request,
                                       envelope["resourceChanges"], self.now, estimate=self.estimate), self.ref)
        with self.assertRaises(ValueError):
            publish_review(self.clients["s3"], "test-control-bucket", self.request, [], self.now, estimate=self.estimate)

    def test_changed_estimate_budget_or_duration_does_not_reach_review(self):
        for changes in (dict(approvedBudgetUsd=80), dict(upperBoundUsd=60.01), dict(maximumHours=4.0)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                publish_review(self.clients["s3"], "test-control-bucket", self.request, [], self.now,
                               estimate=dict(self.estimate, **changes))
        self.session["maximumHours"] = 5
        with self.assertRaises(ValueError): self.work()
        self.assertEqual(len(self.writes), 1)

    def test_worker_downloads_only_the_exact_approved_plan(self):
        approval = self.work()
        self.objects[self.request["plan"]["key"]] = (b"test plan", "plan-version")
        payload, permit = read_authorized_plan(self.clients["s3"], "test-control-bucket", self.ref, approval,
                                             self.session, self.context, self.now + timedelta(minutes=2))
        self.assertEqual(payload, b"test plan")
        self.assertEqual(permit["planVersionId"], "plan-version")
        self.objects[self.request["plan"]["key"]] = (b"changed bytes", "plan-version")
        with self.assertRaises(ValueError):
            read_authorized_plan(self.clients["s3"], "test-control-bucket", self.ref, approval,
                                 self.session, self.context, self.now + timedelta(minutes=2))

    def test_worker_rejects_approval_from_another_prefix_or_review(self):
        approval = self.work()
        for field, value in (("key", "private/untrusted/approval.json"), ("reviewSha256", "d" * 64),
                             ("stage", "edge"), ("attempt", 2)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                read_authorized_plan(self.clients["s3"], "test-control-bucket", self.ref,
                    dict(approval, **{field: value}), self.session, self.context, self.now + timedelta(minutes=2))

    def test_github_api_error_does_not_become_approval(self):
        def denied(endpoint): raise RuntimeError("GitHub unavailable")
        self.github.get = denied
        with self.assertRaises(RuntimeError): self.work()
        self.assertEqual(len(self.writes), 1)


if __name__ == "__main__": unittest.main()
