from copy import deepcopy
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

BOOTSTRAP = Path(__file__).parents[1] / "bootstrap/automation"
sys.path.insert(0, str(BOOTSTRAP))
from deployment_task_loader import admitted_task, run_task

spec = importlib.util.spec_from_file_location("manager_test_support", Path(__file__).with_name("test-deployment-manager.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class TaskLoaderTests(unittest.TestCase):
    def setUp(self):
        # Reuse the SDK-validated dispatch fixture, not its test methods.
        self.fixture = support.ManagerTests()
        self.fixture.setUp()
        self.f = self.fixture
        self.pointer = self.f.dispatch()
        self.get = self.f.clients["s3"].get_object
        self.logs, self.events = [], []
        def get_object(**kw):
            if kw["Key"] == "automation/staging/session.json":
                return dict(Body=io.BytesIO(json.dumps(self.f.record).encode()))
            if kw["Key"].endswith("inputs.json"):
                return dict(Body=io.BytesIO(json.dumps(self.f.private).encode()))
            return self.get(**kw)
        self.f.clients["s3"].get_object = get_object
        self.f.identity["Arn"] = f"arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-executor/{self.f.instance_id}"
        self.args = self.f.clients["s3"], "control-bucket", self.pointer["taskId"], self.f.instance_id, self.f.now

    def test_admission_reads_only_current_exact_task_and_checks_expiry_owner_and_consumption(self):
        task = admitted_task(*self.args)
        self.assertEqual(task["taskId"], self.pointer["taskId"])
        for key, value in dict(state="RUNNING", instanceId="i-00000000", taskId="0" * 32).items():
            original = self.f.record["deploymentTask"][key]
            self.f.record["deploymentTask"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): admitted_task(*self.args)
            self.f.record["deploymentTask"][key] = original
        self.f.record["lease"] = dict(owner="manager-not-released")
        with self.assertRaises(ValueError): admitted_task(*self.args)

    def test_bad_version_or_checksum_never_reaches_bundle_execution(self):
        self.f.record["deploymentTask"]["reference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): admitted_task(*self.args)
        self.f.record["deploymentTask"]["reference"] = deepcopy(self.pointer["reference"])
        get = self.f.clients["s3"].get_object
        def wrong_version(**kw):
            response = get(**kw)
            if "VersionId" in kw: response["VersionId"] = "unexpected-version"
            return response
        self.f.clients["s3"].get_object = wrong_version
        with self.assertRaises(ValueError): admitted_task(*self.args)

    def test_bootstrap_loader_calls_only_the_fixed_gitops_worker_and_requires_its_receipt(self):
        from deployment_tasks import create_task
        self.f.record.pop("deploymentTask")
        self.f.record.update(status="PROVISIONING", stages=dict(foundation=dict(state="APPLIED")),
            bootstrapChart=dict(key=f"chart-selections/staging/{self.f.record['sessionId']}/bootstrap.json", versionId="selection-v1", sha256="e" * 64),
            bootstrapRelease=dict(key="release", versionId="release-v1", sha256="f" * 64))
        _, self.pointer = create_task(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1", self.f.instance_id,
            self.f.owner, "bootstrap", "gitops", 1, "9" * 32, self.f.now)
        self.f.record["deploymentTask"] = self.pointer
        def child(command, **kw):
            self.assertTrue(command[1].endswith("run_gitops_bootstrap.py"))
            self.assertEqual(command[2], "bootstrap")
            self.f.record["stages"]["gitops"] = dict(state="BOOTSTRAPPED", attempt=1, chartSha="b" * 40,
                selectionReference=deepcopy(self.f.record["bootstrapChart"]), releaseReference=deepcopy(self.f.record["bootstrapRelease"]),
                internalAlbArn="arn:aws:elasticloadbalancing:us-east-1:058114477594:loadbalancer/app/techx/123abc")
            return SimpleNamespace(returncode=0)
        self.execute(child)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")

    def test_promotion_loader_selects_fixed_worker_and_requires_exact_merge_receipt_checkpoint(self):
        from deployment_tasks import create_task
        self.f.record.pop("deploymentTask")
        self.f.record.update(status="PROVISIONING", stages=dict(foundation=dict(state="APPLIED")),
            bootstrapPromotionApproval=dict(key=f"promotion-approvals/staging/{self.f.record['sessionId']}/{'f' * 64}.json", versionId="approval-v1", sha256="e" * 64),
            bootstrapRelease=dict(key="release", versionId="release-v1", sha256="f" * 64),
            bootstrapProposal=dict(key="proposal", versionId="proposal-v1", sha256="a" * 64))
        _, self.pointer = create_task(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1", self.f.instance_id,
            self.f.owner, "promote", "chart", 1, "8" * 32, self.f.now)
        self.f.record["deploymentTask"] = self.pointer
        def child(command, **kw):
            self.assertTrue(command[1].endswith("run_chart_promotion.py"))
            self.assertEqual(command[2], "promote")
            approval = self.f.record["bootstrapPromotionApproval"]
            self.f.record["stages"]["chart"] = dict(state="PROMOTED", attempt=1, taskId=self.pointer["taskId"],
                approvalReference=deepcopy(approval), releaseReference=deepcopy(self.f.record["bootstrapRelease"]),
                proposalReference=deepcopy(self.f.record["bootstrapProposal"]), chartSha="b" * 40, headSha="c" * 40, treeSha="d" * 40,
                mergeReceiptReference=dict(key=f"promotions/staging/{self.f.record['sessionId']}/{approval['sha256']}/receipt.json", versionId="merge-v1", sha256="1" * 64))
            return SimpleNamespace(returncode=0)
        self.execute(child)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")

    def child_command(self, command, **kw):
        self.assertEqual(self.f.record["deploymentTask"]["state"], "RUNNING")
        self.assertEqual(command[command.index("--task-id") + 1], self.pointer["taskId"])
        self.assertEqual(command[command.index("--execution-version") + 1], "execution-v1")
        self.assertEqual(command[2], "plan")
        self.assertTrue(kw["check"])
        self.assertEqual(kw["stderr"], subprocess.STDOUT)
        self.events.append("child")
        self.f.remote_result()
        # The child writes stage result; only the loader marks the task terminal.
        self.f.record["deploymentTask"]["state"] = "RUNNING"
        return SimpleNamespace(returncode=0)

    def test_activation_uses_fixed_worker_and_requires_its_exact_receipt_checkpoint(self):
        from deployment_tasks import create_task, exposure_context
        from plan_review_store import checksum
        self.f.record.pop("deploymentTask")
        self.f.exposure()
        _, self.pointer = create_task(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1", self.f.instance_id,
            self.f.owner, "activate", "exposure", 1, "7" * 32, self.f.now)
        self.f.record["deploymentTask"] = self.pointer
        def child(command, **kw):
            self.assertTrue(command[1].endswith("run_edge_activation.py"))
            self.assertEqual(command[2], "activate")
            self.f.record["stages"]["exposure"] = dict(state="ACTIVATED", attempt=1, taskId=self.pointer["taskId"],
                contextSha256=checksum(exposure_context(self.f.record)), receiptReference=dict(
                    key=f"private/staging/{self.f.record['sessionId']}/exposure/{self.pointer['taskId']}.json", versionId="receipt-v1", sha256="e" * 64))
            return SimpleNamespace(returncode=0)
        self.execute(child)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")
        self.assertEqual(self.f.record["status"], "PROVISIONING")

    def execute(self, run=None):
        with tempfile.TemporaryDirectory() as base:
            def load(client, bucket, session, version, directory):
                self.events.append("verified-bundle")
                self.assertEqual(version, "execution-v1")
                directory.mkdir(parents=True)
                (directory / "execution.json").write_text(json.dumps(self.f.execution))
                return directory / "source/techx-infra"
            def put_object(**kw):
                self.assertEqual(kw["ExpectedBucketOwner"], "058114477594")
                self.assertEqual(kw["ServerSideEncryption"], "aws:kms")
                self.assertEqual(kw["SSEKMSKeyId"], self.kms)
                self.assertIn(f"/tasks/{self.pointer['taskId']}/task.private.log", kw["Key"])
                self.logs.append(kw["Key"])
            self.kms = "arn:aws:kms:us-east-1:058114477594:key/12345678-1234-1234-1234-123456789abc"
            self.f.clients["s3"].put_object = put_object
            with patch("deployment_task_loader.load", side_effect=load), \
                 patch("automation_session.S3SessionStore", return_value=self.f.store), \
                 patch("deployment_runtime.TerraformRuntime", return_value=SimpleNamespace(executor=lambda: self.events.append("runtime-identity"))):
                run_task(self.f.clients, "control-bucket", self.kms, self.pointer["taskId"], Path(base),
                         run=run or self.child_command, clock=lambda: self.f.now)

    def test_loader_claims_once_then_runs_typed_command_and_uploads_private_evidence(self):
        self.execute()
        self.assertEqual(self.events, ["verified-bundle", "runtime-identity", "child"])
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")
        self.assertEqual(len(self.logs), 1)
        with self.assertRaises(ValueError): admitted_task(*self.args)

    def test_child_failure_marks_cleanup_and_preserves_private_diagnostics(self):
        def failed(command, **kw):
            self.events.append("failed-child")
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError): self.execute(run=failed)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "FAILED")
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(len(self.logs), 1)

    def test_child_cannot_claim_success_without_durable_stage_result(self):
        with self.assertRaises(ValueError): self.execute(run=lambda *a, **kw: SimpleNamespace(returncode=0))
        self.assertEqual(self.f.record["deploymentTask"]["state"], "FAILED")


if __name__ == "__main__": unittest.main()
