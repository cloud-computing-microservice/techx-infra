from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from lifecycle_coordinator import Coordinator
from lifecycle_dispatch import environment, operation
from lifecycle_orchestration import Orchestrator
from orchestrate_lifecycle import INFRA_REPO, OPEN_WORKFLOW, WORKFLOW, request_cleanup, resolve
from plan_review_store import checksum, encoded
from workflow_handoff import DispatchUncertain

spec = importlib.util.spec_from_file_location("orchestration_handoff_support", Path(__file__).with_name("test-workflow-handoff.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.h = support.HandoffTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.d = self.h.d
        self.f = self.h.f.f
        self.coordinator = Coordinator(self.d.clients, self.d.store, self.d.bucket)
        self.engine = Orchestrator(self.d, self.coordinator, {INFRA_REPO: self.h.api}, Mock())

    def review_success(self):
        self.h.driver.start(self.h.target)
        self.h.runs[301].update(status="completed", conclusion="success")
        for key, (payload, object_version) in list(self.f.objects.items()):
            if key.startswith("approvals/"):
                approval = json.loads(payload)
                approval["reviewRun"] = f"https://github.com/{INFRA_REPO}/actions/runs/301"
                self.f.objects[key] = encoded(approval), object_version
        def actual_apply_build():
            response = self.h.f.start_response
            self.h.f.support.build = dict(response, serviceRole="arn:aws:iam::058114477594:role/techx-automation-lifecycle-manager",
                buildStatus="IN_PROGRESS", buildComplete=False,
                environment=dict(environmentVariables=[dict(name=key, value=value, type="PLAINTEXT") for key, value in
                    environment(self.f.record, operation("apply", "foundation")).items()]))
        self.h.f.on_start = actual_apply_build

    def test_manager_review_wait_automatically_dispatches_one_exact_review(self):
        result = self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(result["workflowRunId"], 301)
        self.assertEqual(self.h.posts[0]["operation"], "review-plan")
        self.assertEqual(len(self.h.posts), 1)
        self.assertFalse(self.h.f.started)

    def test_live_review_keeps_handle_without_post_or_apply_repetition(self):
        self.h.driver.start(self.h.target)
        result = self.engine.run(observations=2, sleep=lambda _: None)
        self.assertEqual(result["workflowRunId"], 301)
        self.assertEqual(len(self.h.posts), 1)
        self.assertFalse(self.h.f.started)

    def test_completed_real_review_auto_resumes_exact_apply_once(self):
        self.review_success()
        self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertEqual(self.f.record["lifecycleDispatch"]["operation"], operation("apply", "foundation"))
        self.assertEqual(len(self.h.f.started), 1)
        self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(len(self.h.f.started), 1)
        self.assertEqual(len(self.h.posts), 1)

    def test_apply_dispatch_survives_lost_consumed_cas_without_replaying_apply(self):
        self.review_success()
        write = self.d.store.write
        def lost(value, etag):
            if value.get("workflowHandoff", {}).get("phase") == "CONSUMED": raise RuntimeError("lost consume response")
            return write(value, etag)
        self.d.store.write = lost
        with self.assertRaises(RuntimeError): self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(len(self.h.f.started), 1)
        self.d.store.write = write
        self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertEqual(len(self.h.f.started), 1)

    def test_uncertain_consumption_without_apply_handle_never_replays(self):
        self.review_success()
        self.h.driver.phase(self.f.record["workflowHandoff"]["id"], "CONSUMING")
        with self.assertRaises(DispatchUncertain): self.engine.run(observations=1, sleep=lambda _: None)
        self.assertFalse(self.h.f.started)

    def test_failed_or_cancelled_child_requests_independent_cleanup(self):
        self.h.driver.start(self.h.target)
        self.h.runs[301].update(status="completed", conclusion="cancelled")
        with self.assertRaisesRegex(ValueError, "independent cleanup"):
            self.engine.run(observations=1, sleep=lambda _: None)
        self.assertTrue(self.h.f.support.cleanup)
        self.assertFalse(self.h.f.started)

    def test_green_workflow_without_its_protected_approval_does_not_apply(self):
        self.h.driver.start(self.h.target)
        self.h.runs[301].update(status="completed", conclusion="success")
        with self.assertRaisesRegex(ValueError, "matching protected approval"):
            self.engine.run(observations=1, sleep=lambda _: None)
        self.assertFalse(self.h.f.started)

    def test_missing_cross_repo_app_does_not_create_or_dispatch_a_cold_intent(self):
        self.h.f.applied()
        result = self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(result["waiting"], "GITHUB_APP_CONFIGURATION")
        self.assertFalse(self.h.posts)
        self.assertFalse(self.h.objects)
        self.assertFalse(self.h.f.started)

    def test_real_applied_foundation_dispatches_typed_cold_workflow_when_app_available(self):
        self.h.f.applied()
        self.h.target = support.target_for("cold-bootstrap", self.f.record, self.d.execution)
        self.engine.apis[support.PLATFORM_REPO] = self.h.api
        result = self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(result["workflowRunId"], 301)
        self.assertEqual(self.h.posts[0]["operation"], "cold-bootstrap")
        self.assertFalse(self.h.f.started)

    def publish_cold_evidence(self, run_id):
        source = self.d.execution["sourceRevisions"]
        manifest = dict(commit=source["techx-platform"], tag="staging-" + source["techx-platform"][:12], images=[
            dict(component=name, image=f"058114477594.dkr.ecr.us-east-1.amazonaws.com/techx/{name}:staging-{source['techx-platform'][:12]}",
                 digest="sha256:" + "e" * 64) for name in ("frontend", "catalog", "order")])
        release = dict(schemaVersion=1, sessionId=self.f.record["sessionId"], executionVersion=self.d.version,
            sourceRevisions=source, sourceCiPassed=True, manifest=manifest,
            sourceCi=dict(repository=support.PLATFORM_REPO, platformSha=source["techx-platform"], infraSha=source["techx-infra"],
                          workflowRunId=run_id, ciRunId=100))
        self.store_artifact("bootstrapRelease", "releases", release)
        proposal = dict(schemaVersion=2, kind="reuse", sessionId=self.f.record["sessionId"], executionVersion=self.d.version,
            sourceRevisions=source, releaseReference=self.f.record["bootstrapRelease"], baselineSha=source["techx-chart"],
            headSha=source["techx-chart"], treeSha="1" * 40)
        self.store_artifact("bootstrapProposal", "proposals", proposal)

    def store_artifact(self, field, prefix, value):
        key = f"{prefix}/staging/{self.f.record['sessionId']}/bootstrap.json"
        self.f.objects[key] = encoded(value), "artifact-v1"
        self.f.record[field] = dict(key=key, versionId="artifact-v1", sha256=checksum(value))

    def cold_child(self, evidence_run=301):
        self.h.f.applied()
        self.h.target = support.target_for("cold-bootstrap", self.f.record, self.d.execution)
        self.engine.apis[support.PLATFORM_REPO] = self.h.api
        self.h.driver.start(self.h.target)
        self.h.runs[301].update(status="completed", conclusion="success")
        self.publish_cold_evidence(evidence_run)

    def test_successful_cold_run_must_own_both_published_receipts(self):
        self.cold_child()
        self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertFalse(self.h.f.started)

    def test_another_green_cold_run_cannot_substitute_its_release(self):
        self.cold_child(evidence_run=999)
        with self.assertRaisesRegex(ValueError, "dispatched platform workflow"):
            self.engine.run(observations=1, sleep=lambda _: None)
        self.assertFalse(self.h.f.started)

    def test_cold_then_human_reuse_selection_advances_to_exact_gitops_manager(self):
        self.cold_child()
        self.engine.run(observations=1, sleep=lambda _: None)
        self.h.target = support.target_for("reuse-chart", self.f.record, self.d.execution)
        self.h.driver.start(self.h.target)
        self.h.runs[302].update(status="completed", conclusion="success")
        chosen = dict(schemaVersion=2, kind="reuse", sessionId=self.f.record["sessionId"], executionVersion=self.d.version,
            sourceRevisions=self.d.execution["sourceRevisions"], releaseReference=self.f.record["bootstrapRelease"],
            proposalReference=self.f.record["bootstrapProposal"], baselineSha=self.d.execution["sourceRevisions"]["techx-chart"],
            chartSha=self.d.execution["sourceRevisions"]["techx-chart"], treeSha="1" * 40,
            provenance=dict(reviewRun=f"https://github.com/{INFRA_REPO}/actions/runs/302", reviewer="reviewer", previewSha256="f" * 64,
                            ciRun=f"https://github.com/cloud-computing-microservice/techx-chart/actions/runs/88"))
        self.store_artifact("bootstrapChart", "chart-selections", chosen)
        self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        def gitops_build():
            self.h.f.support.build = dict(self.h.f.start_response, serviceRole="arn:aws:iam::058114477594:role/techx-automation-lifecycle-manager",
                buildStatus="IN_PROGRESS", buildComplete=False, environment=dict(environmentVariables=[
                    dict(name=key, value=value, type="PLAINTEXT") for key, value in environment(self.f.record, operation("bootstrap", "gitops")).items()]))
        self.h.f.on_start = gitops_build
        self.engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(self.f.record["lifecycleDispatch"]["operation"], operation("bootstrap", "gitops"))
        self.assertEqual(len(self.h.f.started), 1)
        self.assertEqual(len(self.h.posts), 2)


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.h = support.HandoffTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.d = self.h.d
        self.f = self.h.f.f
        self.source_sha = self.d.execution["sourceRevisions"]["techx-infra"]
        self.d.execution["sourceAdmission"] = dict(workflowRun=f"https://github.com/{INFRA_REPO}/actions/runs/200")
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.d.execution), self.d.version
        self.own = dict(id=900, status="in_progress", event="workflow_run", run_attempt=1, head_branch="main", head_sha=self.source_sha,
            path=WORKFLOW, repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        self.source = dict(self.own, id=200, status="completed", event="workflow_dispatch", conclusion="success", path=OPEN_WORKFLOW)
        get = self.d.clients["s3"].get_object
        self.d.clients["s3"].get_object = lambda **kw: dict(VersionId="pointer-v1", Body=__import__('io').BytesIO(encoded(
            dict(sessionId=self.f.record["sessionId"], executionVersion=self.d.version)))) if kw["Key"] == "control/staging/current-execution.json" else get(**kw)
        def api_get(endpoint):
            if endpoint == "actions/runs/900": return deepcopy(self.own)
            if endpoint == "actions/runs/200": return deepcopy(self.source)
            return self.h.api.get(endpoint)
        self.github = SimpleNamespace(get=api_get)
        self.cleanup = []
        self.d.clients["lambda"].invoke = lambda **kw: (self.cleanup.append(kw) or dict(StatusCode=202))

    def resolve(self, number=200, event_name="workflow_run", event=None):
        return resolve(self.d.clients, self.d.store, self.d.bucket, self.github, run_id="900", source_sha=self.source_sha,
            event_name=event_name, event=event if event is not None else dict(workflow_run=dict(id=number)))

    def test_exact_opening_callback_is_bound_to_admitted_execution(self):
        result = self.resolve()
        self.assertEqual(result["executionVersion"], self.d.version)
        self.assertFalse(result["boundFailure"])
        self.source["conclusion"] = "failure"
        self.assertTrue(self.resolve()["boundFailure"])

    def test_stale_root_or_fork_or_rerun_callback_is_a_noop(self):
        self.h.runs[201] = dict(self.source, id=201, path=".github/workflows/staging-plan-review.yml")
        self.assertEqual(self.resolve(201)["waiting"], "UNRELATED_CALLBACK")
        self.h.runs[201]["run_attempt"] = 2
        self.assertEqual(self.resolve(201)["waiting"], "UNRELATED_CALLBACK")
        self.h.runs[201]["run_attempt"] = 1
        self.h.runs[201]["repository"] = dict(full_name="fork/repo")
        self.assertEqual(self.resolve(201)["waiting"], "UNRELATED_CALLBACK")
        self.assertFalse(self.cleanup)

    def test_manual_wakeup_waits_for_opening_actor_to_finish(self):
        self.own["event"] = "workflow_dispatch"
        self.source["status"] = "in_progress"
        inputs = dict(session_id=self.f.record["sessionId"], execution_version=self.d.version)
        self.assertEqual(self.resolve(event_name="workflow_dispatch", event=dict(inputs=inputs))["waiting"], "OPENING_WORKFLOW_RUNNING")
        self.assertFalse(self.cleanup)

    def test_untrusted_opening_identity_cannot_be_used_as_admission(self):
        self.source["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "Opening workflow identity"): self.resolve()
        self.assertFalse(self.cleanup)

    def test_completed_child_callback_requires_the_protected_actual_run_receipt(self):
        self.h.driver.start(self.h.target)
        self.h.runs[301].update(status="completed", conclusion="success")
        self.assertFalse(self.resolve(301)["boundFailure"])
        key = next(key for key in self.h.objects if key.endswith("receipt.json"))
        del self.h.objects[key]
        self.assertEqual(self.resolve(301)["waiting"], "UNRELATED_CALLBACK")

    def test_manual_wakeup_cannot_choose_another_session_or_add_a_stage(self):
        self.own["event"] = "workflow_dispatch"
        inputs = dict(session_id=self.f.record["sessionId"], execution_version=self.d.version)
        self.assertEqual(self.resolve(event_name="workflow_dispatch", event=dict(inputs=inputs))["executionVersion"], self.d.version)
        with self.assertRaises(ValueError): self.resolve(event_name="workflow_dispatch", event=dict(inputs=dict(inputs, stage="apply")))
        with self.assertRaises(ValueError): self.resolve(event_name="workflow_dispatch", event=dict(inputs=dict(inputs, execution_version="other")))

    def test_closed_session_or_wrong_orchestrator_source_cannot_start_work(self):
        self.own["run_attempt"] = 2
        with self.assertRaises(ValueError): self.resolve()
        self.own["run_attempt"] = 1
        self.own["head_sha"] = "0" * 40
        with self.assertRaises(ValueError): self.resolve()
        self.own["head_sha"] = self.source_sha
        self.f.record["status"] = "DESTROYED"
        self.assertEqual(self.resolve()["waiting"], "NO_ACTIVE_SESSION")
        self.assertFalse(self.cleanup)

    def test_manual_wakeup_of_the_exact_failed_session_requests_cleanup_without_advancing(self):
        self.own["event"] = "workflow_dispatch"
        self.f.record["status"] = "FAILED_NEEDS_CLEANUP"
        inputs = dict(session_id=self.f.record["sessionId"], execution_version=self.d.version)
        result = self.resolve(event_name="workflow_dispatch", event=dict(inputs=inputs))
        self.assertTrue(result["boundFailure"])

    def test_cleanup_request_does_not_touch_a_newer_or_destroyed_session(self):
        self.assertTrue(request_cleanup(self.d.clients, self.d.store, self.d.execution, self.d.version))
        self.assertEqual(len(self.cleanup), 1)
        self.f.record["executionVersion"] = "newer-version"
        self.assertFalse(request_cleanup(self.d.clients, self.d.store, self.d.execution, self.d.version))
        self.assertEqual(len(self.cleanup), 1)


if __name__ == "__main__": unittest.main()
