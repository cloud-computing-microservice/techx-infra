from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from continue_lifecycle import WORKFLOW, drive, verified_caller

spec = importlib.util.spec_from_file_location("continuation_dispatch_support", Path(__file__).with_name("test-lifecycle-dispatch.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f = support.DispatchTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.dispatcher = self.f.dispatcher
        self.run = dict(id=123, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha=self.f.f.execution["sourceRevisions"]["techx-infra"], path=WORKFLOW,
            repository=dict(full_name="cloud-computing-microservice/techx-infra"),
            head_repository=dict(full_name="cloud-computing-microservice/techx-infra"))

    def test_caller_must_be_first_attempt_trusted_main_at_admitted_sha(self):
        github = SimpleNamespace(get=lambda endpoint: deepcopy(self.run))
        verified_caller(github, "123", self.f.f.execution)
        for key, value in (("run_attempt", 2), ("event", "pull_request"), ("head_branch", "feature"), ("head_sha", "f" * 40),
                           ("path", ".github/workflows/arbitrary.yml"), ("status", "completed"), ("repository", dict(full_name="fork/repo"))):
            old = self.run[key]
            self.run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): verified_caller(github, "123", self.f.f.execution)
            self.run[key] = old

    def test_review_wait_returns_exact_reference_without_consuming_apply(self):
        coordinator = Mock()
        result = drive(self.dispatcher, coordinator, observations=1, sleep=lambda _: self.fail("no wait"))
        self.assertEqual(result["waiting"], "PLAN_APPROVAL")
        self.assertEqual(result["reviewReference"], self.f.support.ref)
        coordinator.resume_approved.assert_not_called()
        self.assertFalse(self.f.started)

    def test_observation_limit_preserves_specific_live_build_and_never_restarts(self):
        self.f.support.build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        slept = []
        before = deepcopy(self.f.f.record)
        result = drive(self.dispatcher, Mock(), observations=2, sleep=slept.append)
        self.assertEqual(result["waiting"], "OBSERVATION_LIMIT")
        self.assertEqual(result["buildId"], self.f.support.build["id"])
        self.assertEqual(slept, [10, 10])
        self.assertEqual(before, self.f.f.record)
        self.assertFalse(self.f.started)
        self.assertFalse(self.f.support.cleanup)

    def test_real_service_failure_requests_cleanup_without_trying_another_manager(self):
        self.f.support.build.update(buildStatus="FAILED", buildComplete=True)
        with self.assertRaisesRegex(ValueError, "independent cleanup"):
            drive(self.dispatcher, Mock(), observations=1)
        self.assertEqual(len(self.f.support.cleanup), 1)
        self.assertFalse(self.f.started)

    def test_approved_resume_is_consumed_once_then_pipeline_waits_for_cold_release(self):
        calls = []
        def resume(session):
            calls.append(session)
            self.f.applied()
            return dict(buildId="fixture")
        result = drive(self.dispatcher, SimpleNamespace(resume_approved=resume), resume_approved=True, observations=3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["waiting"], "COLD_RELEASE")
        self.assertFalse(self.f.started)

    def test_prepare_continues_to_plan_and_returns_review_without_stage_input(self):
        task = deepcopy(self.f.f.record["deploymentTask"])
        self.f.prepared()
        def completes():
            self.f.support.build.update(self.f.start_response)
            self.f.support.build["environment"]["environmentVariables"] = deepcopy(self.f.started[0]["environmentVariablesOverride"])
            self.f.f.record.update(status="PROVISIONING", pendingPlan=deepcopy(self.f.support.ref))
            self.f.f.record["stages"]["foundation"] = dict(state="AWAITING_APPROVAL", attempt=1, reviewReference=deepcopy(self.f.support.ref))
            self.f.f.record["deploymentTask"] = dict(task, managerArn=self.f.start_response["arn"])
        self.f.on_start = completes
        result = drive(self.dispatcher, Mock(), observations=3)
        self.assertEqual(result["waiting"], "PLAN_APPROVAL")
        self.assertEqual(len(self.f.started), 1)
        self.assertEqual(self.f.f.record["deploymentTask"]["managerArn"], self.f.start_response["arn"])


if __name__ == "__main__": unittest.main()
