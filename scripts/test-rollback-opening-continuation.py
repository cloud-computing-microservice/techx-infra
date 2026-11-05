"""Explicit reviewed opt-in and bounded existing-session rollback continuation."""
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from continue_lifecycle import continue_execution, drive, observation_end
from lifecycle_coordinator import Coordinator
from lifecycle_orchestration import Orchestrator
from plan_review_store import checksum, encoded


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


opening = load("rollback_opening_admission_fixture", "test-lifecycle-admission.py")
continuation = load("rollback_continuation_fixture", "test-rollback-orchestration.py")


class OpeningTests(unittest.TestCase):
    def setUp(self):
        self.p = opening.AdmissionTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)

    def test_rollback_policy_is_cloud_free_in_preview_and_pinned_in_reviewed_execution(self):
        p = self.p
        p.inputs["action"] = "deploy-window-rollback"
        clients = p.flow.clients; p.flow.clients = None
        request = p.flow.preview(p.inputs)
        p.flow.clients = clients
        self.assertFalse(p.f.events)
        self.assertEqual(request["record"]["action"], "deploy-window-rollback")
        self.assertNotIn(p.inputs["budget_alert_email"], json.dumps(request))
        result = p.start(request)
        self.assertEqual(p.f.current["action"], "deploy-window-rollback")
        self.assertEqual(p.f.current["approvedBudgetUsd"], request["record"]["approvedBudgetUsd"])
        self.assertEqual(p.f.current["deadline"], request["record"]["deadline"])
        self.assertEqual(result["deadline"], request["record"]["deadline"])
        self.assertLess(p.f.events.index("schedule"), p.f.events.index("start-build"))

    def test_approval_for_an_ordinary_window_cannot_be_upgraded_to_rollback(self):
        p = self.p
        p.inputs["action"] = "deploy-window"
        request = p.flow.preview(p.inputs)
        changed = dict(p.inputs, action="deploy-window-rollback")
        with self.assertRaises(ValueError): p.start(request, changed)
        self.assertFalse(p.f.events)
        altered = deepcopy(request); altered["record"]["action"] = "deploy-window-rollback"
        with self.assertRaises(ValueError): p.start(altered, changed, checksum(request))
        self.assertFalse(p.f.events)

    def test_ordinary_opening_remains_the_default_policy(self):
        request = self.p.flow.preview(self.p.inputs)
        self.assertEqual(request["record"]["action"], "deploy-test-destroy")
        for action in (None, True, "rollback", "deploy-window-rollback-again"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                opening.typed_inputs(dict(self.p.inputs, action=action))


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.p = continuation.RollbackOrchestrationTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        self.f, self.d = self.p.f, self.p.d

    def failed(self):
        self.p.failed()
        self.p.engine()  # Install the actual scoped controller run/role.
        self.infra = self.p.infra
        get = self.infra.get
        def caller(endpoint):
            value = get(endpoint)
            if endpoint == "actions/runs/300": value["path"] = ".github/workflows/staging-continue.yml"
            return value
        self.infra.get = caller

    def invoke(self, **kwargs):
        return continue_execution(self.d, Coordinator(self.f.clients, self.f.store, self.d.bucket), self.infra,
            self.p.a.p.p.r.h.p.chart, "300", lambda: self.fail("This path must not load source"),
            observations=2, sleep=lambda _: None, **kwargs)

    def test_plan_resume_does_not_approve_a_failed_session_rollback(self):
        self.failed()
        before = deepcopy(self.f.record)
        with self.assertRaises(ValueError): self.invoke(resume_approved=True)
        self.assertEqual(self.f.record, before)
        self.assertFalse(self.f.cleanup)

    def test_existing_failed_session_waits_same_live_manager_without_new_task(self):
        self.failed()
        self.p.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        before = deepcopy(self.f.record)
        self.assertEqual(self.invoke()["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(self.f.record, before)
        self.assertFalse(self.f.cleanup)

    def test_stale_or_closed_session_is_rejected_before_cleanup(self):
        self.failed()
        original = deepcopy(self.f.record)
        for field, value in (("executionVersion", "another-version"), ("status", "DESTROYING"), ("action", "deploy-window")):
            self.f.record = dict(deepcopy(original), **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): self.invoke()
            self.assertFalse(self.f.cleanup)

    def test_nested_failure_continuation_shares_original_observation_deadline(self):
        self.p.engine()
        self.infra = self.p.infra
        clock, slept = [100.0], []
        def sleep(delay): slept.append(delay); clock[0] += delay
        def earlier_observation(dispatcher, coordinator, **kwargs):
            self.assertEqual(kwargs["observation_deadline"], 2500)
            self.p.failed()
            self.p.a.p.p.private_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
            self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
            clock[0] = 2495
            return kwargs["failure_disposition"]()
        with patch("continue_lifecycle.drive", side_effect=earlier_observation):
            result = continue_execution(self.d, Coordinator(self.f.clients, self.f.store, self.d.bucket), self.infra,
                self.p.a.p.p.r.h.p.chart, "300", lambda: self.fail("Pending failure cannot load source"),
                observations=2, sleep=sleep, monotonic=lambda: clock[0])
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(clock[0], 2500)
        self.assertEqual(slept, [5])
        self.assertFalse(self.f.cleanup)
        self.assertNotIn("rollbackAttempt", self.f.record)

    def restore_fixture(self):
        p = self.p.a.rollback_fixture("test-rollback-private-acceptance.py", "RollbackPrivateTests")
        self.d, self.infra = p.d, p.p.controller.infra
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        get = self.infra.get
        def caller(endpoint):
            value = get(endpoint)
            if endpoint == "actions/runs/300": value["path"] = ".github/workflows/staging-continue.yml"
            return value
        self.infra.get = caller
        completed = False
        def finish(_):
            nonlocal completed
            if completed: return
            completed = True
            p.complete_parent()
            p.build.update(buildStatus="SUCCEEDED", buildComplete=True)
            self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        return p, finish

    def test_real_continuation_completes_private_acceptance_and_atomic_ready(self):
        p, finish = self.restore_fixture()
        result = continue_execution(self.d, Coordinator(self.f.clients, self.f.store, self.d.bucket), self.infra,
            p.r.chart.github, "300", lambda: p.runtime.source, observations=3, sleep=finish)
        self.assertEqual(result["waiting"], "READY")
        self.assertEqual(self.f.record["acceptedRelease"], result["acceptedRelease"])
        self.assertEqual(len(p.starts), 1)
        self.assertFalse(self.f.cleanup)

    def test_real_cli_binds_caller_execution_and_archive_before_resuming_failed_session(self):
        import continue_lifecycle as cli
        p, finish = self.restore_fixture()
        original = cli.continue_execution
        def bounded(*args, **kwargs): return original(*args, **kwargs, observations=3, sleep=finish)
        def archive(s3, bucket, session, version, root):
            self.assertEqual((session, version), (self.d.execution["sessionId"], self.d.version))
            root.mkdir(parents=True)
            (root / "execution.json").write_bytes(encoded(self.d.execution))
            (root / "source").mkdir()
        argv = ["continue_lifecycle.py", "--bucket", self.d.bucket, "--session-id", self.d.execution["sessionId"], "--execution-version", self.d.version]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, GITHUB_RUN_ID="300", GITHUB_TOKEN="fixture-token", GITHUB_STEP_SUMMARY=""), \
                patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), patch.object(cli, "S3SessionStore", return_value=self.f.store), \
                patch.object(cli, "Dispatcher", return_value=self.d), patch.object(cli, "GitHub", return_value=p.r.chart.github), \
                patch("workflow_handoff.WorkflowAPI", return_value=self.infra), patch("bootstrap_publish.archive_loader", side_effect=archive), \
                patch.object(cli, "continue_execution", side_effect=bounded), redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertEqual(json.loads(output.getvalue())["waiting"], "READY")
        self.assertEqual(len(p.starts), 1)
        self.assertFalse(self.f.cleanup)


class ObservationTests(unittest.TestCase):
    def test_invalid_or_extended_deadline_is_rejected(self):
        for value in (True, float("nan"), float("inf"), 2501, "2500"):
            with self.subTest(value=value), self.assertRaises(ValueError): observation_end(100, value)
        self.assertEqual(observation_end(100, 200), 200)

    def test_expired_deadline_cannot_read_or_dispatch_work(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        dispatcher = SimpleNamespace(execution=dict(sessionId="staging-expired"), version="v1", store=Mock())
        dispatcher.store.read.side_effect = AssertionError("Expired observation must not touch the session")
        self.assertEqual(drive(dispatcher, None, monotonic=lambda: 100, observation_deadline=99)["waiting"], "OBSERVATION_LIMIT")
        engine = Orchestrator(dispatcher, None, {}, None)
        self.assertEqual(engine.run(monotonic=lambda: 100, observation_deadline=99)["waiting"], "OBSERVATION_LIMIT")


if __name__ == "__main__": unittest.main()
