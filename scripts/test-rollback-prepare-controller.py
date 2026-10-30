"""Positive one-build rollback dispatch and terminal private proposal handoff."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from automation_session import claim, release
from deployment_manager import DeploymentManager
from deployment_tasks import claim_task, finish_task
from lifecycle_dispatch import Dispatcher
from private_rollback_prepare import PrivateRollbackPrepare
from rollback_prepare_controller import RollbackPrepareController
from rollback_task_contract import slot

spec = importlib.util.spec_from_file_location("rollback_controller_private_fixture", Path(__file__).with_name("test-private-rollback-prepare.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.p = support.PrivatePrepareTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        self.f = f = self.p.f
        f.record = deepcopy(self.p.dispatched)
        value = slot(f.record)
        f.record.update(deepcopy(value["origin"]))
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.d = Dispatcher(f.clients, f.store, self.p.d.bucket, self.p.execution, self.p.d.version, clock=lambda: f.now)
        self.controller = RollbackPrepareController(self.d, self.p.r.a.p.r.h.infra, "300")
        self.build = deepcopy(self.p.r.a.p.r.build)
        self.build.update(id=self.p.manager_arn.split("build/", 1)[1], arn=self.p.manager_arn,
            buildStatus="IN_PROGRESS", buildComplete=False)
        self.starts, self.lost_start = [], False
        batch = f.clients["codebuild"].batch_get_builds
        def start(**kwargs):
            model = botocore.session.get_session().get_service_model("codebuild")
            validate_parameters(kwargs, model.operation_model("StartBuild").input_shape)
            self.starts.append(deepcopy(kwargs))
            self.build["environment"] = dict(environmentVariables=deepcopy(kwargs["environmentVariablesOverride"]))
            if self.lost_start: raise TimeoutError("Actual manager started; response unknown")
            return dict(build=deepcopy(self.build))
        f.clients["codebuild"].start_build = start
        f.clients["codebuild"].batch_get_builds = lambda **kwargs: dict(builds=[deepcopy(self.build)]) if kwargs["ids"] == [self.build["id"]] else batch(**kwargs)
        f.remote_result = lambda: None
        f.statuses = ["Success"]

    def complete(self):
        f, p = self.f, self.p
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/codebuild-rollback"
        manager = DeploymentManager(f.clients, f.store, p.execution, p.d.version, p.d.bucket, p.manager_arn,
            clock=lambda: f.now, sleep=lambda _: None)
        pointer = manager.dispatch(p.runtime.private, "rollback", "prepare", 1)
        task = claim_task(f.store, f.clients["s3"], p.d.bucket, pointer["taskId"], f.instance_id, f.now)
        f.record = claim(f.record, p.owner, "rollback", now=f.now)
        epoch = f.record["lease"]["epoch"]
        worker = PrivateRollbackPrepare(p.runtime, f.store, p.d.version, task["taskId"], clock=lambda: f.now,
            clients=lambda *args: (p.chart.github, p.r.a.p.r.h.infra, None))
        worker.perform(p.owner, epoch)
        f.record = release(f.record, p.owner, epoch)
        finish_task(f.store, task, True, f.now)
        manager.wait(pointer)
        self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"

    def test_real_single_dispatch_private_task_and_terminal_receipt_handoff(self):
        self.assertEqual(self.controller.observe()["waiting"], "ROLLBACK_PREPARATION_RUNNING")
        self.assertEqual(self.controller.observe()["waiting"], "ROLLBACK_PREPARATION_RUNNING")
        self.assertEqual(len(self.starts), 1)
        self.complete()
        result = self.controller.observe()
        self.assertEqual(result["waiting"], "ROLLBACK_REVIEW")
        self.assertEqual(self.controller.observe(), result)
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(result["proposalReference"], self.f.record["rollbackAttempt"]["proposalReference"])
        self.assertEqual(self.f.record["acceptedRelease"], self.p.baseline)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertFalse(self.f.cleanup)

    def test_lost_start_response_never_replays_even_after_observation_retry(self):
        self.lost_start = True
        with self.assertRaises(TimeoutError): self.controller.observe()
        self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "CALLING")
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertEqual(len(self.starts), 1)
        self.assertTrue(self.f.cleanup)

    def test_successful_build_without_terminal_remote_task_cannot_produce_review(self):
        self.controller.observe()
        self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertTrue(self.f.cleanup)
        self.assertNotIn("proposalReference", self.f.record["rollbackAttempt"])

    def test_changed_proposal_bytes_cannot_be_consumed(self):
        self.controller.observe(); self.complete()
        ref = self.f.record["rollbackAttempt"]["proposalReference"]
        self.f.objects[ref["key"]] = b'{}', ref["versionId"]
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertTrue(self.f.cleanup)
        self.assertEqual(self.f.record["acceptedRelease"], self.p.baseline)

    def test_expiry_and_failed_build_do_not_restart_or_extend_the_window(self):
        self.controller.observe()
        expiry = slot(self.f.record)["intent"]["expiresAt"]
        self.build.update(buildStatus="FAILED", buildComplete=True)
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertEqual(len(self.starts), 1)
        self.f.now += timedelta(minutes=31)
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertEqual(slot(self.f.record)["intent"]["expiresAt"], expiry)
        self.assertEqual(len(self.starts), 1)

    def test_stale_execution_or_wrong_identity_cannot_dispatch_or_request_cleanup(self):
        self.f.identity["Arn"] = self.p.owner
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertFalse(self.starts); self.assertFalse(self.f.cleanup)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.f.record["executionVersion"] = "new-execution"
        with self.assertRaises(ValueError): self.controller.observe()
        self.assertFalse(self.starts); self.assertFalse(self.f.cleanup)

    def test_normal_lifecycle_cannot_continue_a_failed_session(self):
        with self.assertRaises(ValueError): self.d.current()
        with self.assertRaises(ValueError): self.d.advance()
        self.assertFalse(self.starts)


if __name__ == "__main__": unittest.main()
