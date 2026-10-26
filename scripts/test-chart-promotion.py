from copy import deepcopy
from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

from chart_promotion import ColdPromotionChecks, PromotionPending, protection
from chart_selection import CHART_REPO
from plan_review_store import checksum

spec = importlib.util.spec_from_file_location("promotion_selection_support", Path(__file__).with_name("test-chart-selection.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.f = support.SelectionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        self.policy = dict(enforce_admins=dict(enabled=True), allow_force_pushes=dict(enabled=False),
            allow_deletions=dict(enabled=False), required_conversation_resolution=dict(enabled=True),
            required_status_checks=dict(strict=True, checks=[dict(context="manifests", app_id=15368)]),
            required_pull_request_reviews=dict(required_approving_review_count=1, dismiss_stale_reviews=True,
                require_last_push_approval=True, bypass_pull_request_allowances=dict(users=[], teams=[], apps=[])),
            restrictions=dict(users=[], teams=[], apps=[dict(id=12345, slug="techx-promotion")]))
        f.chart_data["branches/main/protection"] = self.policy
        f.execution["sourceAdmission"] = dict(promotionAuthority=dict(schemaVersion=1, repository=CHART_REPO,
            appId=12345, appSlug="techx-promotion", installationId=67890, policySha256=checksum(self.policy)))
        f.chart_data["git/ref/heads/main"]["object"]["sha"] = "a" * 40
        f.chart_data["git/commits/" + "f" * 40] = dict(sha="f" * 40, tree=dict(sha=f.selected_tree), parents=[dict(sha="a" * 40)])
        self.pull = f.chart_data["pulls/7"]
        self.pull.update(state="open", merged=False, draft=False, mergeable=True, mergeable_state="clean", html_url=f.proposal["pullRequest"])
        self.pull["base"].update(sha="a" * 40)
        self.pull["head"].update(ref=f.proposal["branch"])
        self.run = dict(id=55, head_sha="f" * 40, head_branch=f.proposal["branch"], event="pull_request", run_attempt=1,
            path=".github/workflows/verify.yml", repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO),
            status="completed", conclusion="success", created_at=f.now.isoformat())
        self.history = dict(total_count=1, workflow_runs=[deepcopy(self.run)])
        f.chart_data["actions/workflows/verify.yml/runs?head_sha=" + "f" * 40 + "&per_page=100"] = self.history
        f.chart_data["actions/runs/55"] = self.run
        self.checks = ColdPromotionChecks(f.clients, f.store, "control-bucket", f.execution, "exec-v1", clock=lambda: f.now)
        self.reads = []
        get = f.chart.get
        def read(endpoint):
            self.reads.append(endpoint)
            return get(endpoint)
        self.chart = SimpleNamespace(get=read)

    def evaluate(self, **kwargs):
        return self.checks.evaluate(self.f.source, self.chart, app_id=12345, app_slug="techx-promotion", **kwargs)

    def test_exact_reviewed_candidate_produces_read_only_reproducible_preview(self):
        result = self.evaluate()
        self.assertEqual(result, self.evaluate())
        self.assertEqual(result["headSha"], "f" * 40)
        self.assertEqual(result["baselineSha"], "a" * 40)
        self.assertEqual(result["candidateCi"]["runId"], 55)
        self.assertEqual(result["reviewers"], ["reviewer"])
        self.assertEqual(result["manifest"], self.f.manifest)
        self.assertEqual(result["releaseReference"], self.f.record["bootstrapRelease"])
        self.assertFalse(self.f.writes)
        self.assertEqual(self.f.etag, 1)

    def test_missing_or_changed_opening_app_policy_cannot_be_reauthorized_mid_session(self):
        original = deepcopy(self.checks.execution["sourceAdmission"])
        self.checks.execution.pop("sourceAdmission")
        with self.assertRaisesRegex(ValueError, "public promotion authority"): self.evaluate()
        self.checks.execution["sourceAdmission"] = original
        self.checks.execution["sourceAdmission"]["promotionAuthority"]["policySha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "opening authority"): self.evaluate()
        self.assertFalse(self.f.writes)

    def test_only_an_actual_dedicated_app_and_enforced_branch_policy_are_accepted(self):
        for app_id, slug in ((0, "techx"), (True, "techx"), (12345, ""), (12345, "other-app")):
            with self.subTest(app_id=app_id, slug=slug), self.assertRaises(ValueError):
                protection(self.chart, app_id, slug)
        self.policy["restrictions"]["users"] = [dict(login="owner")]
        with self.assertRaisesRegex(ValueError, "without bypass"): self.evaluate()

    def test_writable_main_for_another_bot_or_team_is_rejected(self):
        self.policy["restrictions"]["apps"].append(dict(id=88, slug="publisher"))
        with self.assertRaises(ValueError): self.evaluate()
        self.policy["restrictions"]["apps"].pop()
        self.policy["restrictions"]["teams"] = [dict(slug="admins")]
        with self.assertRaises(ValueError): self.evaluate()

    def test_review_bypass_or_unenforced_admins_cannot_authorize_promotion(self):
        self.policy["required_pull_request_reviews"]["bypass_pull_request_allowances"]["apps"] = [dict(id=12345)]
        with self.assertRaises(ValueError): self.evaluate()
        self.policy["required_pull_request_reviews"]["bypass_pull_request_allowances"]["apps"] = []
        self.policy["enforce_admins"]["enabled"] = False
        with self.assertRaises(ValueError): self.evaluate()

    def test_missing_strict_ci_wrong_check_app_or_stale_review_settings_are_rejected(self):
        mutations = [("required_status_checks", "strict", False), ("required_status_checks", "checks", [dict(context="manifests", app_id=99)]),
            ("required_pull_request_reviews", "require_last_push_approval", False),
            ("required_pull_request_reviews", "dismiss_stale_reviews", False),
            ("required_pull_request_reviews", "required_approving_review_count", 0)]
        for group, key, value in mutations:
            old = self.policy[group][key]
            self.policy[group][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.evaluate()
            self.policy[group][key] = old

    def test_wrong_head_base_branch_fork_or_already_merged_pr_is_rejected(self):
        for target, key, value in ((self.pull["head"], "sha", "0" * 40), (self.pull["base"], "sha", "0" * 40),
            (self.pull["head"], "ref", "release/other"), (self.pull["head"]["repo"], "full_name", "fork/chart"),
            (self.pull, "merged", True), (self.pull, "draft", True)):
            old = target[key]
            target[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.evaluate()
            target[key] = old

    def test_unknown_mergeability_or_review_block_is_pending_not_success(self):
        self.pull["mergeable"] = None
        with self.assertRaises(PromotionPending): self.evaluate()
        self.pull.update(mergeable=True, mergeable_state="blocked")
        with self.assertRaises(PromotionPending): self.evaluate()

    def test_conflict_or_behind_base_is_rejected(self):
        for state in ("dirty", "behind", "unstable"):
            self.pull["mergeable_state"] = state
            with self.subTest(state=state), self.assertRaises(ValueError): self.evaluate()

    def test_no_human_stale_approval_or_withdrawn_review_cannot_pass(self):
        reviews = self.f.chart_data["pulls/7/reviews?per_page=100"]
        for fields in (dict(commit_id="0" * 40), dict(state="DISMISSED"), dict(state="CHANGES_REQUESTED"), dict(user=dict(id=1, type="Bot", login="bot"))):
            previous = deepcopy(reviews[0])
            reviews[0].update(fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.evaluate()
            reviews[0] = previous

    def test_pending_or_not_yet_created_ci_never_uses_another_sha(self):
        self.run["status"] = "in_progress"
        with self.assertRaises(PromotionPending): self.evaluate()
        self.history.update(total_count=0, workflow_runs=[])
        with self.assertRaises(PromotionPending): self.evaluate()

    def test_new_failed_ci_cannot_reuse_an_older_success(self):
        later = dict(self.run, id=56, conclusion="failure")
        self.history["workflow_runs"].append(deepcopy(later))
        self.history["total_count"] = 2
        self.f.chart_data["actions/runs/56"] = later
        with self.assertRaisesRegex(ValueError, "never fall back"): self.evaluate()

    def test_forged_fork_run_wrong_head_event_or_incomplete_history_is_not_green(self):
        self.history["total_count"] = 101
        with self.assertRaisesRegex(ValueError, "Complete bounded"): self.evaluate()
        self.history["total_count"] = 1
        for key, value in (("head_sha", "0" * 40), ("event", "push"), ("repository", dict(full_name="fork/chart"))):
            old = self.run[key]
            self.run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.evaluate()
            self.run[key] = old

    def test_ci_created_before_this_session_is_rejected(self):
        self.run["created_at"] = (self.f.now - timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "session window"): self.evaluate()

    def test_hidden_template_change_or_wrong_full_tree_parent_is_rejected(self):
        commit = self.f.chart_data["git/commits/" + "f" * 40]
        commit["parents"] = [dict(sha="9" * 40)]
        with self.assertRaisesRegex(ValueError, "exact admitted parent"): self.evaluate()
        commit["parents"] = [dict(sha="a" * 40)]
        self.f.chart_data[f"git/trees/{self.f.selected_tree}?recursive=1"]["tree"][-1]["sha"] = "9" * 40
        with self.assertRaisesRegex(ValueError, "baseline"): self.evaluate()

    def test_pr_file_list_must_cover_the_entire_baseline_diff(self):
        self.pull["changed_files"] = 1
        self.f.chart_data["pulls/7/files?per_page=100"] = [dict(filename="releases/staging.json", status="modified")]
        with self.assertRaisesRegex(ValueError, "full tree and PR file diff"): self.evaluate()

    def test_off_destroyed_deadline_or_different_session_is_rejected(self):
        for status in ("OFF", "DESTROYED", "DESTROYING", "READY"):
            self.f.record["status"] = status
            with self.subTest(status=status), self.assertRaises(ValueError): self.evaluate()
        self.f.record["status"] = "PROVISIONING"
        self.f.now += timedelta(hours=5)
        with self.assertRaises(ValueError): self.evaluate()

    def test_idle_preview_and_private_owned_lease_use_the_same_candidate(self):
        idle = self.evaluate()
        owner = "arn:aws:ec2:us-east-1:058114477594:instance/i-1234567890abcdef0"
        self.f.record["lease"] = dict(owner=owner, operation="deploy", epoch=2, expiresAt=(self.f.now + timedelta(minutes=5)).isoformat())
        with self.assertRaisesRegex(ValueError, "idle session"): self.evaluate()
        self.assertEqual(self.evaluate(owner=owner, epoch=2), idle)
        for fields in (dict(owner=owner, epoch=3), dict(owner="cloud-computing-microservice/techx-infra:100:1", epoch=2)):
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.evaluate(**fields)
        self.f.record["lease"]["operation"] = "release"
        with self.assertRaises(ValueError): self.evaluate(owner=owner, epoch=2)

    def test_cleanup_fence_or_main_move_during_external_reads_is_rejected(self):
        self.f.race_cleanup = True
        with self.assertRaisesRegex(ValueError, "pre-GitOps"): self.evaluate()
        self.f.race_cleanup = False
        self.f.record["status"] = "PROVISIONING"
        self.f.race_main = True
        with self.assertRaisesRegex(ValueError, "main moved|main changed"): self.evaluate()

    def test_ci_review_or_branch_policy_change_during_verification_is_rejected(self):
        for boundary in ("ci", "review", "policy"):
            original = self.chart.get
            counts = {}
            def raced(endpoint):
                counts[endpoint] = counts.get(endpoint, 0) + 1
                result = original(endpoint)
                if boundary == "ci" and endpoint == "actions/runs/55" and counts[endpoint] == 2:
                    result["run_attempt"] = 2
                if boundary == "review" and endpoint == "pulls/7/reviews?per_page=100" and counts[endpoint] == 2:
                    result[0]["state"] = "DISMISSED"
                if boundary == "policy" and endpoint == "branches/main/protection" and counts[endpoint] == 2:
                    result["restrictions"]["users"] = [dict(login="owner")]
                return result
            self.chart.get = raced
            with self.subTest(boundary=boundary), self.assertRaises(ValueError): self.evaluate()
            self.chart.get = original
        self.assertFalse(self.f.writes)


if __name__ == "__main__": unittest.main()
