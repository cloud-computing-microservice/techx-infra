"""Actual active public -> manager -> private task -> VPN/runtime suite fixtures."""
from copy import deepcopy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from active_private_acceptance import ActivePrivateAcceptance
from active_private_contract import read_active_private_receipt
from automation_session import claim, release
from deployment_manager import DeploymentManager
from deployment_tasks import claim_task, finish_task, read_task
from lifecycle_dispatch import operation
from private_http import PrivateFailure
from accepted_release_history import finalize_active_release, read_accepted_history
from leased_command import execute
from plan_review_store import checksum, encoded


spec = importlib.util.spec_from_file_location("active_private_public_fixture", Path(__file__).with_name("test-active-public-acceptance.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class ActivePrivateTests(unittest.TestCase):
    def setUp(self, action=None):
        self.p = support.ActivePublicTests()
        self.p.setUp(action=action); self.addCleanup(self.p.doCleanups)
        p = self.p
        p.perform()
        self.f, self.d, self.r = p.f, p.d, p.r
        f = self.f
        f.record = release(f.record, p.worker.owner, f.record["lease"]["epoch"])
        p.run.update(status="completed", conclusion="success", updated_at=f.now.isoformat())
        p.r.h.observe(2)
        self.baseline = deepcopy(f.record["acceptedRelease"])
        self.cold = deepcopy(f.record["stages"]["private"])
        self.runtime = p.r.runtime
        self.manager = DeploymentManager(f.clients, f.store, self.d.execution, self.d.version, self.d.bucket,
            p.private_build["arn"], clock=lambda: f.now, sleep=lambda _: None)
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/active-private"
        self.pointer = self.manager.dispatch(self.runtime.private, "verify", "active", 1)
        self.dispatched = deepcopy(f.record)
        self.task_id = self.pointer["taskId"]
        self.task = claim_task(f.store, f.clients["s3"], self.d.bucket, self.task_id, f.instance_id, f.now)
        self.owner = f"arn:aws:ec2:us-east-1:058114477594:instance/{f.instance_id}"
        f.record = claim(f.record, self.owner, "acceptance", now=f.now)
        self.epoch = f.record["lease"]["epoch"]
        self.private = p.r.h.p.p.private
        # The cold fixture has already restarted Order once. A new acceptance
        # must observe another Pod replacement, never reuse that earlier UID.
        self.r.g.pods[2]["metadata"]["uid"] = "active-order-before"
        self.auth, self.persist, self.cleanup = True, True, False
        old_run = self.runtime.run
        def run(command, **kw):
            result = old_run(command, **kw)
            if "restart" in command and "deployment/order-api" in command:
                if not self.persist: p.service.orders.clear()
                if self.cleanup: f.record.update(status="DESTROYING")
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
                if path.startswith("/argocd/"): return (200, deepcopy(case.r.app)) if token else (401, dict(error="unauthorized"))
                if not p.service.orders and method == "GET": return 404, dict(error="not found")
                response = p.service.request(method, path, body=json.dumps(payload).encode() if payload else None,
                    headers={"Idempotency-Key": idempotency_key} if idempotency_key else {})
                return response["status"], json.loads(response["body"])
        self.worker = ActivePrivateAcceptance(self.runtime, f.store, self.d.version, self.task_id, clock=lambda: f.now,
            sleep=lambda _: None, tunnel_factory=self.private.worker.tunnel_factory, http_factory=HTTP)

    def perform(self): return self.worker.perform(self.owner, self.epoch)

    def test_full_active_private_suite_matches_new_release_and_retains_old_baseline(self):
        self.perform()
        proof = read_active_private_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(proof["kind"], "active-private-acceptance")
        self.assertEqual(proof["release"]["platformSha"], self.f.record["releaseCandidate"]["sourceSha"])
        self.assertEqual(proof["release"]["chartSha"], self.r.selected)
        self.assertTrue(proof["vpn"]["disconnected"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["stages"]["private"], self.cold)
        self.assertEqual(self.f.record["status"], "TESTING")
        self.assertNotIn("header.payload.signature", json.dumps(proof))
        self.f.record = release(self.f.record, self.owner, self.epoch)
        with self.assertRaises(ValueError): finish_task(self.f.store, self.task, True, self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "RUNNING")

    def test_duplicate_or_changed_public_context_cannot_reuse_the_task(self):
        with self.assertRaises(ValueError): claim_task(self.f.store, self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.f.record["releaseCandidate"]["publicAcceptance"]["receiptReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): read_task(self.f.clients["s3"], self.d.bucket, self.f.record, self.task_id, self.f.now)

    def test_old_cold_receipt_cannot_complete_active_private_acceptance(self):
        self.perform()
        self.f.record["releaseCandidate"]["privateAcceptance"]["receiptReference"] = deepcopy(self.cold["receiptReference"])
        with self.assertRaises(ValueError): read_active_private_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_wrong_runtime_digest_cannot_accept_release(self):
        self.r.g.pods[0]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_failed_private_auth_cannot_accept_release(self):
        self.auth = False
        with self.assertRaisesRegex(PrivateFailure, "argocd-authentication"): self.perform()
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_new_public_order_must_survive_another_restart(self):
        self.persist = False
        with self.assertRaisesRegex(PrivateFailure, "order-persistence"): self.perform()
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_cleanup_takeover_during_restart_fences_receipt_and_baseline(self):
        self.cleanup = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertNotIn("receiptReference", self.f.record["releaseCandidate"]["privateAcceptance"])

    def complete_parent(self, finish=True):
        if self.f.record.get("lease"): self.f.record = release(self.f.record, self.owner, self.epoch)
        case = self
        class Child:
            def wait(self, timeout):
                case.assertEqual(case.f.record["status"], "TESTING")
                case.worker.perform(case.owner, case.f.record["lease"]["epoch"])
                case.assertEqual(case.f.record["acceptedRelease"], case.baseline)
                return 0
            def poll(self): return 0
        writes, original = [], self.f.store.write
        def write(record, etag):
            writes.append(deepcopy(record)); original(record, etag)
        with patch.object(self.f.store, "write", side_effect=write):
            execute(self.f.store, self.owner, "acceptance", self.f.record["sessionId"], ["private-active-worker"], popen=lambda *a, **kw: Child(),
                success_status="READY", active_ready=lambda record: finalize_active_release(self.f.clients["s3"], self.d.bucket, record, self.d.version, now=self.f.now))
        ready = [record for record in writes if record["status"] == "READY"]
        self.assertEqual(len(ready), 1)
        self.assertNotIn("lease", ready[0]); self.assertNotIn("releaseCandidate", ready[0])
        self.assertEqual(self.f.record["stages"]["private"], self.cold)
        if finish: finish_task(self.f.store, self.task, True, self.f.now)

    def test_atomic_ready_history_and_actual_terminal_manager_are_required(self):
        self.complete_parent()
        accepted = read_accepted_history(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(accepted["chartSha"], self.r.selected)
        self.assertNotEqual(accepted, self.baseline)
        with self.assertRaises(ValueError): self.d.advance()
        self.p.private_build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.assertEqual(self.d.advance()["acceptedRelease"], accepted)
        self.assertEqual(self.p.r.h.observe(2)["waiting"], "READY")

    def test_next_publisher_uses_the_new_accepted_baseline_and_preserves_cold_source(self):
        from ready_release_publish import ReadyPublisher
        self.complete_parent()
        accepted, sources = deepcopy(self.f.record["acceptedRelease"]), deepcopy(self.f.record["sourceRevisions"])
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
        intent = publisher.prepare()
        self.assertEqual(intent["baseline"], accepted)
        self.assertEqual(self.f.record["sourceRevisions"], sources)
        self.assertNotEqual(self.f.record["releaseCandidate"]["id"], self.f.record["acceptedActive"]["candidate"]["id"])

    def test_child_failure_after_probe_success_cannot_publish_history_or_ready(self):
        import subprocess
        self.f.record = release(self.f.record, self.owner, self.epoch)
        case = self
        class Child:
            def wait(self, timeout):
                case.worker.perform(case.owner, case.f.record["lease"]["epoch"])
                return 1
            def poll(self): return 1
        with self.assertRaises(subprocess.CalledProcessError):
            execute(self.f.store, self.owner, "acceptance", self.f.record["sessionId"], ["active-private-worker"], popen=lambda *a, **kw: Child(),
                success_status="READY", active_ready=lambda record: finalize_active_release(self.f.clients["s3"], self.d.bucket, record, self.d.version, now=self.f.now))
        self.assertEqual(self.f.record["status"], "TESTING")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertNotIn("acceptedActive", self.f.record)
        with self.assertRaises(ValueError): finish_task(self.f.store, self.task, True, self.f.now)

    def test_cleanup_between_history_write_and_ready_cas_keeps_old_baseline(self):
        self.f.record = release(self.f.record, self.owner, self.epoch)
        case = self
        class Child:
            def wait(self, timeout):
                case.worker.perform(case.owner, case.f.record["lease"]["epoch"])
                return 0
            def poll(self): return 0
        def finalize(record):
            result = finalize_active_release(self.f.clients["s3"], self.d.bucket, record, self.d.version, now=self.f.now)
            current, etag = self.f.store.read(); current.update(status="DESTROYING")
            self.f.store.write(current, etag)
            return result
        with self.assertRaises(Exception):
            execute(self.f.store, self.owner, "acceptance", self.f.record["sessionId"], ["active-private-worker"], popen=lambda *a, **kw: Child(),
                success_status="READY", active_ready=finalize)
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertNotIn("acceptedActive", self.f.record)

    def test_tampered_history_snapshot_or_private_evidence_never_verifies(self):
        self.complete_parent()
        original = deepcopy(self.f.record)
        self.f.record["acceptedActive"]["previousAccepted"]["chartSha"] = "0" * 40
        with self.assertRaises(ValueError): read_accepted_history(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.f.record = original
        ref = self.f.record["acceptedActive"]["candidate"]["privateAcceptance"]["receiptReference"]
        body, object_version = self.r.artifacts[ref["key"]]
        changed = json.loads(body); changed["vpn"]["disconnected"] = False
        self.r.artifacts[ref["key"]] = encoded(changed), object_version
        with self.assertRaises(ValueError): read_accepted_history(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_fixed_loader_routes_active_private_parent_and_cannot_reuse_ready_task(self):
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
        def child(command, **kw):
            self.assertTrue(command[1].endswith("run_active_private_acceptance.py"))
            self.assertEqual(command[2:5], ["verify", "--stage", "active"])
            # The loader owns the task CAS; the parent owns the acceptance lease.
            self.owner = f"arn:aws:ec2:us-east-1:058114477594:instance/{self.f.instance_id}"
            self.epoch = None
            self.complete_parent(finish=False)
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory, patch("deployment_task_loader.load", side_effect=bundle), \
                patch("automation_session.S3SessionStore", return_value=self.f.store), patch("deployment_runtime.TerraformRuntime", return_value=self.runtime):
            run_task(self.f.clients, self.d.bucket, self.runtime.kms, self.task_id, directory, run=child, clock=lambda: self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")
        with self.assertRaises(ValueError): admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)

    def test_real_cli_parent_child_publish_ready_after_terminal_and_receipt_readback(self):
        import run_active_private_acceptance as cli
        self.f.record = release(self.f.record, self.owner, self.epoch)
        local = self.runtime.work / "active-private-execution.json"
        local.write_bytes(encoded(self.d.execution))
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.d.execution), self.d.version
        self.f.clients["secretsmanager"] = SimpleNamespace()
        class Child:
            def __init__(self, command, **kw): self.command = command
            def wait(self, timeout):
                with patch.object(sys, "argv", self.command[1:]): cli.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kw): return execute(*args, **kw, popen=Child)
        arguments = ["run_active_private_acceptance.py", "verify", "--stage", "active", "--attempt", "1", "--execution", str(local),
            "--execution-version", self.d.version, "--bucket", self.d.bucket, "--kms-key", self.runtime.kms, "--task-id", self.task_id]
        with patch.object(sys, "argv", arguments), patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch.object(cli, "TerraformRuntime", return_value=self.runtime), patch.object(cli, "S3SessionStore", return_value=self.f.store), \
                patch.object(cli, "ActivePrivateAcceptance", return_value=self.worker), patch.object(cli, "execute", side_effect=parent), redirect_stdout(io.StringIO()) as output:
            cli.main()
        self.assertIn('"state": "PRIVATE_PASSED"', output.getvalue())
        self.assertEqual(self.f.record["status"], "READY"); self.assertNotIn("lease", self.f.record)
        finish_task(self.f.store, self.task, True, self.f.now)

    def test_vpn_disconnect_is_required_for_active_private_receipt(self):
        self.private.disconnect = False
        with self.assertRaisesRegex(PrivateFailure, "vpn-disconnect-unproven"): self.perform()
        self.assertNotEqual(self.f.record["releaseCandidate"]["privateAcceptance"]["state"], "PASSED")

    def test_private_manager_and_executor_are_distinct_from_public_and_publisher(self):
        record = deepcopy(self.f.record); record.pop("lease")
        with self.assertRaises(ValueError): claim(record, self.p.worker.owner, "acceptance", now=self.f.now)
        with self.assertRaises(ValueError): claim(record, "cloud-computing-microservice/techx-platform:701:1", "acceptance", now=self.f.now)
        record["deploymentTask"]["instanceId"] = "i-00000000000000000"
        with self.assertRaises(ValueError): claim(record, self.owner, "acceptance", now=self.f.now)


if __name__ == "__main__": unittest.main()
