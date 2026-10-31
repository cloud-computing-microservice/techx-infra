"""Real rollback manager/task/lease, Git proposal and immutable private readback."""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation_session import claim, release
from bootstrap_proposal import blob_sha
from deployment_manager import DeploymentManager
from deployment_tasks import claim_task, finish_task, read_task
from lifecycle_dispatch import operation
from private_rollback_prepare import PrivateRollbackPrepare
from rollback_task_contract import context, key, read_proposal_receipt, slot


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


reservation = load("private_rollback_reservation_fixture", "test-rollback-reservation.py")
proposal = load("private_rollback_git_fixture", "test-rollback-proposal.py")


class PrivatePrepareTests(unittest.TestCase):
    def setUp(self):
        self.r = reservation.ReservationTests()
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        self.r.arm()
        self.f, self.d = self.r.f, self.r.d
        f = self.f
        self.baseline = deepcopy(f.record["acceptedRelease"])
        self.sources = deepcopy(f.record["sourceRevisions"])
        self.intent = deepcopy(slot(f.record)["intent"])
        self.manager_arn = "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:abcdef96-1234"
        f.record["lifecycleDispatch"] = dict(id="6" * 64, phase="OBSERVED",
            buildId=self.manager_arn.split("build/", 1)[1], operation=operation("rollback", "prepare"))
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/codebuild-rollback"
        self.execution = self.r.a.execution
        self.runtime = self.r.a.p.r.runtime
        self.runtime.execution, self.runtime.clients = self.execution, f.clients
        self.manager = DeploymentManager(f.clients, f.store, self.execution, self.d.version, self.d.bucket,
            self.manager_arn, clock=lambda: f.now, sleep=lambda _: None)
        self.pointer = self.manager.dispatch(self.runtime.private, "rollback", "prepare", 1)
        self.dispatched = deepcopy(f.record)
        self.task_id = self.pointer["taskId"]
        self.task = claim_task(f.store, f.clients["s3"], self.d.bucket, self.task_id, f.instance_id, f.now)
        self.owner = f"arn:aws:ec2:us-east-1:058114477594:instance/{f.instance_id}"
        f.record = claim(f.record, self.owner, "rollback", now=f.now)
        self.epoch = f.record["lease"]["epoch"]
        self.policy = deepcopy(self.r.a.p.r.h.p.policy)
        self.git_fixture = proposal.RevertTests()
        self.git_fixture.setUp(); self.addCleanup(self.git_fixture.doCleanups)
        self.chart = self.git_fixture.p
        self.prepare_chart()
        self.worker = PrivateRollbackPrepare(self.runtime, f.store, self.d.version, self.task_id, clock=lambda: f.now,
            clients=lambda *args: (self.chart.github, self.r.a.p.r.h.infra, None))

    def prepare_chart(self):
        p, intent = self.chart, self.intent
        renderer = load("private_rollback_renderer", "../../techx-platform/scripts/prepare-chart-release.py")
        names = dict(frontend="frontend", catalog="catalog-api", order="order-api")
        baseline = intent["acceptedBaseline"]
        manifest = deepcopy(self.git_fixture.accepted_manifest)
        manifest.update(commit=baseline["platformSha"], tag="staging-" + baseline["platformSha"][:12])
        for row in manifest["images"]:
            repository, digest = baseline["images"][names[row["component"]]].split("@")
            row.update(image=repository + ":" + manifest["tag"], digest=digest)
        values = (p.source / "techx-chart/values-staging.yaml").read_text()
        accepted_tree = deepcopy(self.git_fixture.accepted_tree)
        for path, data in {"values-staging.yaml": renderer.render_values(values, manifest).encode(),
                           "releases/staging.json": json.dumps(manifest).encode()}.items():
            digest = blob_sha(data); p.blobs[digest] = data
            accepted_tree[path] = dict(sha=digest, mode="100644")
        tree_sha = "3" * 40
        p.trees[tree_sha] = accepted_tree
        p.commits[baseline["chartSha"]] = dict(sha=baseline["chartSha"], tree=dict(sha=tree_sha), parents=[])
        p.base = p.main = intent["failedChartSha"]
        p.commits[p.base] = dict(sha=p.base, tree=dict(sha=p.base_tree), parents=[dict(sha=baseline["chartSha"])])
        p.proposal = SimpleNamespace(branch=f"rollback/{intent['sessionId']}-{intent['rollbackId'][:24]}")
        get = p.github.get
        p.github.get = lambda endpoint: deepcopy(self.policy) if endpoint == "branches/main/protection" else get(endpoint)
        self.runtime.source = p.source

    def perform(self): return self.worker.perform(self.owner, self.epoch)

    def test_private_proposal_completes_one_terminal_task_without_ready_merge_or_baseline_change(self):
        result = self.perform()
        receipt = read_proposal_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(result["state"], "ROLLBACK_PROPOSED")
        self.assertEqual(receipt["taskId"], self.task_id)
        self.assertEqual(receipt["rollbackContext"], context(self.f.record))
        self.assertEqual(receipt["proposal"]["acceptedBaseline"], self.baseline)
        self.assertFalse(self.chart.pr["merged_at"])
        self.f.record = release(self.f.record, self.owner, self.epoch)
        finish_task(self.f.store, self.task, True, self.f.now)
        self.f.remote_result = lambda: None
        self.f.statuses = ["Success"]
        self.assertEqual(self.manager.wait(self.pointer)["state"], "SUCCEEDED")
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["sourceRevisions"], self.sources)
        self.assertNotIn("lease", self.f.record)

    def test_consumed_task_cannot_replay_or_create_another_branch_or_pr(self):
        self.perform(); count = len(self.chart.calls)
        with self.assertRaises(ValueError): self.perform()
        with self.assertRaises(ValueError): claim_task(self.f.store, self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.assertEqual(len(self.chart.calls), count)
        self.assertEqual(sum(path == "pulls" for path, _ in self.chart.calls), 1)

    def test_conflicting_fixed_intent_prevents_all_git_mutations(self):
        self.f.objects[key(self.f.record, "intent")] = b'{}', "hostile-v1"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.chart.calls)

    def test_wrong_executor_lease_or_task_context_is_rejected(self):
        with self.assertRaises(ValueError): self.worker.perform(self.owner + "0", self.epoch)
        self.f.record["deploymentTask"]["expectedRollbackContext"]["rollbackId"] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.chart.calls)

    def test_cleanup_during_git_write_fences_later_proposal_and_preserves_baseline(self):
        self.chart.on_post = lambda endpoint: self.f.record.update(status="DESTROYING")
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertNotIn("proposalReference", self.f.record["rollbackAttempt"])
        self.assertFalse(self.chart.pr)
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_moved_main_cannot_open_pr(self):
        self.chart.main = "0" * 40
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.chart.calls)

    def test_changed_branch_policy_prevents_intent_and_git_writes(self):
        self.policy["allow_force_pushes"]["enabled"] = True
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.chart.calls)
        self.assertNotIn(key(self.f.record, "intent"), self.f.objects)

    def test_expired_original_intent_cannot_extend_task_or_write_artifacts(self):
        self.f.now += timedelta(minutes=31)
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.chart.calls)
        self.assertNotIn(key(self.f.record, "intent"), self.f.objects)

    def test_malformed_reservation_and_boolean_schema_are_rejected(self):
        for value in (None, [], dict(self.f.record["rollbackAttempt"], schemaVersion=True)):
            with self.subTest(value=value), self.assertRaises(ValueError): slot(dict(self.f.record, rollbackAttempt=value))

    def test_manager_requires_immutable_execution_opt_in_not_only_mutable_slot(self):
        original = self.manager.execution["action"]
        self.manager.execution["action"] = "deploy-window"
        with self.assertRaises(ValueError): self.manager.current()
        self.manager.execution["action"] = original
        self.manager.execution["createdAt"] = (self.f.now - timedelta(minutes=3)).isoformat()
        with self.assertRaises(ValueError): self.manager.current()

    def test_parent_worker_cli_releases_lease_and_requires_versioned_readback(self):
        import run_rollback_prepare
        from leased_command import execute
        self.f.record = release(self.f.record, self.owner, self.epoch)
        local = self.runtime.work / "rollback-cli-execution.json"
        local.write_text(json.dumps(self.execution))
        self.f.clients["secretsmanager"] = SimpleNamespace()  # Credential clients are explicitly injected above.
        children = []
        case = self
        class Child:
            def __init__(self, command, **kwargs): self.command = command; children.append(command)
            def wait(self, timeout):
                case.assertEqual(case.f.record["status"], "FAILED_NEEDS_CLEANUP")
                with patch.object(sys, "argv", self.command[1:]): run_rollback_prepare.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kwargs): return execute(*args, **kwargs, popen=Child)
        arguments = ["run_rollback_prepare.py", "rollback", "--stage", "prepare", "--attempt", "1",
            "--execution", str(local), "--execution-version", self.d.version, "--bucket", self.d.bucket,
            "--kms-key", self.runtime.kms, "--task-id", self.task_id]
        with patch.object(sys, "argv", arguments), patch("boto3.client", side_effect=lambda name, **kwargs: self.f.clients[name]), \
                patch("run_rollback_prepare.TerraformRuntime", return_value=self.runtime), \
                patch("run_rollback_prepare.S3SessionStore", return_value=self.f.store), \
                patch("run_rollback_prepare.PrivateRollbackPrepare", return_value=self.worker), \
                patch("run_rollback_prepare.execute", side_effect=parent), redirect_stdout(io.StringIO()) as output:
            run_rollback_prepare.main()
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0][-1], "--worker")
        self.assertIn('"state": "ROLLBACK_PROPOSED"', output.getvalue())
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        finish_task(self.f.store, self.task, True, self.f.now)

    def test_ami_loader_accepts_only_unconsumed_failed_session_preparation(self):
        bootstrap = Path(__file__).parents[1] / "bootstrap/automation"
        sys.path.insert(0, str(bootstrap))
        try:
            spec = importlib.util.spec_from_file_location("rollback_ami_loader", bootstrap / "deployment_task_loader.py")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        finally: sys.path.pop(0)
        self.f.record = deepcopy(self.dispatched)
        loaded = module.admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.assertEqual(loaded["expectedRollbackContext"], self.task["expectedRollbackContext"])
        for status in ("PROVISIONING", "TESTING", "READY", "DESTROYING"):
            self.f.record["status"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                module.admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.f.record = deepcopy(self.dispatched)
        self.f.record["deploymentTask"]["state"] = "RUNNING"
        with self.assertRaises(ValueError): module.admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)


if __name__ == "__main__": unittest.main()
