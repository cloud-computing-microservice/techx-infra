"""Real prepared rollback evidence, accepted Git blobs and fresh PR review/CI."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from chart_selection import CHART_REPO
from promotion_authority import admitted
from rollback_chart_promotion import RollbackPromotionChecks

spec = importlib.util.spec_from_file_location("rollback_review_controller_fixture", Path(__file__).with_name("test-rollback-prepare-controller.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.c = support.ControllerTests()
        self.c.setUp(); self.addCleanup(self.c.doCleanups)
        self.c.controller.observe(); self.c.complete()
        self.p, self.f, self.d = self.c.p, self.c.f, self.c.d
        p, f = self.p, self.f
        self.chart = p.chart
        self.chart.pr.update(merged=False, mergeable=True, mergeable_state="clean", changed_files=2,
            user=dict(type="Bot", login="techx-promotion[bot]"))
        self.reviews = [dict(id=987, state="APPROVED", commit_id=self.chart.branch,
            submitted_at=f.now.isoformat(), user=dict(type="User", id=988, login="reviewer"))]
        self.ci = dict(id=989, head_sha=self.chart.branch, head_branch=self.chart.pr["head"]["ref"],
            event="pull_request", path=".github/workflows/verify.yml", run_attempt=1, status="completed", conclusion="success",
            repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO), created_at=f.now.isoformat())
        self.history = [self.ci]
        self.mutate = None
        get = self.chart.github.get
        def chart_get(endpoint):
            if self.mutate: self.mutate(endpoint)
            if endpoint == "pulls/7": return deepcopy(self.chart.pr)
            if endpoint == "pulls/7/files?per_page=100": return [dict(filename=name, status="modified") for name in ("values-staging.yaml", "releases/staging.json")]
            if endpoint == "pulls/7/reviews?per_page=100": return deepcopy(self.reviews)
            if endpoint.startswith("actions/workflows/verify.yml/runs?"): return dict(total_count=len(self.history), workflow_runs=deepcopy(self.history))
            if endpoint.startswith("actions/runs/"):
                return deepcopy(next(row for row in self.history if row["id"] == int(endpoint.rsplit("/", 1)[1])))
            return get(endpoint)
        self.chart.github.get = chart_get
        self.checks = RollbackPromotionChecks(f.clients, f.store, self.d.bucket, p.execution, self.d.version,
            infra=p.r.a.p.r.h.infra, clock=lambda: f.now)
        policy = admitted(p.execution)
        self.arguments = dict(app_id=policy["appId"], app_slug=policy["appSlug"])

    def evaluate(self): return self.checks.evaluate(self.p.runtime.source, self.chart.github, **self.arguments)

    def test_read_only_preview_binds_original_failure_baseline_proposal_fresh_ci_and_review(self):
        record = deepcopy(self.f.record)
        with patch.object(self.f.store, "write", side_effect=AssertionError("Preview must not mutate session")), \
                patch.object(self.chart.github, "post", side_effect=AssertionError("Preview must not write Git")):
            result = self.evaluate()
        self.assertEqual(result["kind"], "rollback-promotion")
        self.assertEqual(result["intent"], record["rollbackAttempt"]["intent"])
        self.assertEqual(result["proposalReference"], record["rollbackAttempt"]["proposalReference"])
        self.assertEqual(result["manifest"]["commit"], record["acceptedRelease"]["platformSha"])
        self.assertEqual(result["baselineSha"], record["rollbackAttempt"]["intent"]["failedChartSha"])
        self.assertEqual(result["reviewers"], ["reviewer"])
        self.assertEqual(result["candidateCi"]["runId"], 989)
        self.assertEqual(self.f.record, record)
        self.assertFalse(self.chart.pr["merged"])

    def test_failed_release_review_does_not_approve_the_rollback_head(self):
        self.reviews[0]["commit_id"] = self.p.intent["failedChartSha"]
        with self.assertRaises(ValueError): self.evaluate()

    def test_latest_failed_or_running_ci_cannot_reuse_an_older_green_run(self):
        self.history.append(dict(deepcopy(self.ci), id=990, conclusion="failure"))
        with self.assertRaises(ValueError): self.evaluate()
        self.history[-1].update(status="in_progress", conclusion=None)
        with self.assertRaises(ValueError): self.evaluate()

    def test_unrelated_template_change_or_wrong_accepted_digest_is_rejected(self):
        tree = self.chart.trees[self.chart.commits[self.chart.branch]["tree"]["sha"]]
        tree["templates/deployment.yaml"]["sha"] = "0" * 40
        with self.assertRaises(ValueError): self.evaluate()

    def test_expiry_or_missing_terminal_manager_blocks_review(self):
        self.c.build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        with self.assertRaises(ValueError): self.evaluate()
        self.c.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.f.now += timedelta(minutes=31)
        with self.assertRaises(ValueError): self.evaluate()

    def test_unknown_ssm_result_is_not_replaced_by_a_green_manager(self):
        self.f.statuses = ["InProgress"]
        with self.assertRaises(ValueError): self.evaluate()

    def test_changed_main_or_opening_branch_policy_blocks_the_proposal(self):
        self.chart.main = "0" * 40
        with self.assertRaises(ValueError): self.evaluate()
        self.chart.main = self.p.intent["failedChartSha"]
        self.p.policy["allow_force_pushes"]["enabled"] = True
        with self.assertRaises(ValueError): self.evaluate()

    def test_revoked_review_or_cleanup_race_during_external_reads_is_rejected(self):
        count = 0
        def revoke(endpoint):
            nonlocal count
            if endpoint == "pulls/7/reviews?per_page=100":
                count += 1
                if count == 2: self.reviews[0]["state"] = "DISMISSED"
        self.mutate = revoke
        with self.assertRaises(ValueError): self.evaluate()
        self.mutate = lambda endpoint: self.f.record.update(status="DESTROYING") if endpoint == "pulls/7" else None
        with self.assertRaises(ValueError): self.evaluate()
        self.assertEqual(self.f.record["status"], "DESTROYING")

    def test_preview_cannot_be_used_as_private_executor_merge_authority(self):
        with self.assertRaises(ValueError): self.checks.evaluate(self.p.runtime.source, self.chart.github,
            owner=self.p.owner, epoch=self.p.epoch, **self.arguments)


if __name__ == "__main__": unittest.main()
