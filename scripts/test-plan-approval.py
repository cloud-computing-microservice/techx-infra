from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from lifecycle_coordinator import initial_record
from plan_approval import authorize_apply, create_request, issue_approval


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
        sources = {repo: "a" * 40 for repo in ("techx-infra", "techx-chart", "techx-platform")}
        self.session = initial_record("staging-test123", sources, 4, "owner@example.invalid", "deploy-window",
                                      self.now, approved_budget_usd=60)
        self.context = dict(sourceRevisions=deepcopy(sources), approvedBudgetUsd=60,
                            deadline=self.session["deadline"], cleanupStartAt=self.session["cleanupStartAt"],
                            configSha256="b" * 64, providerLockSha256="c" * 64, terraformVersion="1.15.5",
                            backendBucket="techx-control-test", backendKey="staging/terraform.tfstate",
                            stateLineage="12345678-1234-1234-1234-123456789abc", stateSerial=0,
                            executorRoleArn="arn:aws:iam::058114477594:role/techx-automation-lifecycle-executor",
                            callerPublicCidr="8.8.8.8/32")
        self.payload = b"test saved plan, never used with Terraform"
        self.request = self.request_for()
        self.approval = issue_approval(self.request, reviewer="reviewer-login",
                                      review_run="https://github.com/cloud-computing-microservice/techx-infra/actions/runs/123/attempts/1",
                                      now=self.now + timedelta(minutes=1))

    def request_for(self, stage="foundation", **kwargs):
        return create_request(self.session, stage, 1, self.payload, "plan-v1", self.context,
                              expires_at=(self.now + timedelta(hours=1)).isoformat(), now=self.now, **kwargs)

    def authorize(self, **kwargs):
        args = dict(session=self.session, request=self.request, approval=self.approval, context=self.context,
                    plan_bytes=self.payload, plan_object_version="plan-v1", now=self.now + timedelta(minutes=2))
        args.update(kwargs)
        return authorize_apply(**args)

    def test_exact_plan_approval_authorizes_both_deploy_stages(self):
        for stage in ("foundation", "edge"):
            self.request = self.request_for(stage)
            self.approval = issue_approval(self.request, reviewer="operator",
                review_run="https://github.com/cloud-computing-microservice/techx-infra/actions/runs/42", now=self.now)
            self.assertEqual(self.authorize()["stage"], stage)

    def test_changed_observed_context_requires_replan(self):
        mutations = dict(configSha256="d" * 64, providerLockSha256="e" * 64, terraformVersion="1.15.4",
                         backendBucket="another-state-bucket", backendKey="other/terraform.tfstate", stateSerial=1,
                         stateLineage="87654321-4321-4321-4321-cba987654321", callerPublicCidr="1.1.1.1/32",
                         executorRoleArn="arn:aws:iam::058114477594:role/other", approvedBudgetUsd=80,
                         sourceRevisions=dict(self.context["sourceRevisions"], **{"techx-chart": "f" * 40}))
        for key, value in mutations.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.authorize(context=dict(self.context, **{key: value}))

    def test_tampered_plan_or_reuploaded_same_bytes_requires_new_review(self):
        for kwargs in (dict(plan_bytes=b"different plan"), dict(plan_object_version="plan-v2"),
                       dict(plan_object_version="null"), dict(plan_bytes=b"")):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.authorize(**kwargs)

    def test_old_approval_cannot_authorize_changed_attempt_or_stage(self):
        for key, value in (("attempt", 2), ("stage", "edge"), ("expiresAt", (self.now + timedelta(minutes=30)).isoformat())):
            request = deepcopy(self.request)
            request[key] = value
            if key in ("attempt", "stage"):
                request["plan"]["key"] = f"private/staging/staging-test123/plans/{request['stage']}/{request['attempt']}/saved.tfplan"
            with self.subTest(key=key), self.assertRaises(ValueError): self.authorize(request=request)

    def test_expired_or_cleanup_session_never_authorizes(self):
        with self.assertRaises(ValueError): self.authorize(now=self.now + timedelta(hours=1))
        for status in ("READY", "TESTING", "FAILED_NEEDS_CLEANUP", "DESTROYING", "DESTROYED"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.authorize(session=dict(self.session, status=status))
        session = dict(self.session, approvedBudgetUsd=80)
        with self.assertRaises(ValueError): self.authorize(session=session)
        with self.assertRaises(ValueError): self.authorize(session=dict(self.session, sessionId="staging-other123"))

    def test_review_must_expire_before_cleanup_and_reject_future_audit(self):
        request = deepcopy(self.request)
        request["expiresAt"] = self.session["deadline"]
        with self.assertRaises(ValueError): self.authorize(request=request)
        approval = dict(self.approval, approvedAt=(self.now + timedelta(minutes=3)).isoformat())
        with self.assertRaises(ValueError): self.authorize(approval=approval)
        with self.assertRaises(ValueError): self.authorize(approval=dict(self.approval, reviewRun="https://example.com/123"))

    def test_requests_are_snapshots_not_mutable_references(self):
        self.context["sourceRevisions"]["techx-chart"] = "f" * 40
        self.assertEqual(self.request["context"]["sourceRevisions"]["techx-chart"], "a" * 40)
        self.request["attempt"] = 2
        self.assertEqual(self.approval["request"]["attempt"], 1)

    def test_invalid_context_or_scope_rejected_before_approval(self):
        for key, value in (("stateSerial", True), ("callerPublicCidr", "0.0.0.0/0"),
                           ("approvedBudgetUsd", float("nan")), ("sourceRevisions", {})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                create_request(self.session, "foundation", 1, self.payload, "plan-v1", dict(self.context, **{key: value}),
                               expires_at=(self.now + timedelta(hours=1)).isoformat(), now=self.now)
        with self.assertRaises(ValueError): self.request_for("destroy")


if __name__ == "__main__": unittest.main()
