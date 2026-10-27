"""Active promotion policy against real versioned receipts and Git-tree fixtures."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

from automation_session import claim, release
from chart_selection import CHART_REPO
from plan_review_store import checksum
from ready_chart_promotion import ReadyPromotionChecks


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


support = load("ready_promotion_candidate_fixture", "test-ready-release-publish.py")


class ActivePromotionTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ReadyReleaseTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        p = self.p
        p.publish()
        self.chart = p.chart()
        self.chart.proposal.perform(p.source)
        self.f = f = p.f
        f.record = release(f.record, p.owner, f.record["lease"]["epoch"])
        self.policy = dict(enforce_admins=dict(enabled=True), allow_force_pushes=dict(enabled=False),
            allow_deletions=dict(enabled=False), required_conversation_resolution=dict(enabled=True),
            required_status_checks=dict(strict=True, checks=[dict(context="manifests", app_id=15368)]),
            required_pull_request_reviews=dict(required_approving_review_count=1, dismiss_stale_reviews=True,
                require_last_push_approval=True, bypass_pull_request_allowances=dict(users=[], teams=[], apps=[])),
            restrictions=dict(users=[], teams=[], apps=[dict(id=12345, slug="techx-promotion")]))
        p.publisher.execution["sourceAdmission"] = dict(promotionAuthority=dict(schemaVersion=1, repository=CHART_REPO,
            appId=12345, appSlug="techx-promotion", installationId=67890, policySha256=checksum(self.policy)))
        p.run.update(status="completed", conclusion="success", created_at=f.now.isoformat())
        p.jobs[1].update(status="completed", conclusion="success")
        self.pull = self.chart.pr
        self.pull.update(merged=False, changed_files=2, mergeable=True, mergeable_state="clean", user=dict(login="publisher"))
        self.review = [dict(state="APPROVED", submitted_at=f.now.isoformat(), commit_id=self.chart.branch,
            user=dict(type="User", id=31, login="reviewer"))]
        self.ci = dict(id=955, head_sha=self.chart.branch, head_branch=self.chart.proposal.branch, event="pull_request", run_attempt=1,
            path=".github/workflows/verify.yml", repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO),
            status="completed", conclusion="success", created_at=f.now.isoformat())
        read = self.chart.github.get
        def get(endpoint):
            if endpoint == "branches/main/protection": return deepcopy(self.policy)
            if endpoint == "pulls/7": return deepcopy(self.pull)
            if endpoint == "pulls/7/files?per_page=100": return [dict(filename=name, status="modified") for name in ("values-staging.yaml", "releases/staging.json")]
            if endpoint == "pulls/7/reviews?per_page=100": return deepcopy(self.review)
            if endpoint == "actions/runs/955": return deepcopy(self.ci)
            if endpoint == f"actions/workflows/verify.yml/runs?head_sha={self.chart.branch}&per_page=100": return dict(total_count=1, workflow_runs=[deepcopy(self.ci)])
            return read(endpoint)
        self.chart.github.get = get
        self.checks = ReadyPromotionChecks(f.clients, f.store, "control-bucket", p.publisher.execution, "execution-v1", platform=p.github, clock=lambda: f.now)

    def evaluate(self, **kw):
        return self.checks.evaluate(self.p.source, self.chart.github, app_id=12345, app_slug="techx-promotion", **kw)

    def reviews(self):
        from review_chart_promotion import ReadyPromotionReview, ENVIRONMENT, WORKFLOW
        self.infra_run = dict(id=100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha=self.checks.execution["sourceRevisions"]["techx-infra"], path=WORKFLOW, repository=dict(full_name="cloud-computing-microservice/techx-infra"),
            head_repository=dict(full_name="cloud-computing-microservice/techx-infra"), created_at=self.f.now.isoformat(), updated_at=self.f.now.isoformat())
        self.approvals = [dict(state="approved", environments=[dict(id=10, name=ENVIRONMENT)], user=dict(type="User", id=31, login="reviewer"))]
        def get(endpoint):
            if endpoint == "actions/runs/100": return deepcopy(self.infra_run)
            if endpoint == "actions/runs/100/approvals": return deepcopy(self.approvals)
            raise AssertionError(endpoint)
        infra = SimpleNamespace(get=get)
        args = (self.f.clients, self.f.store, "control-bucket", self.checks.execution, "execution-v1")
        extra = dict(run_id=100, platform=self.p.github, clock=lambda: self.f.now)
        return ReadyPromotionReview(*args, **extra), ReadyPromotionReview(*args, read_only=False, **extra), infra

    def grant(self):
        reader, writer, infra = self.reviews()
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-chart-reader/techx-chart-read-100"
        request = reader.preview(self.p.source, self.chart.github, infra, app_id=12345, app_slug="techx-promotion")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-promotion-approver/techx-promotion-approve-100"
        ref = writer.approve(self.p.source, self.chart.github, infra, request, checksum(request), app_id=12345, app_slug="techx-promotion")
        return ref, request, infra

    def test_active_preview_keeps_original_execution_and_binds_accepted_baseline(self):
        original = deepcopy(self.f.record)
        calls = len(self.chart.calls)
        result = self.evaluate()
        self.assertEqual(result["kind"], "ready-promotion")
        self.assertEqual(result["releaseId"], self.f.record["releaseCandidate"]["id"])
        self.assertEqual(result["acceptedBaseline"], original["acceptedRelease"])
        self.assertEqual(result["sourceRevisions"], original["sourceRevisions"])
        self.assertEqual(result["releaseReference"], original["releaseCandidate"]["manifestReference"])
        self.assertEqual(result["manifest"]["commit"], self.p.source_sha)
        self.assertEqual(result["reviewers"], ["reviewer"])
        self.assertEqual(self.f.record, original)
        self.assertEqual(len(self.chart.calls), calls)

    def test_failed_or_running_source_and_stale_platform_cannot_promote(self):
        for obj, field, changed in ((self.p.run, "status", "in_progress"), (self.p.run, "run_attempt", 2),
                (self.p.jobs[0], "conclusion", "failure"), (self.p.jobs[1], "status", "in_progress"), (self.p.jobs[0], "id", 999)):
            original = obj[field]; obj[field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError): self.evaluate()
            obj[field] = original
        self.p.main = "0" * 40
        with self.assertRaises(ValueError): self.evaluate()

    def test_private_checks_require_this_executor_release_lease_and_current_epoch(self):
        owner = "arn:aws:ec2:us-east-1:058114477594:instance/i-0123456789abcdef0"
        self.f.record = claim(self.f.record, owner, "release", now=self.f.now)
        epoch = self.f.record["lease"]["epoch"]
        with self.assertRaises(ValueError): self.evaluate()
        self.assertEqual(self.evaluate(owner=owner, epoch=epoch)["kind"], "ready-promotion")
        for changed_owner, changed_epoch in ((self.p.owner, epoch), (owner, epoch + 1)):
            with self.assertRaises(ValueError): self.evaluate(owner=changed_owner, epoch=changed_epoch)
        self.f.record["lease"]["operation"] = "deploy"
        with self.assertRaises(ValueError): self.evaluate(owner=owner, epoch=epoch)

    def test_candidate_ci_review_and_full_tree_remain_mandatory(self):
        self.ci["conclusion"] = "failure"
        with self.assertRaises(ValueError): self.evaluate()
        self.ci["conclusion"] = "success"
        self.review[0]["commit_id"] = "0" * 40
        with self.assertRaises(ValueError): self.evaluate()
        self.review[0]["commit_id"] = self.chart.branch
        self.chart.bad_tree = True
        with self.assertRaises(ValueError): self.evaluate()

    def test_closed_window_cleanup_or_changed_baseline_blocks_preview(self):
        original = deepcopy(self.f.record)
        for fields in (dict(status="DESTROYING"), dict(status="TESTING"), dict(acceptedRelease=dict(original["acceptedRelease"], platformSha="0" * 40))):
            self.f.record = dict(deepcopy(original), **fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.evaluate()
        self.f.record = original
        self.f.now += timedelta(hours=4)
        with self.assertRaises(ValueError): self.evaluate()

    def test_mid_review_candidate_change_is_not_a_reusable_green_preview(self):
        get = self.chart.github.get
        count = []
        def changed(endpoint):
            value = get(endpoint)
            if endpoint == "pulls/7/reviews?per_page=100":
                count.append(endpoint)
                if len(count) == 2: self.f.record["releaseCandidate"]["proposalReference"]["sha256"] = "0" * 64
            return value
        self.chart.github.get = changed
        with self.assertRaises(ValueError): self.evaluate()

    def test_real_review_writes_a_separate_active_grant_that_cold_consumer_rejects(self):
        from review_chart_promotion import read_promotion_approval
        ref, request, _ = self.grant()
        grant = read_promotion_approval(self.f.clients["s3"], "control-bucket", self.checks.execution, "execution-v1", ref, self.f.now, kind="ready-promotion")
        self.assertEqual(grant["kind"], "ready-promotion-approval")
        self.assertEqual(grant["request"], request)
        self.assertEqual(request["acceptedBaseline"], self.f.record["acceptedRelease"])
        self.assertNotIn("lease", self.f.record)
        with self.assertRaises(ValueError):
            read_promotion_approval(self.f.clients["s3"], "control-bucket", self.checks.execution, "execution-v1", ref, self.f.now)

    def test_active_grant_consumption_requires_real_completed_review_and_private_lease(self):
        from review_chart_promotion import authorize_promotion
        ref, _, infra = self.grant()
        owner = "arn:aws:ec2:us-east-1:058114477594:instance/i-0123456789abcdef0"
        self.f.record = claim(self.f.record, owner, "release", now=self.f.now)
        epoch = self.f.record["lease"]["epoch"]
        def consume():
            return authorize_promotion(self.checks, self.p.source, self.chart.github, infra, ref,
                owner=owner, epoch=epoch, app_id=12345, app_slug="techx-promotion")
        with self.assertRaises(ValueError): consume()
        self.infra_run.update(status="completed", conclusion="success")
        self.assertEqual(consume()["reference"], ref)
        self.approvals[0]["state"] = "rejected"
        with self.assertRaises(ValueError): consume()

    def test_preview_role_and_missing_human_review_cannot_issue_active_grant(self):
        reader, writer, infra = self.reviews()
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-chart-reader/techx-chart-read-100"
        request = reader.preview(self.p.source, self.chart.github, infra, app_id=12345, app_slug="techx-promotion")
        with self.assertRaises(ValueError): reader.approve(self.p.source, self.chart.github, infra, request, checksum(request), app_id=12345, app_slug="techx-promotion")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-promotion-approver/techx-promotion-approve-100"
        self.approvals.clear()
        with self.assertRaises(ValueError): writer.approve(self.p.source, self.chart.github, infra, request, checksum(request), app_id=12345, app_slug="techx-promotion")


if __name__ == "__main__": unittest.main()
