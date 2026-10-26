from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
from pathlib import Path
import unittest

from chart_selection import CHART_REPO, INFRA_REPO
from orchestrate_lifecycle import OPEN_WORKFLOW, WORKFLOW, resolve
from plan_review_store import encoded

spec = importlib.util.spec_from_file_location("chart_callback_handoff_support", Path(__file__).with_name("test-promotion-handoff.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class ChartCallbackTests(unittest.TestCase):
    def setUp(self):
        self.p = support.PromotionHandoffTests()
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)
        self.d, self.f = self.p.d, self.p.f
        self.source_sha = self.d.execution["sourceRevisions"]["techx-infra"]
        self.d.execution["sourceAdmission"] = dict(workflowRun=f"https://github.com/{INFRA_REPO}/actions/runs/200")
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.d.execution), self.d.version
        self.own = dict(id=900, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha=self.source_sha, path=WORKFLOW, repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        self.root = dict(self.own, id=200, status="completed", conclusion="success", path=OPEN_WORKFLOW)
        get = self.d.clients["s3"].get_object
        self.pointer = dict(sessionId=self.f.record["sessionId"], executionVersion=self.d.version)
        self.d.clients["s3"].get_object = lambda **kw: dict(VersionId="pointer-v1", Body=io.BytesIO(encoded(self.pointer))) \
            if kw["Key"] == "control/staging/current-execution.json" else get(**kw)
        self.p.h.runs.update({900: self.own, 200: self.root})
        self.p.pull["merge_commit_sha"] = "e" * 40
        self.source = dict(id=90, status="completed", conclusion="success", run_attempt=1, event="pull_request",
            path=".github/workflows/verify.yml", head_branch=self.p.proposal["branch"], head_sha=self.p.proposal["headSha"],
            repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO), created_at=self.f.now.isoformat(),
            pull_requests=[dict(number=7, head=dict(ref=self.p.proposal["branch"], sha=self.p.proposal["headSha"]),
                base=dict(ref="main", sha=self.p.proposal["baselineSha"]))])
        get_chart = self.p.engine.chart.get
        self.p.engine.chart.get = lambda path: deepcopy(self.source) if path == "actions/runs/90" else get_chart(path)

    def resolve(self, inputs=None):
        return resolve(self.d.clients, self.d.store, self.d.bucket, self.p.h.api,
            run_id="900", source_sha=self.source_sha, event_name="workflow_dispatch",
            event=dict(inputs=inputs if inputs is not None else dict(chart_run_id="90")), chart=self.p.engine.chart)

    def test_exact_chart_candidate_hint_binds_current_private_execution_without_dispatch(self):
        result = self.resolve()
        self.assertEqual(result["executionVersion"], self.d.version)
        self.assertFalse(result["boundFailure"])
        self.assertFalse(self.p.h.posts)
        self.assertFalse(self.p.h.f.started)

    def test_optional_github_defaults_keep_existing_child_wakeups_compatible(self):
        inputs = dict(session_id=self.f.record["sessionId"], execution_version=self.d.version, chart_run_id="")
        self.assertEqual(self.resolve(inputs)["executionVersion"], self.d.version)
        inputs = dict(session_id="", execution_version="", chart_run_id="90")
        self.assertEqual(self.resolve(inputs)["executionVersion"], self.d.version)

    def test_mixed_inputs_unknown_fields_or_invalid_run_ids_cannot_choose_work(self):
        for inputs in (dict(chart_run_id="90", session_id=self.f.record["sessionId"]), dict(chart_run_id="90", stage="apply"),
                       dict(chart_run_id="90", approved_budget_usd="60"), dict(chart_run_id=90),
                       dict(chart_run_id="90/../91"), dict(chart_run_id="0"), {}):
            with self.subTest(inputs=inputs), self.assertRaises(ValueError): self.resolve(inputs)
        self.assertFalse(self.p.h.f.started)

    def test_unrelated_fork_sha_pr_branch_or_workflow_is_a_noop(self):
        original = deepcopy(self.source)
        for fields in (dict(status="in_progress"), dict(head_sha="0" * 40), dict(head_branch="unrelated"),
                       dict(repository=dict(full_name="fork/chart")), dict(head_repository=dict(full_name="fork/chart")),
                       dict(path=".github/workflows/post-deploy.yml"), dict(event="issue_comment"), dict(pull_requests=[{}, {}])):
            self.source = dict(original, **fields)
            self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))
        self.source = original
        self.source["pull_requests"][0]["number"] = 8
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))

    def test_empty_review_associations_still_require_exact_branch_sha_and_current_proposal(self):
        self.source.update(event="pull_request_review", path=".github/workflows/chart-review-event.yml", pull_requests=[])
        self.assertFalse(self.resolve()["boundFailure"])
        self.source["head_branch"] = "other-release"
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))
        self.source["head_branch"] = self.p.proposal["branch"]
        self.source["head_sha"] = "0" * 40
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))

    def test_old_run_or_other_session_proposal_cannot_target_the_new_session(self):
        self.source["created_at"] = (self.f.now - timedelta(hours=1)).isoformat()
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))
        self.source["created_at"] = self.f.now.isoformat()
        self.p.pull["head"]["sha"] = "0" * 40
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))

    def test_review_synthetic_merge_is_only_a_wakeup_and_can_resume_after_observation_timeout(self):
        self.p.pull["mergeable_state"] = "blocked"
        self.assertEqual(self.p.observe()["waiting"], "OBSERVATION_LIMIT")
        self.source.update(event="pull_request_review", path=".github/workflows/chart-review-event.yml", head_sha="e" * 40)
        self.assertFalse(self.resolve()["boundFailure"])
        self.p.pull["mergeable_state"] = "clean"
        self.p.observe()
        self.assertEqual(len(self.p.h.posts), 1)
        self.assertEqual(self.p.h.posts[0]["operation"], "review-promotion")
        self.assertFalse(self.p.h.f.started)

    def test_review_run_cannot_substitute_candidate_ci_or_another_synthetic_merge(self):
        self.source.update(event="pull_request_review", path=".github/workflows/chart-review-event.yml", head_sha="e" * 40)
        self.assertFalse(self.resolve()["boundFailure"])
        self.p.candidate.update(status="in_progress", conclusion=None)
        self.p.observe()
        self.assertFalse(self.p.h.posts)
        self.source["head_sha"] = "0" * 40
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))

    def test_exact_main_ci_resumes_selection_after_a_previous_wait(self):
        self.p.pull.update(state="closed", merged=True, merge_commit_sha="b" * 40)
        self.assertEqual(self.p.observe()["waiting"], "OBSERVATION_LIMIT")
        self.source.update(event="push", head_sha="b" * 40, head_branch="main", pull_requests=[])
        self.assertFalse(self.resolve()["boundFailure"])
        self.p.main_history = dict(total_count=1, workflow_runs=[deepcopy(self.source)])
        self.p.observe()
        self.assertEqual(self.p.h.posts[0]["operation"], "select-chart")
        self.source["head_sha"] = "c" * 40
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))

    def test_late_failed_or_rerun_hint_does_not_override_latest_ci(self):
        self.source.update(conclusion="failure", run_attempt=2)
        self.assertFalse(self.resolve()["boundFailure"])
        self.p.observe()
        self.assertEqual(self.p.h.posts[0]["operation"], "review-promotion")

    def test_closed_off_or_preproposal_session_never_dispatches(self):
        self.f.record["status"] = "DESTROYED"
        self.assertEqual(self.resolve(), dict(waiting="NO_ACTIVE_SESSION"))
        self.f.record["status"] = "PROVISIONING"
        self.pointer = dict(status="OFF")
        self.assertEqual(self.resolve(), dict(waiting="NO_ACTIVE_SESSION"))
        self.pointer = dict(sessionId=self.f.record["sessionId"], executionVersion=self.d.version)
        self.f.record.pop("bootstrapProposal")
        self.assertEqual(self.resolve(), dict(waiting="UNRELATED_CALLBACK"))
        self.assertFalse(self.p.h.posts)


if __name__ == "__main__": unittest.main()
