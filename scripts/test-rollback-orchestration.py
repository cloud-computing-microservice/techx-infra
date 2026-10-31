"""Original failed manager disposition and integrated orchestration, SDK mocked."""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from chart_selection import INFRA_REPO
from automation_session import utc
from deployment_manager import RollbackCandidateFailed
from lifecycle_coordinator import Coordinator
from lifecycle_orchestration import Orchestrator
from plan_review_store import encoded


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


support = load("rollback_disposition_private_fixture", "test-active-private-failure.py")


class RollbackOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.a = support.PrivateFailureTests()
        self.a.setUp(); self.addCleanup(self.a.doCleanups)
        self.f, self.d = self.a.f, self.a.d
        self.manager = self.a.p.manager
        self.infra = self.a.p.p.r.h.infra

    def failed(self, **kwargs):
        self.a.fail(**kwargs)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/failed-private"
        return deepcopy(self.f.record["deploymentTask"])

    def engine(self, *, source=None):
        f = self.f
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        own = dict(id=300, status="in_progress", event="workflow_dispatch", head_branch="main", run_attempt=1,
            head_sha=self.d.execution["sourceRevisions"]["techx-infra"], path=".github/workflows/staging-orchestrate.yml",
            created_at=f.now.isoformat(), repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        get = self.infra.get
        self.infra.get = lambda endpoint: deepcopy(own) if endpoint == "actions/runs/300" else get(endpoint)
        return Orchestrator(self.d, Coordinator(f.clients, f.store, self.d.bucket), {INFRA_REPO: self.infra}, self.a.p.p.r.h.p.chart,
            run_id="300", rollback_source=source)

    def test_manager_exits_failed_without_cleanup_only_after_positive_private_failure(self):
        pointer = self.failed()
        self.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        with patch.object(self.f.clients["codebuild"], "batch_get_builds", side_effect=AssertionError("Manager has no build-read permission")), \
                self.assertRaises(RollbackCandidateFailed): self.manager.wait(pointer, max_observations=1)
        self.assertFalse(self.f.cleanup)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_unproven_vpn_or_changed_task_still_requests_cleanup(self):
        pointer = self.failed(disconnect=False)
        with self.assertRaises(ValueError): self.manager.wait(pointer, max_observations=1)
        self.assertTrue(self.f.cleanup)

    def test_manager_cannot_defer_when_the_original_window_is_too_short(self):
        pointer = self.failed()
        self.f.now = utc(self.f.record["cleanupStartAt"]) - timedelta(minutes=29)
        with self.assertRaises(ValueError): self.manager.wait(pointer, max_observations=1)
        self.assertTrue(self.f.cleanup)

    def test_orchestrator_waits_same_live_failed_manager_without_reserving_or_loading_source(self):
        self.failed()
        self.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        source = lambda: self.fail("Pending parent cannot load or dispatch rollback")
        engine = self.engine(source=source)
        before = deepcopy(self.f.record)
        result = engine.run(observations=2, sleep=lambda _: None)
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(self.f.record, before)
        self.assertFalse(self.f.cleanup)

    def test_cancelled_failed_parent_is_cleanup_instead_of_a_new_attempt(self):
        self.failed()
        self.a.p.p.private_build.update(buildStatus="STOPPED", buildComplete=True)
        with self.assertRaises(ValueError): self.engine().run(observations=1, sleep=lambda _: None)
        self.assertTrue(self.f.cleanup)
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_public_failure_waits_exact_live_hosted_job_without_consuming_attempt(self):
        public = load("pending_public_failure_fixture", "test-rollback-admission.py").AdmissionTests()
        public.setUp(); self.addCleanup(public.doCleanups)
        self.f, self.d, self.infra = public.f, public.d, public.p.r.h.infra
        self.d.execution = deepcopy(public.execution)
        public.p.run.update(status="in_progress", conclusion=None)
        public.p.job.update(status="in_progress", conclusion=None)
        # The public child may be unwinding its existing HTTP-only lease.
        self.f.record["lease"] = dict(deepcopy(self.a.before["lease"]), owner=public.p.worker.owner)
        engine = self.engine(source=lambda: self.fail("A live public job cannot dispatch rollback"))
        before = deepcopy(self.f.record)
        result = engine.run(observations=2, sleep=lambda _: None)
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(self.f.record, before)
        self.assertFalse(self.f.cleanup)

    def test_private_readiness_or_receipt_uncertainty_cannot_delay_cleanup(self):
        self.failed()
        self.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        self.f.record["releaseCandidate"]["privateAcceptance"]["contextSha256"] = "0" * 64
        with self.assertRaises(ValueError): self.engine().run(observations=1, sleep=lambda _: None)
        self.assertTrue(self.f.cleanup)

    def test_missing_cleanup_schedule_blocks_even_a_known_pending_failure(self):
        self.failed()
        self.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        self.f.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): self.engine().run(observations=1, sleep=lambda _: None)
        self.assertTrue(self.f.cleanup)

    def test_real_manager_entry_keeps_nonzero_exit_and_rechecks_failure_before_deferring(self):
        import run_lifecycle_manager as cli
        pointer = self.failed()
        execution = self.a.p.runtime.work / "failed-private-manager-execution.json"
        execution.write_bytes(encoded(self.d.execution))
        arguments = ["run_lifecycle_manager.py", "verify", "--stage", "active", "--attempt", "1", "--execution", str(execution),
            "--execution-version", self.d.version, "--bucket", self.d.bucket]
        with patch.object(sys, "argv", arguments), patch.dict(os.environ, CODEBUILD_BUILD_ARN=self.manager.owner), \
                patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch.object(cli, "S3SessionStore", return_value=self.f.store), patch.object(cli, "DeploymentManager", return_value=self.manager), \
                patch.object(self.manager, "dispatch", return_value=pointer), self.assertRaises(RollbackCandidateFailed): cli.main()
        self.assertFalse(self.f.cleanup)

    def test_manager_entry_requests_cleanup_if_failure_changes_before_final_exit(self):
        import run_lifecycle_manager as cli
        pointer = self.failed()
        execution = self.a.p.runtime.work / "changed-private-manager-execution.json"
        execution.write_bytes(encoded(self.d.execution))
        candidate, calls = self.manager.rollback_candidate, 0
        def changed(task):
            nonlocal calls
            result = candidate(task)
            calls += 1
            if calls == 1:
                self.f.record["releaseCandidate"]["privateAcceptance"]["failureReference"]["sha256"] = "0" * 64
            return result
        arguments = ["run_lifecycle_manager.py", "verify", "--stage", "active", "--attempt", "1", "--execution", str(execution),
            "--execution-version", self.d.version, "--bucket", self.d.bucket]
        with patch.object(sys, "argv", arguments), patch.dict(os.environ, CODEBUILD_BUILD_ARN=self.manager.owner), \
                patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch.object(cli, "S3SessionStore", return_value=self.f.store), patch.object(cli, "DeploymentManager", return_value=self.manager), \
                patch.object(self.manager, "dispatch", return_value=pointer), patch.object(self.manager, "rollback_candidate", side_effect=changed), \
                self.assertRaises(ValueError): cli.main()
        self.assertTrue(self.f.cleanup)
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_main_orchestration_admits_and_dispatches_exactly_one_preparation(self):
        p = self.a.rollback_fixture("test-rollback-prepare-controller.py", "ControllerTests")
        self.d, self.infra = p.d, p.p.r.a.p.r.h.infra
        self.f.record.pop("rollbackAttempt")  # Model the unreserved failed session.
        engine = Orchestrator(self.d, Coordinator(self.f.clients, self.f.store, self.d.bucket), {INFRA_REPO: self.infra}, p.p.chart.github,
            run_id="300", rollback_source=lambda: p.p.runtime.source)
        first = engine.run(observations=1, sleep=lambda _: None)
        reservation = deepcopy(self.f.record["rollbackAttempt"])
        second = engine.run(observations=1, sleep=lambda _: None)
        self.assertEqual(first["waiting"], second["waiting"])
        self.assertEqual(first["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(len(p.starts), 1)
        self.assertEqual(self.f.record["rollbackAttempt"], reservation)
        self.assertEqual(self.f.record["lifecycleDispatch"]["operation"], dict(mode="rollback", stage="prepare", attempt=1))
        self.assertFalse(self.f.cleanup)

    def callback_main(self, *, opening_failed):
        import orchestrate_lifecycle as cli
        self.failed()
        self.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        self.engine()  # Install the actual scoped controller fixture.
        event_path = self.a.p.runtime.work / "rollback-callback-event.json"
        event_path.write_text(json.dumps(dict(inputs=dict(session_id=self.f.record["sessionId"], execution_version=self.d.version))))
        class BoundedOrchestrator(Orchestrator):
            def run(self): return super().run(observations=1, sleep=lambda _: None)
        context = dict(execution=self.d.execution, executionVersion=self.d.version, boundFailure=True, openingFailed=opening_failed)
        invoke = self.f.clients["lambda"].invoke
        def acknowledged(**kw): invoke(**kw); return dict(StatusCode=202)
        with patch.dict(os.environ, GITHUB_RUN_ID="300", GITHUB_SHA=self.d.execution["sourceRevisions"]["techx-infra"],
                GITHUB_EVENT_PATH=str(event_path), GITHUB_EVENT_NAME="workflow_dispatch", CONTROL_BUCKET=self.d.bucket,
                GITHUB_TOKEN="fixture-github-token"), patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch.object(cli, "resolve", return_value=context), patch.object(cli, "S3SessionStore", return_value=self.f.store), \
                patch.object(cli, "Dispatcher", return_value=self.d), patch.object(cli, "WorkflowAPI", return_value=self.infra), \
                patch.object(cli, "Orchestrator", BoundedOrchestrator), patch.object(self.f.clients["lambda"], "invoke", side_effect=acknowledged), \
                redirect_stdout(io.StringIO()) as output:
            cli.main()
        return json.loads(output.getvalue().splitlines()[0])

    def test_callback_entry_routes_opted_in_candidate_failure_to_live_parent_observation(self):
        result = self.callback_main(opening_failed=False)
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertFalse(self.f.cleanup)
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_failed_or_cancelled_opening_cannot_be_reinterpreted_as_candidate_failure(self):
        result = self.callback_main(opening_failed=True)
        self.assertEqual(result["waiting"], "CLEANUP_REQUESTED")
        self.assertTrue(result["requestAcknowledged"])
        self.assertTrue(self.f.cleanup)
        self.assertNotIn("rollbackAttempt", self.f.record)

    def test_old_failed_session_cannot_request_cleanup_of_a_new_session(self):
        self.failed()
        engine = self.engine()
        self.f.record["executionVersion"] = "new-execution-version"
        with self.assertRaises(ValueError): engine.run(observations=1, sleep=lambda _: None)
        self.assertFalse(self.f.cleanup)

    def test_main_orchestration_resumes_private_acceptance_and_verifies_atomic_ready(self):
        p = self.a.rollback_fixture("test-rollback-private-acceptance.py", "RollbackPrivateTests")
        self.d, self.infra = p.d, p.p.controller.infra
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        engine = Orchestrator(self.d, Coordinator(self.f.clients, self.f.store, self.d.bucket), {INFRA_REPO: self.infra}, p.r.chart.github,
            run_id="300", rollback_source=lambda: p.runtime.source)
        completed = False
        def child_finishes(_):
            nonlocal completed
            if completed: return
            completed = True
            p.complete_parent()
            p.build.update(buildStatus="SUCCEEDED", buildComplete=True)
            self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        result = engine.run(observations=3, sleep=child_finishes)
        self.assertEqual(result["waiting"], "READY")
        self.assertEqual(self.f.record["acceptedRelease"], result["acceptedRelease"])
        self.assertEqual(len(p.starts), 1)
        self.assertFalse(self.f.cleanup)


if __name__ == "__main__": unittest.main()
