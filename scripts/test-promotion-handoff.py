"""Real controller/handoff transitions with controlled service observations."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from botocore.exceptions import ClientError
from chart_selection import CHART_REPO, INFRA_REPO
from lifecycle_dispatch import environment, operation
from plan_review_store import checksum, encoded
from review_chart_promotion import ENVIRONMENT, promotion_index_key

spec = importlib.util.spec_from_file_location("promotion_orchestration_support", Path(__file__).with_name("test-lifecycle-orchestration.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class PromotionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.o = support.OrchestrationTests()
        self.o.setUp()
        self.addCleanup(self.o.doCleanups)
        self.h, self.d, self.f, self.engine = self.o.h, self.o.d, self.o.f, self.o.engine
        self.h.f.applied()
        self.o.publish_cold_evidence(299)
        source = self.d.execution["sourceRevisions"]
        self.proposal = dict(schemaVersion=1, sessionId=self.f.record["sessionId"], executionVersion=self.d.version,
            sourceRevisions=source, releaseReference=self.f.record["bootstrapRelease"], baselineSha=source["techx-chart"],
            headSha="f" * 40, treeSha="2" * 40, branch="release/bootstrap-" + self.f.record["sessionId"],
            prNumber=7, pullRequest=f"https://github.com/{CHART_REPO}/pull/7")
        self.o.store_artifact("bootstrapProposal", "proposals", self.proposal)
        self.pull = dict(number=7, html_url=self.proposal["pullRequest"], state="open", merged=False, draft=False,
            changed_files=2, mergeable=True, mergeable_state="clean", base=dict(ref="main", sha=source["techx-chart"], repo=dict(full_name=CHART_REPO)),
            head=dict(sha="f" * 40, ref=self.proposal["branch"], repo=dict(full_name=CHART_REPO)))
        self.candidate = dict(id=55, head_sha="f" * 40, head_branch=self.proposal["branch"], event="pull_request", run_attempt=1,
            path=".github/workflows/verify.yml", repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO),
            status="completed", conclusion="success", created_at=self.f.now.isoformat())
        self.candidate_history = dict(total_count=1, workflow_runs=[deepcopy(self.candidate)])
        self.main_history = dict(total_count=0, workflow_runs=[])
        def chart_get(endpoint):
            if endpoint == "pulls/7": return deepcopy(self.pull)
            if endpoint == "pulls/7/files?per_page=100":
                return [dict(filename=path, status="modified") for path in ("values-staging.yaml", "releases/staging.json")]
            if "runs?head_sha=" + "f" * 40 in endpoint: return deepcopy(self.candidate_history)
            if "runs?head_sha=" + "b" * 40 in endpoint: return deepcopy(self.main_history)
            if endpoint == "actions/runs/55": return deepcopy(self.candidate)
            raise AssertionError(endpoint)
        self.engine.chart = SimpleNamespace(get=chart_get)
        self.reviews = [dict(state="approved", environments=[dict(id=10, name=ENVIRONMENT)],
            user=dict(id=123, type="User", login="reviewer"))]
        get = self.h.api.get
        def infra_get(endpoint):
            if endpoint == "actions/runs/301/approvals": return deepcopy(self.reviews)
            return get(endpoint)
        self.h.api.get = infra_get

    def observe(self, observations=1, sleep=lambda _: None):
        return self.engine.run(observations=observations, sleep=sleep)

    def complete_review(self):
        self.observe()
        run = self.h.runs[301]
        run.update(status="completed", conclusion="success", updated_at=self.f.now.isoformat())
        self.source_run = f"https://github.com/{INFRA_REPO}/actions/runs/301"
        request = dict(schemaVersion=1, kind="cold-promotion", sessionId=self.f.record["sessionId"],
            executionVersion=self.d.version, sourceRevisions=self.d.execution["sourceRevisions"],
            approvedBudgetUsd=self.d.execution["approvedBudgetUsd"], deadline=self.d.execution["deadline"],
            cleanupStartAt=self.d.execution["cleanupStartAt"], releaseReference=self.f.record["bootstrapRelease"],
            proposalReference=self.f.record["bootstrapProposal"], workflowRun=self.source_run)
        self.grant = dict(schemaVersion=1, kind="cold-promotion-approval", request=request, requestSha256=checksum(request),
            reviewer="reviewer", approvedAt=self.f.now.isoformat(), expiresAt=(self.f.now + timedelta(minutes=15)).isoformat())
        self.grant_ref = dict(key=f"promotion-approvals/staging/{self.f.record['sessionId']}/{checksum(request)}.json",
            versionId="grant-v1", sha256=checksum(self.grant))
        self.index = dict(schemaVersion=1, kind="cold-promotion-index", sessionId=self.f.record["sessionId"],
            executionVersion=self.d.version, sourceRevisions=self.d.execution["sourceRevisions"],
            dispatchId=self.f.record["workflowHandoff"]["id"], workflowRun=self.source_run, approvalReference=self.grant_ref)
        self.save_approval()

    def save_approval(self):
        self.f.objects[self.grant_ref["key"]] = encoded(self.grant), "grant-v1"
        self.f.objects[promotion_index_key(self.f.record["sessionId"], 301)] = encoded(self.index), "index-v1"

    def test_clean_exact_candidate_dispatches_one_protected_review(self):
        result = self.observe()
        self.assertEqual(result["workflowRunId"], 301)
        self.assertEqual(self.h.posts[0]["operation"], "review-promotion")
        self.assertEqual(self.h.posts[0]["inputs"], dict(session_id=self.f.record["sessionId"], execution_version=self.d.version))
        self.assertFalse(self.h.f.started)
        self.observe(2)
        self.assertEqual(len(self.h.posts), 1)
        self.assertNotIn("bootstrapPromotionApproval", self.f.record)

    def test_human_pr_review_and_candidate_ci_wait_keep_one_observation_loop(self):
        self.pull["mergeable_state"] = "blocked"
        result = self.observe(3)
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(result["pullRequest"], self.proposal["pullRequest"])
        self.assertFalse(self.h.posts)
        self.pull["mergeable_state"] = "clean"
        self.candidate.update(status="in_progress", conclusion=None)
        self.observe(2)
        self.assertFalse(self.h.posts)
        self.candidate.update(status="completed", conclusion="success")
        self.observe()
        self.assertEqual(len(self.h.posts), 1)

    def test_failed_candidate_does_not_dispatch_review_or_merge(self):
        self.candidate["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "candidate CI failed"): self.observe()
        self.assertFalse(self.h.posts)
        self.assertFalse(self.h.f.started)

    def test_successful_review_binds_grant_and_dispatches_one_typed_private_manager(self):
        self.complete_review()
        self.observe()
        self.assertEqual(self.f.record["bootstrapPromotionApproval"], self.grant_ref)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        def manager():
            self.h.f.support.build = dict(self.h.f.start_response,
                serviceRole="arn:aws:iam::058114477594:role/techx-automation-lifecycle-manager",
                buildStatus="IN_PROGRESS", buildComplete=False, environment=dict(environmentVariables=[
                    dict(name=key, value=value, type="PLAINTEXT") for key, value in
                    environment(self.f.record, operation("promote", "chart")).items()]))
        self.h.f.on_start = manager
        self.observe()
        self.assertEqual(self.f.record["lifecycleDispatch"]["operation"], operation("promote", "chart"))
        self.observe(2)
        self.assertEqual(len(self.h.f.started), 1)
        self.assertEqual(len(self.h.posts), 1)

    def test_lost_grant_cas_response_recovers_without_reissuing_review_or_manager(self):
        self.complete_review()
        write = self.d.store.write
        failed = False
        def lost(value, etag):
            nonlocal failed
            result = write(value, etag)
            if value.get("bootstrapPromotionApproval") and not failed:
                failed = True
                raise TimeoutError("CAS committed; response lost")
            return result
        self.d.store.write = lost
        self.observe()
        self.assertTrue(failed)
        self.assertEqual(self.f.record["bootstrapPromotionApproval"], self.grant_ref)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertFalse(self.h.f.started)
        self.assertEqual(len(self.h.posts), 1)

    def test_index_for_another_dispatch_run_source_or_version_cannot_bind(self):
        self.complete_review()
        original = deepcopy(self.index)
        for fields in (dict(dispatchId="0" * 64), dict(workflowRun=self.source_run + "0"), dict(executionVersion="other"),
                       dict(sourceRevisions={}), dict(approvalReference=dict(self.grant_ref, versionId=None))):
            self.index = dict(original, **fields)
            self.save_approval()
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.observe()
            self.assertNotIn("bootstrapPromotionApproval", self.f.record)
        self.assertFalse(self.h.f.started)

    def test_changed_release_or_proposal_after_review_prevents_binding(self):
        self.complete_review()
        for name in ("bootstrapRelease", "bootstrapProposal"):
            original = deepcopy(self.f.record[name])
            self.f.record[name] = dict(original, versionId="newer")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "superseded"): self.observe()
            self.f.record[name] = original
        self.assertNotIn("bootstrapPromotionApproval", self.f.record)

    def test_expired_grant_or_withdrawn_human_approval_cannot_bind(self):
        self.complete_review()
        self.reviews.clear()
        with self.assertRaises(ValueError): self.observe()
        self.assertNotIn("bootstrapPromotionApproval", self.f.record)
        self.reviews.append(dict(state="approved", environments=[dict(id=10, name=ENVIRONMENT)],
            user=dict(id=123, type="User", login="reviewer")))
        self.f.now += timedelta(minutes=16)
        with self.assertRaisesRegex(ValueError, "expired"): self.observe()
        self.assertFalse(self.h.f.started)

    def test_cleanup_race_during_human_review_read_blocks_cas(self):
        self.complete_review()
        get = self.h.api.get
        def cleanup(endpoint):
            value = get(endpoint)
            if endpoint.endswith("/approvals"): self.f.record["status"] = "DESTROYING"
            return value
        self.h.api.get = cleanup
        with self.assertRaises(ValueError): self.observe()
        self.assertNotIn("bootstrapPromotionApproval", self.f.record)
        self.assertFalse(self.h.f.started)

    def test_green_review_without_protected_index_or_wrong_grant_bytes_never_dispatches(self):
        self.complete_review()
        key = promotion_index_key(self.f.record["sessionId"], 301)
        del self.f.objects[key]
        get = self.d.clients["s3"].get_object
        def missing(**kwargs):
            if kwargs["Key"] == key: raise ClientError(dict(Error=dict(Code="NoSuchKey")), "GetObject")
            return get(**kwargs)
        self.d.clients["s3"].get_object = missing
        with self.assertRaises(ClientError): self.observe()
        self.d.clients["s3"].get_object = get
        self.save_approval()
        self.f.objects[self.grant_ref["key"]] = encoded(dict(self.grant, reviewer="other")), "grant-v1"
        with self.assertRaisesRegex(ValueError, "checksum"): self.observe()
        self.assertNotIn("bootstrapPromotionApproval", self.f.record)

    def test_missing_main_ci_waits_for_real_run_before_selecting(self):
        self.pull.update(merged=True, state="closed", merge_commit_sha="b" * 40)
        self.observe(2)
        self.assertFalse(self.h.posts)
        self.main_history = dict(total_count=1, workflow_runs=[dict(id=99, head_sha="b" * 40, head_branch="main", event="push",
            path=".github/workflows/verify.yml", repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO),
            status="in_progress", conclusion=None)])
        self.observe(2)
        self.assertFalse(self.h.posts)
        self.main_history["workflow_runs"][0].update(status="completed", conclusion="success")
        self.observe()
        self.assertEqual(self.h.posts[0]["operation"], "select-chart")
        self.assertEqual(self.h.posts[0]["inputs"]["chart_sha"], "b" * 40)

    def test_failed_or_incomplete_main_ci_cannot_select(self):
        self.pull.update(merged=True, state="closed", merge_commit_sha="b" * 40)
        self.main_history = dict(total_count=2, workflow_runs=[])
        with self.assertRaisesRegex(ValueError, "Complete chart main CI"): self.observe()
        self.assertFalse(self.h.posts)
        valid = dict(id=98, head_sha="b" * 40, head_branch="main", event="push", path=".github/workflows/verify.yml",
            repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO), status="completed", conclusion="success")
        self.main_history = dict(total_count=2, workflow_runs=[valid, dict(valid, id=99, conclusion="failure")])
        with self.assertRaisesRegex(ValueError, "Latest merged chart CI failed"): self.observe()
        self.assertFalse(self.h.posts)


if __name__ == "__main__": unittest.main()
