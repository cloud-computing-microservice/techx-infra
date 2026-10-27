"""Real READY controller, receipt, review and CAS flow against SDK/Git fixtures."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

from chart_selection import INFRA_REPO, PLATFORM_REPO
from lifecycle_dispatch import Dispatcher, environment, operation
from lifecycle_orchestration import Orchestrator
from plan_review_store import checksum
from workflow_handoff import intent_for, target_for


spec = importlib.util.spec_from_file_location("active_handoff_review_fixture", Path(__file__).with_name("test-ready-chart-promotion.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ActiveHandoffTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ActivePromotionTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        p, f = self.p, self.p.f
        self.f = f
        self.baseline = deepcopy(f.record["acceptedRelease"])
        f.record["deploymentTask"]["commandId"] = f.command_id
        op = operation("verify", "private")
        build_id = f.owner.split(":build/", 1)[1]
        f.record["lifecycleDispatch"] = dict(phase="OBSERVED", id="c" * 64, operation=op, buildId=build_id)
        self.build = dict(id=build_id, arn=f.owner, projectName="techx-automation-lifecycle",
            serviceRole="arn:aws:iam::058114477594:role/techx-automation-lifecycle-manager", buildStatus="SUCCEEDED", buildComplete=True,
            environment=dict(environmentVariables=[dict(name=key, value=value, type="PLAINTEXT") for key, value in environment(f.record, op).items()]))
        self.active_id = "techx-automation-lifecycle:abcdef98-1234"
        self.active_build = dict(id=self.active_id, arn="arn:aws:codebuild:us-east-1:058114477594:build/" + self.active_id,
            projectName="techx-automation-lifecycle", serviceRole=self.build["serviceRole"], buildStatus="IN_PROGRESS", buildComplete=False,
            environment=dict(environmentVariables=[dict(name=key, value=value, type="PLAINTEXT") for key, value in environment(f.record, operation("release", "active")).items()]))
        self.build_calls = []
        model = botocore.session.get_session().get_service_model("codebuild")
        def start_build(**kw):
            validate_parameters(kw, model.operation_model("StartBuild").input_shape)
            self.assertEqual(f.record["status"], "TESTING")
            self.assertEqual(f.record["lifecycleDispatch"]["phase"], "CALLING")
            self.build_calls.append(kw)
            return dict(build=deepcopy(self.active_build))
        f.clients["codebuild"] = SimpleNamespace(start_build=start_build,
            batch_get_builds=lambda **kw: dict(builds=[deepcopy(self.active_build if kw["ids"] == [self.active_id] else self.build)]))
        # The real private worker above already wrote its durable result. SSM
        # observation must not replay the base fixture's old foundation worker.
        f.remote_result = lambda: None
        f.statuses = ["Success"]
        self.d = Dispatcher(f.clients, f.store, "control-bucket", p.checks.execution, "execution-v1", clock=lambda: f.now)
        self.objects = {}
        s3 = f.clients["s3"]
        old_get, old_put, old_list = s3.get_object, s3.put_object, s3.list_objects_v2
        def get_object(**kw):
            if kw["Key"] == "automation/staging/session.json": return dict(Body=io.BytesIO(json.dumps(f.record).encode()), VersionId="current-session")
            if kw["Key"].startswith("handoffs/"): return dict(Body=io.BytesIO(self.objects[kw["Key"]]), VersionId="handoff-v1")
            return old_get(**kw)
        def put_object(**kw):
            if not kw["Key"].startswith("handoffs/"): return old_put(**kw)
            self.assertEqual(kw["IfNoneMatch"], "*")
            if kw["Key"] in self.objects: raise ClientError(dict(Error=dict(Code="PreconditionFailed")), "PutObject")
            self.objects[kw["Key"]] = kw["Body"]
            return dict(VersionId="handoff-v1")
        s3.get_object, s3.put_object = get_object, put_object
        s3.list_objects_v2 = lambda **kw: dict(IsTruncated=False, Contents=[dict(Key=key) for key in self.objects if key.startswith(kw["Prefix"])]) if kw["Prefix"].startswith("handoffs/") else old_list(**kw)
        self.posts, self.runs = [], {}
        self.lost_post = False
        self.reviews = []
        def get(path):
            if path == "git/ref/heads/main": return dict(object=dict(sha=self.d.execution["sourceRevisions"]["techx-infra"]))
            if path == "actions/runs/100": return deepcopy(self.runs[100])
            if path == "actions/runs/100/approvals": return deepcopy(self.reviews)
            if path.startswith("actions/workflows/staging-chart-promote.yml/runs?"): return dict(total_count=len(self.runs), workflow_runs=list(self.runs.values()))
            raise AssertionError(path)
        def dispatch(target, dispatch_id):
            self.posts.append(deepcopy(target))
            self.runs[100] = dict(id=100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main", head_sha=target["sourceSha"],
                path=".github/workflows/staging-chart-promote.yml", display_title="TechX handoff " + dispatch_id,
                repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO), created_at=f.now.isoformat())
            if self.lost_post: raise TimeoutError("Lost workflow dispatch response")
            return dict(workflow_run_id=100, run_url=f"https://api.github.com/repos/{INFRA_REPO}/actions/runs/100", html_url=f"https://github.com/{INFRA_REPO}/actions/runs/100")
        self.infra = SimpleNamespace(get=get, dispatch=dispatch)
        self.coordinator = SimpleNamespace(resume_approved=lambda _: self.fail("Active review cannot start a cold apply"))
        self.engine = Orchestrator(self.d, self.coordinator, {INFRA_REPO: self.infra, PLATFORM_REPO: p.p.github}, p.chart.github)

    def observe(self, count=1): return self.engine.run(observations=count, sleep=lambda _: None)

    def complete_review(self):
        self.observe()
        reader, writer, infra = self.p.reviews()
        dispatch_id = self.f.record["workflowHandoff"]["id"]
        reader.dispatch_id = writer.dispatch_id = dispatch_id
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-chart-reader/techx-chart-read-100"
        request = reader.preview(self.p.p.source, self.p.chart.github, infra, app_id=12345, app_slug="techx-promotion")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-promotion-approver/techx-promotion-approve-100"
        self.ref = writer.approve(self.p.p.source, self.p.chart.github, infra, request, checksum(request), app_id=12345, app_slug="techx-promotion")
        self.reviews = self.p.approvals
        self.runs[100].update(status="completed", conclusion="success", updated_at=self.f.now.isoformat())

    def test_dispatch_once_real_review_then_bind_only_the_active_grant(self):
        self.complete_review()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["operation"], "review-promotion")
        self.assertEqual(self.observe(3)["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(self.f.record["releaseCandidate"]["promotionApprovalReference"], self.ref)
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["status"], "TESTING")
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertEqual(self.observe()["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(self.build_calls), 1)

    def test_pending_ci_or_source_run_waits_without_dispatch_or_cleanup(self):
        for obj, field, value in ((self.p.ci, "status", "in_progress"), (self.p.p.run, "status", "in_progress")):
            original = obj[field]; obj[field] = value
            self.assertEqual(self.observe(2)["waiting"], "OBSERVATION_LIMIT")
            obj[field] = original
        self.assertFalse(self.posts)

    def test_live_manager_or_ssm_cannot_start_active_review(self):
        self.build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        self.assertEqual(self.observe(2)["waiting"], "OBSERVATION_LIMIT")
        self.assertFalse(self.posts)
        self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.f.statuses = ["InProgress"]
        with self.assertRaises(ValueError): self.observe()
        self.assertFalse(self.posts)

    def test_withdrawn_human_review_cannot_be_consumed(self):
        self.complete_review()
        self.reviews.clear()
        with self.assertRaises(ValueError): self.observe()
        self.assertNotIn("promotionApprovalReference", self.f.record["releaseCandidate"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_changed_candidate_cannot_consume_an_older_completed_grant(self):
        self.complete_review()
        self.f.record["releaseCandidate"]["manifestReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.observe()
        self.assertNotIn("promotionApprovalReference", self.f.record["releaseCandidate"])

    def test_lost_dispatch_response_recovers_the_same_run_without_repost(self):
        self.lost_post = True
        self.observe()
        self.observe(2)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "OBSERVED")

    def test_lost_grant_cas_response_is_reconciled_without_second_review(self):
        self.complete_review()
        write, lost = self.f.store.write, []
        def lost_response(value, etag):
            write(value, etag)
            if value.get("releaseCandidate", {}).get("promotionApprovalReference") and not lost:
                lost.append(True)
                raise TimeoutError("Grant CAS succeeded; response lost")
        self.f.store.write = lost_response
        self.assertEqual(self.observe(3)["waiting"], "OBSERVATION_LIMIT")
        self.assertTrue(lost)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.f.record["releaseCandidate"]["promotionApprovalReference"], self.ref)

    def test_ready_handoff_cannot_dispatch_cold_bootstrap_or_other_work(self):
        for op in ("cold-bootstrap", "reuse-chart"):
            with self.assertRaises(ValueError): intent_for(self.f.record, self.d.version, target_for(op, self.f.record, self.d.execution))
        self.assertFalse(self.posts)

    def test_publisher_cannot_republish_after_active_review_starts(self):
        self.observe()
        p = self.p.p
        p.run["status"] = "in_progress"
        p.jobs[1]["status"] = "in_progress"
        with self.assertRaises(ValueError): p.publisher.admission(self.f.record)
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__": unittest.main()
