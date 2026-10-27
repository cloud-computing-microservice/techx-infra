"""Actual dispatch/manager/task/merge/Kubernetes rollout with bounded SDK fixtures."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
import sys
from unittest.mock import patch

from automation_session import claim, release
from deployment_manager import DeploymentManager
from deployment_tasks import claim_task, finish_task, read_task
from gitops_bootstrap import COMPONENTS, SESSION_ANNOTATION
from lifecycle_dispatch import environment, operation
from private_active_rollout import PrivateActiveRollout, STAMP
from private_chart_promotion import MergeUncertain
from plan_review_store import checksum, encoded


spec = importlib.util.spec_from_file_location("active_rollout_handoff_fixture", Path(__file__).with_name("test-ready-release-handoff.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.h = support.ActiveHandoffTests()
        self.h.setUp(); self.addCleanup(self.h.doCleanups)
        h = self.h
        h.complete_review(); h.observe(3)
        self.f, self.d = h.f, h.d
        f = self.f
        self.original_baseline = deepcopy(f.record["acceptedRelease"])
        self.original_sources = deepcopy(f.record["sourceRevisions"])
        self.id, self.build, self.build_calls = h.active_id, h.active_build, h.build_calls
        self.manager_arn = self.build["arn"]
        self.manager = DeploymentManager(f.clients, f.store, self.d.execution, self.d.version, self.d.bucket, self.manager_arn, clock=lambda: f.now, sleep=lambda _: None)
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/codebuild-active"
        self.private = h.p.p.private.runtime.private
        self.pointer = self.manager.dispatch(self.private, "release", "active", 1)
        self.dispatched_record = deepcopy(f.record)
        self.task_id = self.pointer["taskId"]
        self.task = claim_task(f.store, f.clients["s3"], "control-bucket", self.task_id, f.instance_id, f.now)
        self.owner = f"arn:aws:ec2:us-east-1:058114477594:instance/{f.instance_id}"
        f.record = claim(f.record, self.owner, "release", now=f.now)
        self.epoch = f.record["lease"]["epoch"]
        self.runtime = h.p.p.private.runtime
        self.runtime.execution = self.d.execution
        self.runtime.clients = f.clients
        self.g = h.p.p.private.g
        self.app = self.g.objects[("application", "techx-staging", "argocd")]
        self.app["metadata"].update(uid="application-uid", resourceVersion="41")
        self.app["metadata"].setdefault("annotations", {})[SESSION_ANNOTATION] = f.record["sessionId"]
        self.merge_calls, self.patch_calls = [], []
        self.lost_merge = self.lost_patch = self.bad_uid = self.cleanup_in_merge = False
        self.lost_merge_marker = self.lost_app_marker = self.wrong_runtime = False
        self.selected = "9" * 40
        self.ci = dict(id=966, head_sha=self.selected, head_branch="main", event="push", run_attempt=1,
            path=".github/workflows/verify.yml", repository=dict(full_name="cloud-computing-microservice/techx-chart"), head_repository=dict(full_name="cloud-computing-microservice/techx-chart"),
            status="completed", conclusion="success", created_at=f.now.isoformat())
        chart = h.p.chart
        get = chart.github.get
        def chart_get(path):
            if path == f"actions/workflows/verify.yml/runs?head_sha={self.selected}&per_page=100": return dict(total_count=1, workflow_runs=[deepcopy(self.ci)])
            if path == "actions/runs/966": return deepcopy(self.ci)
            return get(path)
        def merge(number, head, title, message):
            self.assertEqual((number, head), (7, chart.branch))
            self.merge_calls.append((number, head))
            chart.pr.update(merged=True, state="closed", merge_commit_sha=self.selected, merged_by=dict(type="Bot", login="techx-promotion[bot]"), merged_at=f.now.isoformat())
            chart.commits[self.selected] = dict(sha=self.selected, tree=dict(sha=chart.commits[chart.branch]["tree"]["sha"]),
                parents=[dict(sha=chart.base), dict(sha=chart.branch)], message=title + "\n\n" + message)
            chart.main = self.selected
            if self.cleanup_in_merge: f.record["status"] = "DESTROYING"
            if self.lost_merge: raise TimeoutError("Lost positive merge response")
            return dict(merged=True, sha=self.selected)
        self.chart = SimpleNamespace(get=chart_get, merge=merge)
        old_run = self.runtime.run
        def run(command, **kw):
            if "patch" not in command: return old_run(command, **kw)
            self.assertIn("--type=json", command)
            self.assertIn("--patch-file=/dev/stdin", command)
            ops = json.loads(kw["input"])
            self.patch_calls.append(ops)
            if self.bad_uid: self.app["metadata"]["uid"] = "replaced-application"
            for op in ops:
                if op["op"] != "test": continue
                actual = self.app
                for part in op["path"].lstrip("/").split("/"): actual = actual[part.replace("~1", "/").replace("~0", "~")]
                if actual != op["value"]: raise ValueError("Kubernetes JSON Patch test failed atomically")
            self.app["metadata"]["annotations"][STAMP] = next(op["value"] for op in ops if op["op"] == "add")
            self.app["spec"]["source"]["targetRevision"] = self.selected
            self.app["status"] = dict(sync=dict(status="Synced", revision=self.selected), health=dict(status="Healthy"))
            images = {COMPONENTS[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in h.p.p.manifest["images"]}
            for name, image in images.items():
                self.g.objects[("deployment", name, "techx-staging")]["spec"]["template"]["spec"]["containers"][0]["image"] = image
            for pod in self.g.pods:
                name = pod["metadata"]["labels"]["app.kubernetes.io/component"]
                pod["spec"]["containers"][0]["image"] = images[name]
                pod["status"]["containerStatuses"][0]["imageID"] = images[name]
            if self.wrong_runtime: self.g.pods[0]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "0" * 64
            if self.lost_patch: raise TimeoutError("Patch succeeded; response lost")
            return SimpleNamespace(stdout=json.dumps(self.app).encode())
        self.runtime.run = run
        # Model immutable merge/rollout artifacts separately from the initial
        # foundation fixture's resource producer callbacks.
        s3 = f.clients["s3"]
        old_get, old_put, old_list = s3.get_object, s3.put_object, s3.list_objects_v2
        self.artifacts = {}
        def private_key(key): return key.startswith("promotions/") or "/active/" in key and key.startswith("private/")
        def get_object(**kw):
            if not private_key(kw["Key"]): return old_get(**kw)
            payload, object_version = self.artifacts[kw["Key"]]
            if kw.get("VersionId"): self.assertEqual(kw["VersionId"], object_version)
            return dict(Body=io.BytesIO(payload), VersionId=object_version)
        def put_object(**kw):
            if not private_key(kw["Key"]): return old_put(**kw)
            from botocore.exceptions import ClientError
            if kw["Key"] in self.artifacts: raise ClientError(dict(Error=dict(Code="PreconditionFailed")), "PutObject")
            self.artifacts[kw["Key"]] = kw["Body"], "active-v1"
            if (self.lost_merge_marker and kw["Key"].startswith("promotions/") or self.lost_app_marker and "/application/" in kw["Key"]) and kw["Key"].endswith("/calling.json"):
                raise TimeoutError("Stored call marker response lost")
            return dict(VersionId="active-v1")
        s3.get_object, s3.put_object = get_object, put_object
        s3.list_objects_v2 = lambda **kw: dict(IsTruncated=False, Contents=[dict(Key=key) for key in self.artifacts if key.startswith(kw["Prefix"])]) if private_key(kw["Prefix"]) else old_list(**kw)
        self.worker = PrivateActiveRollout(self.runtime, f.store, self.d.version, self.task_id, clock=lambda: f.now,
            sleep=lambda _: None, clients=lambda *args: (self.chart, h.infra, h.p.p.github))

    def perform(self): return self.worker.perform(self.owner, self.epoch)

    def test_real_dispatch_task_merge_ci_application_and_terminal_manager(self):
        result = self.perform()
        self.assertEqual(result["state"], "ACTIVE_ROLLED_OUT")
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(len(self.patch_calls), 1)
        self.assertEqual(self.app["spec"]["source"]["targetRevision"], self.selected)
        self.assertEqual(self.f.record["status"], "TESTING")
        self.assertEqual(self.f.record["acceptedRelease"], self.original_baseline)
        self.assertEqual(self.f.record["sourceRevisions"], self.original_sources)
        self.f.record = release(self.f.record, self.owner, self.epoch)
        finish_task(self.f.store, self.task, True, self.f.now)
        self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.assertEqual(self.d.advance()["waiting"], "ACTIVE_PUBLIC_ACCEPTANCE")

    def test_lost_merge_and_patch_responses_use_positive_observation_not_replay(self):
        self.lost_merge = self.lost_patch = True
        self.assertEqual(self.perform()["state"], "ACTIVE_ROLLED_OUT")
        self.assertEqual((len(self.merge_calls), len(self.patch_calls)), (1, 1))

    def test_failed_main_ci_keeps_application_on_accepted_baseline(self):
        self.ci["conclusion"] = "failure"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.patch_calls)
        self.assertEqual(self.app["spec"]["source"]["targetRevision"], self.original_baseline["chartSha"])

    def test_pending_main_ci_is_bounded_without_restarting_merge_or_manager(self):
        self.ci.update(status="in_progress", conclusion=None)
        def heartbeat(seconds):
            self.f.now += timedelta(seconds=seconds)
            self.f.record = claim(self.f.record, self.owner, "release", now=self.f.now)
        self.worker.sleep = heartbeat
        with self.assertRaises(TimeoutError): self.perform()
        self.assertEqual((len(self.build_calls), len(self.merge_calls)), (1, 1))
        self.assertFalse(self.patch_calls)

    def test_replaced_application_cannot_receive_the_owned_rollout(self):
        self.bad_uid = True
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertNotIn("rollout", self.f.record["releaseCandidate"])

    def test_cleanup_takeover_during_merge_prevents_application_patch(self):
        self.cleanup_in_merge = True
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.patch_calls)
        self.assertEqual(self.f.record["acceptedRelease"], self.original_baseline)

    def test_immutable_task_rejects_changed_candidate_or_duplicate_claim(self):
        with self.assertRaises(ValueError): claim_task(self.f.store, self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.f.record["releaseCandidate"]["manifestReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): read_task(self.f.clients["s3"], self.d.bucket, self.f.record, self.task_id, self.f.now)
        self.assertFalse(self.merge_calls)

    def test_unknown_merge_marker_does_not_issue_a_merge(self):
        self.lost_merge_marker = True
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertFalse(self.merge_calls)
        self.assertFalse(self.patch_calls)

    def test_unknown_application_marker_does_not_issue_a_patch(self):
        self.lost_app_marker = True
        with self.assertRaises(MergeUncertain): self.perform()
        self.assertEqual(len(self.merge_calls), 1)
        self.assertFalse(self.patch_calls)

    def test_synced_healthy_application_with_wrong_runtime_digest_cannot_complete(self):
        self.wrong_runtime = True
        with self.assertRaises(ValueError): self.perform()
        self.assertNotIn("rollout", self.f.record["releaseCandidate"])
        self.assertEqual(self.f.record["acceptedRelease"], self.original_baseline)

    def test_publisher_and_cold_operations_cannot_use_testing_release_authority(self):
        unowned = deepcopy(self.f.record); unowned.pop("lease")
        with self.assertRaises(ValueError): claim(unowned, "cloud-computing-microservice/techx-platform:701:1", "release", now=self.f.now)
        with self.assertRaises(ValueError): self.manager.dispatch(self.private, "plan", "foundation", 1)
        with self.assertRaises(ValueError): self.d.start(deepcopy(self.f.record), operation("release", "active"))
        self.assertEqual(len(self.build_calls), 1)

    def test_parent_checks_receipt_runtime_and_application_intent_not_only_its_hash(self):
        from active_release_contract import read_rollout_receipt
        self.perform()
        ref = self.f.record["releaseCandidate"]["rollout"]["receiptReference"]
        body, object_version = self.artifacts[ref["key"]]
        original = json.loads(body)
        for field in ("images", "application"):
            changed = deepcopy(original)
            if field == "images": changed["images"]["order-api"] = "wrong-image"
            else: changed["application"]["stamp"] = "0" * 64
            self.artifacts[ref["key"]] = encoded(changed), object_version
            ref["sha256"] = checksum(changed)
            with self.subTest(field=field), self.assertRaises(ValueError): read_rollout_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_ami_loader_admits_only_the_exact_unconsumed_testing_task(self):
        spec = importlib.util.spec_from_file_location("active_task_loader", Path(__file__).parents[1] / "bootstrap/automation/deployment_task_loader.py")
        sys.path.insert(0, str(Path(__file__).parents[1] / "bootstrap/automation"))
        try:
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        finally: sys.path.pop(0)
        self.f.record = deepcopy(self.dispatched_record)
        actual = module.admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)
        self.assertEqual(actual["expectedReleaseContext"], self.task["expectedReleaseContext"])
        self.f.record["status"] = "PROVISIONING"
        with self.assertRaises(ValueError): module.admitted_task(self.f.clients["s3"], self.d.bucket, self.task_id, self.f.instance_id, self.f.now)

    def test_real_cli_parent_and_worker_keep_testing_and_release_the_owned_lease(self):
        import run_active_rollout
        from leased_command import execute
        self.f.record = release(self.f.record, self.owner, self.epoch)
        local = self.runtime.work / "cli-execution.json"
        local.write_bytes(encoded(self.d.execution))
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.d.execution), self.d.version
        self.f.clients["secretsmanager"] = SimpleNamespace()  # App clients are the explicit injected dependency in this fixture.
        class Child:
            def __init__(self, command, **kw): self.command = command
            def wait(self, timeout):
                with patch.object(sys, "argv", self.command[1:]): run_active_rollout.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kw): return execute(*args, **kw, popen=Child)
        arguments = ["run_active_rollout.py", "release", "--stage", "active", "--attempt", "1", "--execution", str(local),
            "--execution-version", self.d.version, "--bucket", self.d.bucket, "--kms-key", self.runtime.kms, "--task-id", self.task_id]
        with patch.object(sys, "argv", arguments), patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch("run_active_rollout.TerraformRuntime", return_value=self.runtime), patch("run_active_rollout.S3SessionStore", return_value=self.f.store), \
                patch("run_active_rollout.PrivateActiveRollout", return_value=self.worker), patch("run_active_rollout.execute", side_effect=parent), redirect_stdout(io.StringIO()) as output:
            run_active_rollout.main()
        self.assertIn('"state": "ACTIVE_ROLLED_OUT"', output.getvalue())
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(self.f.record["status"], "TESTING")
        finish_task(self.f.store, self.task, True, self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")


if __name__ == "__main__": unittest.main()
