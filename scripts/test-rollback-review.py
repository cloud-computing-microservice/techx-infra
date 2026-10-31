"""Protected rollback handoff and a fresh, original-expiry-bounded human grant."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import unittest

from automation_session import utc
from chart_selection import INFRA_REPO
from plan_review_store import checksum, encoded
from review_chart_promotion import (ENVIRONMENT, WORKFLOW, RollbackPromotionReview, read_promotion_approval)
from rollback_prepare_controller import RollbackHandoffDriver
from workflow_handoff import target_for, title

spec = importlib.util.spec_from_file_location("protected_rollback_review_fixture", Path(__file__).with_name("test-rollback-chart-promotion.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ProtectedReviewTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ReviewTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        self.f, self.d = self.p.f, self.p.d
        self.infra = self.p.checks.infra
        self.run = dict(id=1100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha=self.d.execution["sourceRevisions"]["techx-infra"], path=WORKFLOW,
            repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO),
            created_at=self.f.now.isoformat(), updated_at=self.f.now.isoformat())
        self.approvals = [dict(state="approved", environments=[dict(id=10, name=ENVIRONMENT)],
            user=dict(type="User", id=31, login="reviewer"))]
        self.dispatches = []
        get = self.infra.get
        def infra_get(endpoint):
            if endpoint == "actions/runs/1100": return deepcopy(self.run)
            if endpoint == "actions/runs/1101": return dict(deepcopy(self.run), id=1101, run_attempt=2)
            if endpoint == "actions/runs/1100/approvals": return deepcopy(self.approvals)
            return get(endpoint)
        def dispatch(target, dispatch_id):
            self.dispatches.append(deepcopy(target))
            self.assertEqual(target["operation"], "review-rollback")
            self.run["display_title"] = title(dispatch_id)
            return dict(workflow_run_id=1100, run_url=f"https://api.github.com/repos/{INFRA_REPO}/actions/runs/1100",
                html_url=f"https://github.com/{INFRA_REPO}/actions/runs/1100")
        self.infra.get, self.infra.dispatch = infra_get, dispatch
        self.driver = RollbackHandoffDriver(self.d, {INFRA_REPO: self.infra})
        self.target = target_for("review-rollback", self.f.record, self.d.execution)
        self.driver.start(self.target)
        self.dispatch_id = self.f.record["workflowHandoff"]["id"]
        args = self.f.clients, self.f.store, self.d.bucket, self.d.execution, self.d.version
        kwargs = dict(infra=self.infra, run_id=1100, dispatch_id=self.dispatch_id, clock=lambda: self.f.now)
        self.reader = RollbackPromotionReview(*args, **kwargs)
        self.writer = RollbackPromotionReview(*args, read_only=False, **kwargs)

    def preview(self):
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-chart-reader/techx-chart-read-1100"
        return self.reader.preview(self.p.p.runtime.source, self.p.chart.github, self.infra, **self.p.arguments)

    def approve(self, request):
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-promotion-approver/techx-promotion-approve-1100"
        return self.writer.approve(self.p.p.runtime.source, self.p.chart.github, self.infra, request, checksum(request), **self.p.arguments)

    def read(self, reference):
        return read_promotion_approval(self.f.clients["s3"], self.d.bucket, self.d.execution, self.d.version,
            reference, self.f.now, kind="rollback-promotion")

    def test_exact_protected_handoff_and_human_grant_do_not_merge_or_mark_ready(self):
        self.driver.start(self.target)
        request = self.preview()
        reference = self.approve(request)
        value = self.read(reference)
        self.assertEqual(value["kind"], "rollback-promotion-approval")
        self.assertEqual(value["request"], request)
        self.assertEqual(value["reviewer"], "reviewer")
        self.assertLessEqual(utc(value["expiresAt"]), utc(request["intent"]["expiresAt"]))
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], self.p.p.baseline)
        self.assertFalse(self.p.chart.pr["merged"])
        self.assertNotIn("lease", self.f.record)
        with self.assertRaises(ValueError): read_promotion_approval(self.f.clients["s3"], self.d.bucket,
            self.d.execution, self.d.version, reference, self.f.now)

    def test_review_handoff_refuses_unknown_manager_or_nonrollback_target(self):
        self.p.c.build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        with self.assertRaises(ValueError): self.driver.start(self.target)
        with self.assertRaises(ValueError): self.driver.start(dict(self.target, operation="review-promotion"))
        self.assertEqual(len(self.dispatches), 1)

    def test_approval_late_in_original_window_cannot_buy_fifteen_more_minutes(self):
        self.f.now += timedelta(minutes=20)
        request = self.preview()
        value = self.read(self.approve(request))
        self.assertEqual(value["expiresAt"], request["intent"]["expiresAt"])
        self.assertLess(utc(value["expiresAt"]), self.f.now + timedelta(minutes=15))

    def test_reader_or_missing_environment_approval_cannot_issue_grant(self):
        request = self.preview()
        with self.assertRaises(ValueError): self.reader.approve(self.p.p.runtime.source, self.p.chart.github,
            self.infra, request, checksum(request), **self.p.arguments)
        self.approvals.clear()
        with self.assertRaises(ValueError): self.approve(request)

    def test_changed_pr_approval_after_preview_does_not_issue_a_grant(self):
        request = self.preview()
        self.p.reviews[0]["state"] = "DISMISSED"
        with self.assertRaises(ValueError): self.approve(request)

    def test_changed_protected_run_identity_or_predecessor_cannot_preview(self):
        self.run["run_attempt"] = 2
        with self.assertRaises(ValueError): self.preview()
        self.run["run_attempt"] = 1
        self.reader.run_id = "1101"
        with self.assertRaises(ValueError): self.preview()
        self.reader.run_id = "1100"
        self.f.record["deploymentTask"]["commandId"] = "00000000-0000-0000-0000-000000000000"
        with self.assertRaises(Exception): self.preview()

    def test_missing_dispatch_id_or_receipt_cannot_use_manual_review_bypass(self):
        self.reader.dispatch_id = ""
        with self.assertRaises(ValueError): self.preview()
        self.reader.dispatch_id = self.dispatch_id
        self.reader.run_id = "1101"
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-chart-reader/techx-chart-read-1101"
        with self.assertRaises(ValueError): self.reader.guard()

    def test_grant_reader_rejects_an_expiry_beyond_its_original_rollback_intent(self):
        request = self.preview(); reference = self.approve(request)
        data, version = self.f.objects[reference["key"]]
        value = json.loads(data)
        self.f.now += timedelta(minutes=20)
        # Model a different privileged writer; recomputing a checksum cannot
        # turn an extended intent into the originally authorized rollback.
        value.update(approvedAt=self.f.now.isoformat(), expiresAt=(self.f.now + timedelta(minutes=14)).isoformat())
        self.f.objects[reference["key"]] = encoded(value), version
        changed = dict(reference, sha256=checksum(value))
        with self.assertRaises(ValueError): self.read(changed)


if __name__ == "__main__": unittest.main()
