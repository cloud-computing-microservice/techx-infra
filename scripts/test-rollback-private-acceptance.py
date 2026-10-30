"""Restored public proof -> typed manager/task -> actual private suite fixtures."""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import botocore.session
from botocore.validate import validate_parameters

from automation_session import claim, release
from accepted_rollback_history import finalize_rollback
from accepted_release_history import read_accepted_history
from deployment_manager import DeploymentManager
from deployment_tasks import claim_task, finish_task, read_task, validate_result
from lifecycle_dispatch import operation
from leased_command import execute
from plan_review_store import encoded
from private_active_rollout import STAMP
from private_http import PrivateFailure
from rollback_private_acceptance import RollbackPrivateAcceptance
from rollback_private_contract import read_private_receipt, restored_manifest


spec = importlib.util.spec_from_file_location("rollback_private_public_fixture", Path(__file__).with_name("test-rollback-public-acceptance.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class RollbackPrivateTests(unittest.TestCase):
    def setUp(self):
        self.p = support.RollbackPublicTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        p = self.p
        p.perform(); p.terminal(); p.controller.observe_public()
        self.f, self.d, self.r = p.f, p.d, p.r
        f = self.f
        self.baseline, self.failed = deepcopy(f.record["acceptedRelease"]), deepcopy(f.record["releaseCandidate"])
        self.cold = deepcopy(f.record["stages"]["private"])
        self.runtime = self.r.runtime
        self.private = self.r.original.h.p.p.private
        self.build = deepcopy(self.r.build)
        self.build.update(id="techx-automation-lifecycle:abcdef96-1234",
            arn="arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:abcdef96-1234",
            buildStatus="IN_PROGRESS", buildComplete=False)
        old_batch = f.clients["codebuild"].batch_get_builds
        self.starts = []
        def start(**kwargs):
            validate_parameters(kwargs, botocore.session.get_session().get_service_model("codebuild").operation_model("StartBuild").input_shape)
            self.starts.append(deepcopy(kwargs))
            self.build["environment"] = dict(environmentVariables=deepcopy(kwargs["environmentVariablesOverride"]))
            return dict(build=deepcopy(self.build))
        f.clients["codebuild"].start_build = start
        f.clients["codebuild"].batch_get_builds = lambda **kw: dict(builds=[deepcopy(self.build)]) if kw["ids"] == [self.build["id"]] else old_batch(**kw)
        self.before_dispatch = deepcopy(f.record)
        self.d.start(deepcopy(f.record), operation("verify", "rollback"))
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/rollback-private"
        self.manager = DeploymentManager(f.clients, f.store, self.d.execution, self.d.version, self.d.bucket,
            self.build["arn"], clock=lambda: f.now, sleep=lambda _: None)
        self.pointer = self.manager.dispatch(self.runtime.private, "verify", "rollback", 1)
        self.dispatched = deepcopy(f.record)
        self.task_id = self.pointer["taskId"]
        self.task = claim_task(f.store, f.clients["s3"], self.d.bucket, self.task_id, f.instance_id, f.now)
        self.owner = self.r.owner
        f.record = claim(f.record, self.owner, "rollback", now=f.now)
        self.epoch = f.record["lease"]["epoch"]
        self.r.kube.pods[2]["metadata"]["uid"] = "rollback-order-before"
        self.auth, self.persist, self.cleanup, self.bad_http_identity = True, True, False, False
        old_run = self.runtime.run
        def run(command, **kwargs):
            result = old_run(command, **kwargs)
            if "restart" in command and "deployment/order-api" in command:
                if not self.persist: p.service.orders.clear()
                if self.cleanup: f.record["status"] = "DESTROYING"
            return result
        self.runtime.run = run
        case = self
        class HTTP:
            def __init__(self, tunnel, guard): self.guard, self.observations = guard, []
            def request(self, method, path, *, payload=None, token=None, idempotency_key=None):
                self.guard()
                self.observations.append(dict(method=method, path=path, status=200))
                if path == "/argocd/api/v1/session": return (200, dict(token="header.payload.signature")) if case.auth else (401, {})
                if path == "/argocd/api/v1/session/userinfo": return 200, dict(loggedIn=True, username="admin")
                if path.startswith("/argocd/"):
                    app = deepcopy(case.r.app)
                    if case.bad_http_identity: app["metadata"]["uid"] = "another-application"
                    return (200, app) if token else (401, {})
                if not p.service.orders and method == "GET": return 404, dict(error="not found")
                result = p.service.request(method, path, body=json.dumps(payload).encode() if payload else None,
                    headers={"Idempotency-Key": idempotency_key} if idempotency_key else {})
                return result["status"], json.loads(result["body"])
        self.worker = RollbackPrivateAcceptance(self.runtime, f.store, self.d.version, self.task_id, clock=lambda: f.now,
            sleep=lambda _: None, tunnel_factory=self.private.worker.tunnel_factory, http_factory=HTTP)

    def perform(self): return self.worker.perform(self.owner, self.epoch)

    def assert_unaccepted(self):
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["releaseCandidate"], self.failed)
        self.assertEqual(self.f.record["stages"]["private"], self.cold)

    def test_private_suite_tests_restored_images_and_new_public_order_without_ready(self):
        self.perform()
        proof = read_private_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(proof["kind"], "rollback-private-acceptance")
        self.assertEqual(proof["release"]["chartSha"], self.r.chosen)
        self.assertEqual(proof["release"]["platformSha"], self.baseline["platformSha"])
        self.assertEqual(proof["release"]["images"], self.baseline["images"])
        self.assertTrue(proof["vpn"]["disconnected"])
        self.assertNotIn("header.payload.signature", json.dumps(proof))
        self.assertEqual(len(self.starts), 1)
        self.assert_unaccepted()
        self.f.record = release(self.f.record, self.owner, self.epoch)
        with self.assertRaises(ValueError): validate_result(self.f.record, self.task)
        with self.assertRaises(ValueError): finish_task(self.f.store, self.task, True, self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "RUNNING")

    def test_duplicate_or_changed_public_receipt_does_not_reuse_task(self):
        with self.assertRaises(ValueError): claim_task(self.f.store, self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.f.record["rollbackAttempt"]["publicAcceptance"]["receiptReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): read_task(self.f.clients["s3"], self.d.bucket, self.f.record, self.task_id, self.f.now)

    def test_cold_receipt_cannot_replace_restored_private_proof(self):
        self.perform()
        self.f.record["rollbackAttempt"]["privateAcceptance"]["receiptReference"] = deepcopy(self.cold["receiptReference"])
        with self.assertRaises(ValueError): read_private_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assert_unaccepted()

    def test_runtime_digest_mismatch_cannot_accept(self):
        self.r.kube.pods[0]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assert_unaccepted()

    def test_authenticated_argo_must_have_original_uid_and_restore_stamp(self):
        self.bad_http_identity = True
        with self.assertRaisesRegex(PrivateFailure, "rollback-application-identity"): self.perform()
        self.assert_unaccepted()

    def test_argo_stamp_cannot_change_after_restoration(self):
        self.r.app["metadata"]["annotations"][STAMP] = "0" * 64
        with self.assertRaisesRegex(PrivateFailure, "rollback-application-identity"): self.perform()
        self.assert_unaccepted()

    def test_application_replaced_during_readiness_cannot_publish_private_receipt(self):
        run = self.runtime.run
        def changed(command, **kwargs):
            result = run(command, **kwargs)
            if "pods" in command: self.r.app["metadata"]["uid"] = "changed-during-readiness"
            return result
        self.runtime.run = changed
        with self.assertRaisesRegex(PrivateFailure, "rollback-application-identity"): self.perform()
        self.assert_unaccepted()

    def test_private_authentication_failure_cannot_accept(self):
        self.auth = False
        with self.assertRaisesRegex(PrivateFailure, "argocd-authentication"): self.perform()
        self.assert_unaccepted()

    def test_fresh_rollback_public_order_must_survive_restart(self):
        self.persist = False
        with self.assertRaisesRegex(PrivateFailure, "order-persistence"): self.perform()
        self.assert_unaccepted()

    def test_vpn_disconnect_must_be_positive(self):
        self.private.disconnect = False
        with self.assertRaisesRegex(PrivateFailure, "vpn-disconnect-unproven"): self.perform()
        self.assert_unaccepted()

    def test_cleanup_takeover_fences_receipt(self):
        self.cleanup = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertNotIn("receiptReference", self.f.record["rollbackAttempt"]["privateAcceptance"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_expiry_and_immutable_execution_change_cannot_accept(self):
        original = deepcopy(self.f.record)
        self.f.record["createdAt"] = (self.f.now - timedelta(minutes=1)).isoformat()
        with self.assertRaises(ValueError): self.perform()
        self.f.record = original
        self.f.now += timedelta(minutes=31)
        with self.assertRaises(ValueError): self.perform()
        self.assert_unaccepted()

    def test_manifest_is_the_restored_accepted_source_not_failed_candidate(self):
        manifest = restored_manifest(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)["manifest"]
        self.assertEqual(manifest["commit"], self.baseline["platformSha"])
        self.assertNotEqual(manifest["commit"], self.failed["sourceSha"])

    def complete_parent(self, *, finish=True, child_code=0, finalize=None):
        if self.f.record.get("lease"): self.f.record = release(self.f.record, self.owner, self.f.record["lease"]["epoch"])
        case = self
        class Child:
            def wait(self, timeout):
                case.assert_unaccepted()
                case.worker.perform(case.owner, case.f.record["lease"]["epoch"])
                case.assert_unaccepted()
                return child_code
            def poll(self): return child_code
        writes, original = [], self.f.store.write
        def write(record, etag):
            writes.append(deepcopy(record)); original(record, etag)
        with patch.object(self.f.store, "write", side_effect=write):
            execute(self.f.store, self.owner, "rollback", self.f.record["sessionId"], ["private-rollback-worker"], popen=lambda *a, **kw: Child(),
                success_status="READY", rollback_ready=finalize or (lambda record: finalize_rollback(self.f.clients["s3"], self.d.bucket, record, self.d.version, now=self.f.now)))
        ready = [record for record in writes if record["status"] == "READY"]
        self.assertEqual(len(ready), 1)
        self.assertNotIn("lease", ready[0]); self.assertNotIn("releaseCandidate", ready[0]); self.assertNotIn("acceptedActive", ready[0])
        self.assertEqual(self.f.record["stages"]["private"], self.cold)
        if finish: finish_task(self.f.store, self.task, True, self.f.now)

    def test_atomic_ready_history_and_actual_terminal_manager(self):
        self.complete_parent()
        accepted = read_accepted_history(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(accepted["chartSha"], self.r.chosen)
        self.assertEqual(accepted["images"], self.baseline["images"])
        self.assertEqual(self.f.record["rollbackAttempt"], self.f.record["acceptedRollback"]["rollback"])
        self.assertEqual(self.manager.wait(self.pointer)["state"], "SUCCEEDED")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.assertEqual(self.p.controller.observe_private()["waiting"], "ROLLBACK_PRIVATE_RUNNING")
        self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.assertEqual(self.p.controller.observe_private()["acceptedRelease"], accepted)
        self.assertEqual(self.d.advance()["acceptedRelease"], accepted)
        self.assertEqual(len(self.starts), 1)

    def test_controller_consumes_one_private_dispatch_and_does_not_replay_unknown(self):
        self.f.record = deepcopy(self.before_dispatch)
        self.starts.clear()
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.assertEqual(self.p.controller.observe_private()["waiting"], "ROLLBACK_PRIVATE_RUNNING")
        self.assertEqual(self.p.controller.observe_private()["waiting"], "ROLLBACK_PRIVATE_RUNNING")
        self.assertEqual(len(self.starts), 1)
        self.f.record["lifecycleDispatch"]["phase"] = "CALLING"
        with self.assertRaises(ValueError): self.p.controller.observe_private()
        self.assertEqual(len(self.starts), 1)
        self.assertTrue(self.f.cleanup)

    def test_failed_private_manager_requires_cleanup_without_accepting_baseline(self):
        self.build.update(buildStatus="FAILED", buildComplete=True)
        self.f.record = release(self.f.record, self.owner, self.epoch)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        with self.assertRaises(ValueError): self.p.controller.observe_private()
        self.assertTrue(self.f.cleanup)
        self.assert_unaccepted()

    def test_late_rollback_callback_cannot_clean_up_a_new_release_or_session(self):
        self.complete_parent()
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.f.record["lifecycleDispatch"]["operation"] = operation("verify", "active")
        with self.assertRaises(ValueError): self.p.controller.observe_private()
        self.assertFalse(self.f.cleanup)
        self.f.record["executionVersion"] = "another-execution"
        with self.assertRaises(ValueError): self.p.controller.observe_private()
        self.assertFalse(self.f.cleanup)

    def test_next_publisher_uses_restored_baseline_and_preserves_one_attempt(self):
        from ready_release_publish import ReadyPublisher
        self.complete_parent()
        accepted, attempt = deepcopy(self.f.record["acceptedRelease"]), deepcopy(self.f.record["rollbackAttempt"])
        source, owner, repo = "d" * 40, "cloud-computing-microservice/techx-platform:702:1", "cloud-computing-microservice/techx-platform"
        def get(endpoint):
            if endpoint == "actions/runs/702":
                return dict(id=702, run_attempt=1, status="in_progress", event="push", head_branch="main", head_sha=source,
                    path=".github/workflows/verify.yml", repository=dict(full_name=repo), head_repository=dict(full_name=repo))
            if endpoint == "actions/runs/702/jobs?filter=latest&per_page=100":
                return dict(total_count=2, jobs=[dict(id=901, name="acceptance / acceptance", run_id=702, head_sha=source, status="completed", conclusion="success"),
                    dict(id=902, name="release / publish", run_id=702, head_sha=source, status="in_progress")])
            if endpoint == "git/ref/heads/main": return dict(object=dict(sha=source))
            if endpoint == f"compare/{accepted['platformSha']}...{source}": return dict(status="ahead", merge_base_commit=dict(sha=accepted["platformSha"]))
            raise AssertionError(endpoint)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-publisher/techx-publish-702"
        publisher = ReadyPublisher(self.f.clients, self.f.store, self.d.bucket, self.d.execution, self.d.version,
            source, owner, github=SimpleNamespace(get=get), clock=lambda: self.f.now)
        self.f.record = claim(self.f.record, owner, "release", now=self.f.now)
        publisher.epoch = self.f.record["lease"]["epoch"]
        self.assertEqual(publisher.prepare()["baseline"], accepted)
        self.assertEqual(self.f.record["rollbackAttempt"], attempt)

    def test_failed_child_after_passed_probe_cannot_publish_ready(self):
        with self.assertRaises(subprocess.CalledProcessError): self.complete_parent(child_code=1)
        self.assert_unaccepted()
        self.assertNotIn("acceptedRollback", self.f.record)

    def test_cleanup_race_after_history_write_keeps_old_baseline(self):
        def finalize(record):
            payload = finalize_rollback(self.f.clients["s3"], self.d.bucket, record, self.d.version, now=self.f.now)
            current, etag = self.f.store.read()
            current["status"] = "DESTROYING"
            self.f.store.write(current, etag)
            return payload
        with self.assertRaises(Exception): self.complete_parent(finalize=finalize)
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertNotIn("acceptedRollback", self.f.record)

    def test_tampered_history_or_retired_attempt_cannot_verify(self):
        self.complete_parent()
        original = deepcopy(self.f.record)
        self.f.record["acceptedRollback"]["previousAccepted"]["chartSha"] = "0" * 40
        with self.assertRaises(ValueError): read_accepted_history(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.f.record = original
        self.f.record.pop("rollbackAttempt")
        with self.assertRaises(ValueError): read_accepted_history(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_loader_routes_fixed_private_parent_and_cannot_reuse_ready_task(self):
        sys.path.insert(0, str(Path(__file__).parents[1] / "bootstrap/automation"))
        self.addCleanup(sys.path.pop, 0)
        from deployment_task_loader import admitted_task, run_task
        self.f.record = deepcopy(self.dispatched)
        self.f.identity["Arn"] = f"arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-executor/{self.f.instance_id}"
        admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        def bundle(client, bucket, session, version, directory):
            directory.mkdir(parents=True)
            (directory / "execution.json").write_text(json.dumps(self.d.execution))
            return directory / "source/techx-infra"
        def child(command, **kwargs):
            self.assertTrue(command[1].endswith("run_rollback_private_acceptance.py"))
            self.assertEqual(command[2:5], ["verify", "--stage", "rollback"])
            self.complete_parent(finish=False)
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory, patch("deployment_task_loader.load", side_effect=bundle), \
                patch("automation_session.S3SessionStore", return_value=self.f.store), patch("deployment_runtime.TerraformRuntime", return_value=self.runtime):
            run_task(self.f.clients, self.d.bucket, self.runtime.kms, self.task_id, directory, run=child, clock=lambda: self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")
        with self.assertRaises(ValueError): admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)

    def test_real_cli_parent_child_validate_proof_before_ready(self):
        import run_rollback_private_acceptance as cli
        self.f.record = release(self.f.record, self.owner, self.epoch)
        local = self.runtime.work / "rollback-private-execution.json"
        local.write_bytes(encoded(self.d.execution))
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.d.execution), self.d.version
        self.f.clients["secretsmanager"] = SimpleNamespace()
        class Child:
            def __init__(self, command, **kwargs): self.command = command
            def wait(self, timeout):
                with patch.object(sys, "argv", self.command[1:]): cli.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kwargs): return execute(*args, **kwargs, popen=Child)
        arguments = ["run_rollback_private_acceptance.py", "verify", "--stage", "rollback", "--attempt", "1", "--execution", str(local),
            "--execution-version", self.d.version, "--bucket", self.d.bucket, "--kms-key", self.runtime.kms, "--task-id", self.task_id]
        with patch.object(sys, "argv", arguments), patch("boto3.client", side_effect=lambda name, **kwargs: self.f.clients[name]), \
                patch.object(cli, "TerraformRuntime", return_value=self.runtime), patch.object(cli, "S3SessionStore", return_value=self.f.store), \
                patch.object(cli, "RollbackPrivateAcceptance", return_value=self.worker), patch.object(cli, "execute", side_effect=parent), redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertIn('"state": "PRIVATE_PASSED"', output.getvalue())
        self.assertEqual(self.f.record["status"], "READY"); self.assertNotIn("lease", self.f.record)
        finish_task(self.f.store, self.task, True, self.f.now)


if __name__ == "__main__": unittest.main()
