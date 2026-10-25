from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from automation_session import claim, release
from deployment_tasks import claim_task, create_task, finish_task
from lifecycle_coordinator import Coordinator
from plan_review_store import encoded
from run_lifecycle_stage import LifecycleStageWorker

def support(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

runtime = support("resume_runtime_support", "test-deployment-runtime.py")
manager = support("resume_manager_support", "test-deployment-manager.py")
sys.path.insert(0, str(Path(__file__).parents[1] / "bootstrap/automation"))
from lifecycle_loader import command


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.f = runtime.RuntimeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        config_fixture = manager.ManagerTests()
        config_fixture.setUp()
        self.f.execution["deploymentConfig"] = deepcopy(config_fixture.config)
        self.f.runtime = self.f.create()
        self.f.flow()
        f = self.f
        f.execution["costEstimate"] = dict(includesAutomation=True, maximumHours=f.record["maximumHours"], upperBoundUsd=20,
            approvedBudgetUsd=f.record["approvedBudgetUsd"])
        f.record = release(f.record, f.owner, f.record["lease"]["epoch"])
        self.task_id = "d" * 32
        _, pointer = create_task(f.clients["s3"], f.bucket, f.record, "execution-version", f.instance["InstanceId"],
            config_fixture.owner, "plan", "foundation", 1, self.task_id, f.now)
        f.record["deploymentTask"] = pointer
        task = claim_task(f.store, f.clients["s3"], f.bucket, self.task_id, f.instance["InstanceId"], f.now)
        f.record = claim(f.record, f.owner, "deploy", now=f.now)
        f.worker = LifecycleStageWorker(f.runtime, f.store, "execution-version", clock=lambda: f.now, task_id=self.task_id)
        self.ref = f.perform()
        f.record = release(f.record, f.owner, f.record["lease"]["epoch"])
        finish_task(f.store, task, True, f.now)
        f.record["deploymentTask"]["commandId"] = config_fixture.command_id
        f.approve(self.ref)
        f.objects[f"executions/{f.record['sessionId']}.json"] = encoded(f.execution), "execution-version"
        self.status, self.started, self.cleanup = "Success", [], []
        self.lost_start = False
        from lifecycle_dispatch import environment, operation, ROLE
        f.record["lifecycleDispatch"] = dict(id="e" * 64, phase="OBSERVED", buildId=config_fixture.owner.split("build/")[1],
            operation=operation("plan", "foundation"))
        self.build = dict(id=f.record["lifecycleDispatch"]["buildId"], arn=config_fixture.owner,
            projectName="techx-automation-lifecycle", serviceRole=ROLE, buildComplete=True, buildStatus="SUCCEEDED",
            environment=dict(environmentVariables=[dict(name=k, value=v, type="PLAINTEXT") for k, v in
                environment(f.record, f.record["lifecycleDispatch"]["operation"]).items()]))
        def start_build(**kw):
            self.started.append(kw)
            if self.lost_start: raise TimeoutError("StartBuild response unknown")
            build_id = "techx-automation-lifecycle:abcdef12"
            return dict(build=dict(id=build_id, arn=f"arn:aws:codebuild:us-east-1:058114477594:build/{build_id}", projectName="techx-automation-lifecycle"))
        f.clients["codebuild"] = SimpleNamespace(start_build=start_build, batch_get_builds=lambda **kw: dict(builds=[deepcopy(self.build)]))
        f.clients["lambda"] = SimpleNamespace(invoke=lambda **kw: self.cleanup.append(kw))
        f.clients["ssm"] = SimpleNamespace(get_command_invocation=lambda **kw: dict(CommandId=config_fixture.command_id,
            InstanceId=f.instance["InstanceId"], DocumentName="techx-automation-deployment-task", DocumentVersion="1",
            PluginName="runTask", Status=self.status, ResponseCode=0 if self.status == "Success" else -1),
            exceptions=SimpleNamespace(InvocationDoesNotExist=Exception))
        self.coordinator = Coordinator(f.clients, f.store, f.bucket)

    def resume(self): return self.coordinator.resume_approved(self.f.record["sessionId"], self.f.now)

    def test_resume_after_real_plan_store_approval_and_task_completion_dispatches_typed_apply_only(self):
        before = deepcopy(self.f.record)
        result = self.resume()
        self.assertEqual(result["stage"], "foundation")
        self.assertEqual(result["attempt"], 1)
        expected = deepcopy(before)
        expected["lifecycleDispatch"] = deepcopy(self.f.record["lifecycleDispatch"])
        self.assertEqual(self.f.record, expected)
        self.assertEqual(self.f.record["lifecycleDispatch"]["operation"], dict(mode="apply", stage="foundation", attempt=1))
        env = {row["name"]: row["value"] for row in self.started[0]["environmentVariablesOverride"]}
        self.assertEqual(env["TECHX_MODE"], "apply")
        self.assertEqual(env["TECHX_EXECUTION_VERSION"], "execution-version")
        self.assertNotIn("approved", env)

    def test_missing_approval_active_command_or_missing_task_never_dispatches(self):
        self.status = "InProgress"
        with self.assertRaises(ValueError): self.resume()
        self.status = "Success"
        task = self.f.record.pop("deploymentTask")
        with self.assertRaises(ValueError): self.resume()
        self.f.record["deploymentTask"] = task
        approval = next(key for key in self.f.objects if key.startswith("approvals/"))
        self.f.objects.pop(approval)
        with self.assertRaises(self.f.clients["s3"].exceptions.NoSuchKey): self.resume()
        self.assertFalse(self.started)

    def test_disabled_schedule_stale_source_or_busy_session_never_dispatches(self):
        self.f.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): self.resume()
        self.f.schedule["State"] = "ENABLED"
        self.f.record["lease"] = dict(owner="active-worker")
        with self.assertRaises(ValueError): self.resume()
        self.f.record.pop("lease")
        self.f.record["sourceRevisions"]["techx-infra"] = "e" * 40
        with self.assertRaises(ValueError): self.resume()
        self.assertFalse(self.started)

    def test_lost_resume_response_requests_independent_cleanup_without_reopening_session(self):
        self.lost_start = True
        with self.assertRaises(TimeoutError): self.resume()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.cleanup), 1)
        self.assertEqual(self.f.record["pendingPlan"], self.ref)
        self.assertEqual(self.f.record["status"], "PROVISIONING")
        self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "CALLING")
        with self.assertRaises(ValueError): self.resume()
        self.assertEqual(len(self.started), 1)

    def test_loader_accepts_only_typed_operations_and_never_interpolates_a_shell(self):
        infra, directory = Path("/verified/source/techx-infra"), Path("/verified")
        result = command(infra, directory, "control-bucket", "execution-version", {})
        self.assertEqual(result[2], "prepare")
        bootstrap = command(infra, directory, "control-bucket", "execution-version", dict(TECHX_MODE="bootstrap", TECHX_PLAN_STAGE="gitops"))
        self.assertEqual(bootstrap[2], "bootstrap")
        with self.assertRaises(ValueError):
            command(infra, directory, "control-bucket", "execution-version", dict(TECHX_MODE="bootstrap", TECHX_PLAN_STAGE="foundation"))
        for key, value in dict(TECHX_MODE="bash", TECHX_PLAN_STAGE="edge; bad", TECHX_PLAN_ATTEMPT="1; bad").items():
            with self.subTest(key=key), self.assertRaises(ValueError): command(infra, directory, "control-bucket", "execution-version", {key: value})


if __name__ == "__main__": unittest.main()
