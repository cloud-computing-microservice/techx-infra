"""Real consumed grant, dispatch/task, one merge, exact CI and Argo restore."""
from copy import deepcopy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import botocore.session
from botocore.validate import validate_parameters

from automation_session import claim, release
from deployment_manager import DeploymentManager
from deployment_tasks import claim_task, finish_task, read_task
from lifecycle_dispatch import completed_manager, operation
from private_active_rollout import STAMP
from private_chart_promotion import MergeUncertain
from private_rollback_rollout import PrivateRollbackRollout
from rollback_rollout_contract import read_rollback_rollout
from rollback_task_contract import rollout_context

spec = importlib.util.spec_from_file_location("rollback_rollout_grant_fixture", Path(__file__).with_name("test-rollback-grant.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class RollbackRolloutTests(unittest.TestCase):
    def setUp(self):
        self.setup_rollout()

    def setup_rollout(self, *, controller_dispatch=False):
        self.g = support.GrantTests()
        self.g.setUp(); self.addCleanup(self.g.doCleanups)
        self.f, self.d = self.g.f, self.g.d
        self.review = self.g.p
        self.preparation = self.review.p.c
        self.p = self.preparation.p
        self.runtime, self.chart, self.infra = self.p.runtime, self.p.chart, self.g.infra
        self.original = self.p.r.a.p.r
        self.app, self.kube = self.original.app, self.original.g
        self.baseline = deepcopy(self.f.record["acceptedRelease"])
        self.failed_candidate = deepcopy(self.f.record["releaseCandidate"])
        self.sources = deepcopy(self.f.record["sourceRevisions"])
        self.chosen = "8" * 40
        self.ci = dict(deepcopy(self.review.p.ci), id=1989, head_sha=self.chosen, head_branch="main", event="push")
        self.merge_calls, self.patch_calls = [], []
        self.lost_merge = self.lost_patch = self.bad_uid = self.cleanup_on_merge = self.wrong_runtime = False
        self.lost_calling = self.lost_app_calling = False
        self.move_main_after_calling = False
        self.stamp_race = self.uid_after_readiness = False
        self.install_git()
        self.install_kubernetes()
        self.install_storage_faults()
        self.build = deepcopy(self.preparation.build)
        self.build.update(id="techx-automation-lifecycle:abcdef95-1234",
            arn="arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:abcdef95-1234", buildStatus="IN_PROGRESS", buildComplete=False)
        batch = self.f.clients["codebuild"].batch_get_builds
        self.starts = []
        def start(**kwargs):
            validate_parameters(kwargs, botocore.session.get_session().get_service_model("codebuild").operation_model("StartBuild").input_shape)
            self.starts.append(deepcopy(kwargs))
            self.build["environment"] = dict(environmentVariables=deepcopy(kwargs["environmentVariablesOverride"]))
            return dict(build=deepcopy(self.build))
        self.f.clients["codebuild"].start_build = start
        self.f.clients["codebuild"].batch_get_builds = lambda **kwargs: dict(builds=[deepcopy(self.build)]) if kwargs["ids"] == [self.build["id"]] else batch(**kwargs)
        if controller_dispatch:
            from rollback_release_controller import RollbackReleaseController
            self.release_controller = RollbackReleaseController(self.d, self.infra, self.chart.github, self.runtime.source, "300")
            self.assertEqual(self.release_controller.observe()["waiting"], "ROLLBACK_ROLLOUT_RUNNING")
        else:
            self.g.consume()
            self.d.start(deepcopy(self.f.record), operation("rollback", "rollout"))
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/codebuild-rollback-rollout"
        self.manager = DeploymentManager(self.f.clients, self.f.store, self.d.execution, self.d.version, self.d.bucket,
            self.build["arn"], clock=lambda: self.f.now, sleep=lambda _: None)
        self.pointer = self.manager.dispatch(self.runtime.private, "rollback", "rollout", 1)
        self.dispatched = deepcopy(self.f.record)
        self.task_id = self.pointer["taskId"]
        self.task = claim_task(self.f.store, self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.owner = self.p.owner
        self.f.record = claim(self.f.record, self.owner, "rollback", now=self.f.now)
        self.epoch = self.f.record["lease"]["epoch"]
        self.worker = PrivateRollbackRollout(self.runtime, self.f.store, self.d.version, self.task_id,
            clock=lambda: self.f.now, sleep=lambda _: None, clients=lambda *args: (self.chart.github, self.infra))

    def install_git(self):
        get = self.chart.github.get
        def chart_get(endpoint):
            if endpoint == f"actions/workflows/verify.yml/runs?head_sha={self.chosen}&per_page=100": return dict(total_count=1, workflow_runs=[deepcopy(self.ci)])
            if endpoint == "actions/runs/1989": return deepcopy(self.ci)
            return get(endpoint)
        def merge(number, head, title, message):
            self.assertEqual((number, head), (7, self.chart.branch))
            self.merge_calls.append((number, head))
            self.chart.pr.update(merged=True, state="closed", merge_commit_sha=self.chosen,
                merged_by=dict(type="Bot", login="techx-promotion[bot]"), merged_at=self.f.now.isoformat())
            self.chart.commits[self.chosen] = dict(sha=self.chosen, tree=deepcopy(self.chart.commits[head]["tree"]),
                parents=[dict(sha=self.chart.base), dict(sha=head)], message=title + "\n\n" + message)
            self.chart.main = self.chosen
            if self.cleanup_on_merge: self.f.record["status"] = "DESTROYING"
            if self.lost_merge: raise TimeoutError("Merge succeeded, response lost")
            return dict(merged=True, sha=self.chosen)
        self.chart.github.get, self.chart.github.merge = chart_get, merge

    def install_kubernetes(self):
        run = self.runtime.run
        def execute(command, **kwargs):
            if "patch" not in command:
                if self.uid_after_readiness and self.patch_calls and "pods" in command:
                    self.app["metadata"]["uid"] = "replaced-after-application-read"
                return run(command, **kwargs)
            self.assertIn("--type=json", command)
            self.assertIn("--patch-file=/dev/stdin", command)
            operations = json.loads(kwargs["input"])
            self.patch_calls.append(deepcopy(operations))
            if self.bad_uid: self.app["metadata"]["uid"] = "replaced-application"
            if self.stamp_race: self.app["metadata"]["annotations"][STAMP] = "0" * 64
            # Apply the supported JSON Patch operations atomically like the API.
            updated = deepcopy(self.app)
            for op in operations:
                target = updated
                parts = [part.replace("~1", "/").replace("~0", "~") for part in op["path"].lstrip("/").split("/")]
                for part in parts[:-1]: target = target[part]
                if op["op"] == "test":
                    if target.get(parts[-1]) != op["value"]: raise ValueError("Atomic JSON Patch predecessor test failed")
                else: target[parts[-1]] = deepcopy(op["value"])
            self.app.clear(); self.app.update(updated)
            self.app["status"] = dict(sync=dict(status="Synced", revision=self.chosen), health=dict(status="Healthy"))
            for name, image in self.baseline["images"].items():
                self.kube.objects[("deployment", name, "techx-staging")]["spec"]["template"]["spec"]["containers"][0]["image"] = image
            for pod in self.kube.pods:
                name = pod["metadata"]["labels"]["app.kubernetes.io/component"]
                pod["spec"]["containers"][0]["image"] = self.baseline["images"][name]
                pod["status"]["containerStatuses"][0]["imageID"] = self.baseline["images"][name]
            if self.wrong_runtime: self.kube.pods[0]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "0" * 64
            if self.lost_patch: raise TimeoutError("Patch succeeded, response lost")
            return SimpleNamespace(stdout=json.dumps(self.app).encode())
        self.runtime.run = execute

    def install_storage_faults(self):
        put = self.f.clients["s3"].put_object
        def write(**kwargs):
            result = put(**kwargs)
            key = kwargs["Key"]
            if self.move_main_after_calling and key.startswith("promotions/") and key.endswith("/calling.json"):
                self.chart.main = "0" * 40
            if self.lost_calling and key.startswith("promotions/") and key.endswith("/calling.json"): raise TimeoutError("Stored merge marker; unknown response")
            if self.lost_app_calling and "/rollback/application/" in key and key.endswith("/calling.json"): raise TimeoutError("Stored patch marker; unknown response")
            return result
        self.f.clients["s3"].put_object = write

    def perform(self): return self.worker.perform(self.owner, self.epoch)

    def test_exact_approved_merge_ci_and_application_restore_keep_failed_state_and_baseline(self):
        result = self.perform()
        receipt = read_rollback_rollout(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(result["state"], "ROLLBACK_RESTORED")
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(len(self.patch_calls), 1)
        self.assertEqual(receipt["images"], self.baseline["images"])
        self.assertEqual(self.app["spec"]["source"]["targetRevision"], self.chosen)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["releaseCandidate"], self.failed_candidate)
        self.assertEqual(self.f.record["sourceRevisions"], self.sources)
        self.f.record = release(self.f.record, self.owner, self.epoch)
        finish_task(self.f.store, self.task, True, self.f.now)
        self.manager.wait(self.pointer)
        self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.assertEqual(completed_manager(self.f.clients, self.d.execution, self.f.record), operation("rollback", "rollout"))

    def test_lost_merge_and_patch_responses_are_reconciled_without_repeating_mutations(self):
        self.lost_merge = self.lost_patch = True
        self.assertEqual(self.perform()["state"], "ROLLBACK_RESTORED")
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(len(self.patch_calls), 1)

    def test_unknown_merge_call_marker_does_not_issue_a_merge(self):
        self.lost_calling = True
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertFalse(self.merge_calls); self.assertFalse(self.patch_calls)

    def test_unknown_application_call_marker_does_not_issue_a_patch(self):
        self.lost_app_calling = True
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertEqual(len(self.merge_calls), 1); self.assertFalse(self.patch_calls)

    def test_failed_main_ci_does_not_update_application(self):
        self.ci["conclusion"] = "failure"
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.merge_calls), 1); self.assertFalse(self.patch_calls)

    def test_main_movement_after_calling_marker_prevents_the_merge_api(self):
        self.move_main_after_calling = True
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.merge_calls)
        self.assertFalse(self.patch_calls)

    def test_application_uid_or_predecessor_stamp_change_is_rejected(self):
        self.app["metadata"]["annotations"][STAMP] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.patch_calls)

    def test_stamp_race_is_rejected_atomically_by_the_application_patch(self):
        self.stamp_race = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.patch_calls), 1)
        self.assertEqual(self.app["spec"]["source"]["targetRevision"], self.p.intent["failedChartSha"])
        self.assertNotIn("rolloutReference", self.f.record["rollbackAttempt"])

    def test_application_uid_change_after_readiness_read_cannot_publish_receipt(self):
        self.uid_after_readiness = True
        with self.assertRaises(ValueError): self.perform()
        self.assertNotIn("rolloutReference", self.f.record["rollbackAttempt"])

    def test_cleanup_after_merge_fences_application_and_receipt(self):
        self.cleanup_on_merge = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertFalse(self.patch_calls)
        self.assertNotIn("rolloutReference", self.f.record["rollbackAttempt"])

    def test_healthy_synced_app_with_wrong_runtime_digest_is_not_restored(self):
        self.wrong_runtime = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["rollbackAttempt"]["phase"], "APPROVED")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_changed_grant_or_duplicate_claim_cannot_use_this_task(self):
        with self.assertRaises(ValueError): claim_task(self.f.store, self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.f.record["rollbackAttempt"]["approvalReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): read_task(self.f.clients["s3"], self.d.bucket, self.f.record, self.task_id, self.f.now)
        self.assertFalse(self.merge_calls)

    def test_restored_task_cannot_replay_its_merge_or_application_patch(self):
        self.perform()
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(len(self.patch_calls), 1)

    def cli(self, arguments):
        import run_rollback_rollout
        from leased_command import execute
        self.f.clients["secretsmanager"] = SimpleNamespace()  # Pinned App clients are explicitly injected in this fixture.
        children, services = [], []
        case = self
        class Child:
            def __init__(self, command, **kwargs): self.command = command; children.append(command)
            def wait(self, timeout):
                with patch.object(sys, "argv", self.command[1:]): run_rollback_rollout.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kwargs): return execute(*args, **kwargs, popen=Child)
        def client(name, **kwargs): services.append(name); return self.f.clients[name]
        with patch.object(sys, "argv", arguments), patch("boto3.client", side_effect=client), \
                patch("run_rollback_rollout.TerraformRuntime", return_value=self.runtime), \
                patch("run_rollback_rollout.S3SessionStore", return_value=self.f.store), \
                patch("run_rollback_rollout.PrivateRollbackRollout", return_value=self.worker), \
                patch("run_rollback_rollout.execute", side_effect=parent), redirect_stdout(io.StringIO()) as output:
            run_rollback_rollout.main()
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0][-1], "--worker")
        self.assertTrue({"s3", "sts", "ec2", "scheduler", "secretsmanager", "codebuild", "ssm", "eks"} <= set(services))
        return output.getvalue()

    def test_real_cli_parent_worker_releases_owned_lease_and_reads_restored_receipt(self):
        self.f.record = release(self.f.record, self.owner, self.epoch)
        local = self.runtime.work / "rollback-rollout-cli.json"
        local.write_text(json.dumps(self.d.execution))
        output = self.cli(["run_rollback_rollout.py", "rollback", "--stage", "rollout", "--attempt", "1", "--execution", str(local),
            "--execution-version", self.d.version, "--bucket", self.d.bucket, "--kms-key", self.runtime.kms, "--task-id", self.task_id])
        self.assertIn('"state": "ROLLBACK_RESTORED"', output)
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        finish_task(self.f.store, self.task, True, self.f.now)

    def test_ami_loader_routes_exact_task_through_real_rollout_cli_and_rejects_redelivery(self):
        import tempfile
        bootstrap = Path(__file__).parents[1] / "bootstrap/automation"
        previous_path = list(sys.path)
        sys.path.insert(0, str(bootstrap))
        try:
            spec = importlib.util.spec_from_file_location("rollback_rollout_ami_loader", bootstrap / "deployment_task_loader.py")
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            self.f.record = deepcopy(self.dispatched)
            self.f.identity["Arn"] = f"arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-executor/{self.f.instance_id}"
            def load(client, bucket, session, version, directory):
                self.assertEqual((bucket, session, version), (self.d.bucket, self.d.execution["sessionId"], self.d.version))
                directory.mkdir(parents=True)
                (directory / "execution.json").write_text(json.dumps(self.d.execution))
                return Path(__file__).parents[1]
            def child(command, **kwargs):
                self.assertEqual(Path(command[1]).name, "run_rollback_rollout.py")
                self.assertEqual(command[2:5], ["rollback", "--stage", "rollout"])
                self.assertEqual(self.f.record["deploymentTask"]["state"], "RUNNING")
                text = self.cli(command[1:])
                kwargs["stdout"].write(text.encode())
                return SimpleNamespace(returncode=0)
            with tempfile.TemporaryDirectory() as directory, patch.object(module, "load", side_effect=load), \
                    patch("deployment_runtime.TerraformRuntime", return_value=self.runtime), \
                    patch("automation_session.S3SessionStore", return_value=self.f.store):
                module.run_task(self.f.clients, self.d.bucket, self.runtime.kms, self.task_id, Path(directory),
                    run=child, clock=lambda: self.f.now)
                self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")
                with self.assertRaises(ValueError):
                    module.run_task(self.f.clients, self.d.bucket, self.runtime.kms, self.task_id, Path(directory),
                        run=child, clock=lambda: self.f.now)
            self.assertEqual(len(self.merge_calls), 1)
            self.assertEqual(len(self.patch_calls), 1)
        finally: sys.path[:] = previous_path


if __name__ == "__main__": unittest.main()
