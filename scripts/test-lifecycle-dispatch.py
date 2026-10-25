from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import botocore.session
from botocore.validate import validate_parameters

from lifecycle_dispatch import Dispatcher, completed_manager, environment, next_operation, operation
from cleanup_execution import stop_dispatched_manager, stop_executor

spec = importlib.util.spec_from_file_location("dispatch_resume_support", Path(__file__).with_name("test-lifecycle-resume.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.support = support.ResumeTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.f = self.support.f
        self.dispatcher = Dispatcher(self.f.clients, self.f.store, self.f.bucket, self.f.execution, "execution-version", clock=lambda: self.f.now)
        self.model = botocore.session.get_session().get_service_model("codebuild")
        self.started = []
        self.on_start = None
        self.start_response = dict(id="techx-automation-lifecycle:abcdef98-1234",
            arn="arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:abcdef98-1234", projectName="techx-automation-lifecycle")
        def start(**kw):
            validate_parameters(kw, self.model.operation_model("StartBuild").input_shape)
            self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "CALLING")
            self.started.append(kw)
            if self.on_start: self.on_start()
            return dict(build=deepcopy(self.start_response))
        self.f.clients["codebuild"].start_build = start

    def previous(self, op):
        self.f.record["lifecycleDispatch"]["operation"] = deepcopy(op)
        self.support.build["environment"]["environmentVariables"] = [dict(name=k, value=v, type="PLAINTEXT")
            for k, v in environment(self.f.record, op).items()]
        if op["mode"] != "prepare": self.f.record["deploymentTask"].update(op)

    def prepared(self):
        self.f.record.update(status="PLANNING", stages={})
        self.f.record.pop("pendingPlan", None)
        self.f.record.pop("deploymentTask")
        self.f.record["executorPrepared"] = dict(instanceId=self.f.instance["InstanceId"], imageId=self.f.execution["deploymentConfig"]["imageId"],
            managerArn=self.support.build["arn"])
        self.previous(operation("prepare", "foundation"))

    def applied(self):
        self.f.record.pop("pendingPlan")
        self.f.record["stages"]["foundation"] = dict(state="APPLIED", attempt=1, stateSerial=1,
            stateLineage="12345678-1234-1234-1234-123456789abc", savedPlanSha256="a" * 64)
        self.previous(operation("apply", "foundation"))

    def test_prepare_to_foundation_plan_is_typed_and_single_consumption(self):
        self.prepared()
        result = self.dispatcher.advance()
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["stage"], "foundation")
        self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "OBSERVED")
        self.assertEqual(len(self.started), 1)
        # Same continuation cannot create another manager, even if its caller
        # retries after the provider's short idempotency-token lifetime.
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertEqual(len(self.started), 1)

    def test_pending_review_is_an_explicit_wait_and_never_apply_authorization(self):
        result = self.dispatcher.advance()
        self.assertEqual(result["waiting"], "PLAN_APPROVAL")
        self.assertFalse(self.started)
        self.assertEqual(self.f.record["pendingPlan"], self.support.ref)

    def test_cold_release_and_selection_waits_do_not_dispatch_unimplemented_workflows(self):
        self.applied()
        self.assertEqual(self.dispatcher.advance()["waiting"], "COLD_RELEASE")
        self.f.record["bootstrapRelease"] = dict(key="fixture")
        self.assertEqual(self.dispatcher.advance()["waiting"], "CHART_PROPOSAL")
        self.f.record["bootstrapProposal"] = dict(key="fixture")
        self.assertEqual(self.dispatcher.advance()["waiting"], "CHART_SELECTION")
        self.assertFalse(self.started)

    def test_unknown_start_keeps_calling_intent_and_cannot_be_replayed(self):
        self.prepared()
        def lost(): raise TimeoutError("StartBuild may have succeeded")
        self.on_start = lost
        with self.assertRaises(TimeoutError): self.dispatcher.advance()
        self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "CALLING")
        self.assertEqual(len(self.support.cleanup), 1)
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertEqual(len(self.started), 1)

    def test_active_failed_missing_or_wrong_manager_observations_never_advance(self):
        self.prepared()
        for key, value in (("buildStatus", "IN_PROGRESS"), ("buildComplete", False), ("buildStatus", "FAILED"),
                           ("serviceRole", "arn:aws:iam::058114477594:role/admin"), ("projectName", "another-project"),
                           ("arn", "arn:aws:codebuild:us-east-1:058114477594:build/another:123")):
            old = self.support.build[key]
            self.support.build[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): self.dispatcher.advance()
            self.support.build[key] = old
        self.f.clients["codebuild"].batch_get_builds = lambda **kw: dict(builds=[], buildsNotFound=["missing"])
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertFalse(self.started)

    def test_wrong_service_environment_or_missing_preparation_checkpoint_blocks(self):
        self.prepared()
        rows = self.support.build["environment"]["environmentVariables"]
        rows.append(deepcopy(rows[0]))
        with self.assertRaises(ValueError): self.dispatcher.advance()
        rows.pop()
        old = rows[0]["value"]
        rows[0]["value"] = "staging-another"
        with self.assertRaises(ValueError): self.dispatcher.advance()
        rows[0]["value"] = old
        self.f.record["executorPrepared"]["managerArn"] = "unproven"
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertFalse(self.started)

    def test_real_ssm_success_and_checkpoint_both_required_even_after_codebuild_success(self):
        self.support.status = "InProgress"
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.support.status = "Success"
        self.f.record["deploymentTask"]["managerArn"] = "different-manager"
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertFalse(self.started)

    def test_cas_change_deadline_schedule_or_cleanup_closes_dispatch(self):
        self.prepared()
        checked = deepcopy(self.f.record)
        self.f.record["changed"] = True
        with self.assertRaises(ValueError): self.dispatcher.start(checked, operation("plan", "foundation"))
        self.f.record.pop("changed")
        self.f.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.f.schedule["State"] = "ENABLED"
        self.f.record["status"] = "DESTROYING"
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.f.record["status"] = "PLANNING"
        self.dispatcher.clock = lambda: self.f.now + timedelta(hours=4)
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertFalse(self.started)

    def test_lost_observed_cas_response_preserves_worker_fields_and_recovers_same_build(self):
        self.prepared()
        original = self.f.store.write
        lost = []
        def write(value, etag):
            original(value, etag)
            if value.get("lifecycleDispatch", {}).get("phase") == "OBSERVED" and not lost:
                lost.append(True)
                self.f.record["workerCheckpoint"] = "already-running"
                raise TimeoutError("successful CAS response lost")
        self.f.store.write = write
        self.dispatcher.advance()
        self.assertEqual(self.f.record["workerCheckpoint"], "already-running")
        self.assertEqual(len(self.started), 1)
        self.assertFalse(self.support.cleanup)

    def test_lost_prepared_cas_response_requests_cleanup_and_cannot_dispatch_later(self):
        self.prepared()
        original = self.f.store.write
        def write(value, etag):
            original(value, etag)
            if value.get("lifecycleDispatch", {}).get("phase") == "PREPARED":
                raise TimeoutError("successful preparation CAS response lost")
        self.f.store.write = write
        with self.assertRaises(TimeoutError): self.dispatcher.advance()
        self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "PREPARED")
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertFalse(self.started)
        self.assertEqual(len(self.support.cleanup), 1)

    def test_cleanup_takeover_during_start_cannot_be_overwritten(self):
        self.prepared()
        self.on_start = lambda: self.f.record.update(status="DESTROYING")
        with self.assertRaises(ValueError): self.dispatcher.advance()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["lifecycleDispatch"]["phase"], "CALLING")
        self.assertEqual(len(self.support.cleanup), 1)

    def test_gitops_to_edge_and_applied_edge_stops_before_dns_acceptance(self):
        self.applied()
        ref = dict(key=f"chart-selections/staging/{self.f.record['sessionId']}/bootstrap.json", versionId="selection-v1", sha256="d" * 64)
        self.f.record.update(bootstrapChart=ref, bootstrapRelease=dict(key="release-fixture"))
        self.f.record["stages"]["gitops"] = dict(state="BOOTSTRAPPED", attempt=1, selectionReference=deepcopy(ref),
            releaseReference=deepcopy(self.f.record["bootstrapRelease"]), chartSha="e" * 40,
            internalAlbArn="arn:aws:elasticloadbalancing:us-east-1:058114477594:loadbalancer/app/techx/abcdef")
        self.assertEqual(next_operation(self.f.record, operation("bootstrap", "gitops")), operation("plan", "edge"))
        self.f.record["stages"]["edge"] = dict(state="APPLIED", attempt=1)
        self.assertEqual(next_operation(self.f.record, operation("apply", "edge")), dict(waiting="DNS_AND_ACCEPTANCE"))
        self.assertEqual(self.f.record["status"], "PROVISIONING")

    def test_bootstrap_revalidates_immutable_receipts_before_startbuild(self):
        self.applied()
        self.f.record.update(bootstrapRelease=dict(key="release"), bootstrapProposal=dict(key="proposal"), bootstrapChart=dict(
            key=f"chart-selections/staging/{self.f.record['sessionId']}/bootstrap.json", versionId="selection-v1", sha256="d" * 64))
        with patch("chart_selection.read_release", side_effect=ValueError("invalid cold receipt")) as release:
            with self.assertRaises(ValueError): self.dispatcher.advance()
            release.assert_called_once()
        self.assertFalse(self.started)

    def recovery(self):
        self.proofs = {}
        class Missing(Exception): pass
        def get(**kw):
            if kw["Key"] not in self.proofs: raise Missing()
            return dict(Body=io.BytesIO(self.proofs[kw["Key"]]))
        self.recovery_clients = dict(self.f.clients)
        self.recovery_clients["s3"] = SimpleNamespace(get_object=get, put_object=lambda **kw: self.proofs.update({kw["Key"]: kw["Body"]}),
            exceptions=SimpleNamespace(NoSuchKey=Missing))
        self.f.record["lifecycleDispatch"]["phase"] = "CALLING"
        self.f.record["lifecycleDispatch"].pop("buildId")
        self.f.clients["codebuild"].list_builds_for_project = lambda **kw: dict(ids=[self.support.build["id"]])

    def test_cleanup_recovers_unknown_start_by_exact_environment_and_persists_terminal_proof(self):
        self.recovery()
        arn = stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup")
        self.assertEqual(arn, self.support.build["arn"])
        proof = json.loads(next(iter(self.proofs.values())))
        self.assertTrue(proof["terminated"])
        self.assertEqual(proof["dispatchId"], self.f.record["lifecycleDispatch"]["id"])
        self.f.clients["codebuild"].list_builds_for_project = lambda **kw: self.fail("cached positive proof should avoid list propagation")
        self.assertEqual(stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup"), arn)

    def test_cleanup_unknown_start_never_uses_empty_or_truncated_listing_as_success(self):
        self.recovery()
        self.f.clients["codebuild"].list_builds_for_project = lambda **kw: dict(ids=[])
        with self.assertRaisesRegex(ValueError, "outcome remains unproven"):
            stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup")
        self.f.clients["codebuild"].list_builds_for_project = lambda **kw: dict(ids=[], nextToken="still-more")
        with self.assertRaisesRegex(ValueError, "outcome remains unproven"):
            stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup")
        self.assertFalse(self.proofs)

    def test_cleanup_recovery_follows_pages_without_invalid_sort_override(self):
        self.recovery()
        pages = []
        def listing(**kw):
            validate_parameters(kw, self.model.operation_model("ListBuildsForProject").input_shape)
            self.assertNotIn("sortOrder", kw)
            pages.append(kw.get("nextToken"))
            if not kw.get("nextToken"): return dict(ids=[], nextToken="page2")
            return dict(ids=[self.support.build["id"]])
        self.f.clients["codebuild"].list_builds_for_project = listing
        self.assertEqual(stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup"), self.support.build["arn"])
        self.assertEqual(pages, [None, "page2"])

    def test_cleanup_rejects_duplicate_matches_and_wrong_recovered_role(self):
        self.recovery()
        second = deepcopy(self.support.build)
        second["id"] = "techx-automation-lifecycle:fffff-123"
        self.f.clients["codebuild"].list_builds_for_project = lambda **kw: dict(ids=[self.support.build["id"], second["id"]])
        self.f.clients["codebuild"].batch_get_builds = lambda **kw: dict(builds=[deepcopy(self.support.build), second])
        with self.assertRaisesRegex(ValueError, "outcome remains unproven"):
            stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup")
        self.f.clients["codebuild"].list_builds_for_project = lambda **kw: dict(ids=[self.support.build["id"]])
        self.f.clients["codebuild"].batch_get_builds = lambda **kw: dict(builds=[deepcopy(self.support.build)])
        self.support.build["serviceRole"] = "untrusted"
        with self.assertRaises(ValueError): stop_dispatched_manager(self.recovery_clients, self.f.bucket, self.f.record, "cleanup")
        self.assertFalse(self.proofs)

    def test_cleanup_known_manager_stops_without_lease_and_rejects_wrong_service_role(self):
        self.prepared()
        self.assertNotIn("lease", self.f.record)
        stopped = []
        self.support.build["buildStatus"] = "IN_PROGRESS"
        def stop(**kw):
            stopped.append(kw)
            self.support.build["buildStatus"] = "STOPPED"
        self.f.clients["codebuild"].stop_build = stop
        with patch("cleanup_execution.stop_executor", side_effect=lambda client, arn, owner: stop_executor(client, arn, owner, sleep=lambda _: None)):
            self.assertEqual(stop_dispatched_manager(self.f.clients, self.f.bucket, self.f.record, "cleanup"), self.support.build["arn"])
        self.assertEqual(stopped, [dict(id=self.support.build["id"])])
        self.support.build["serviceRole"] = "untrusted"
        with self.assertRaises(ValueError): stop_dispatched_manager(self.f.clients, self.f.bucket, self.f.record, "cleanup")


if __name__ == "__main__": unittest.main()
