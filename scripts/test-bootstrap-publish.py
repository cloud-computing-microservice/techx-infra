from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from automation_session import claim
from bootstrap_publish import ACCOUNT, CI, ColdPublisher
from cleanup_schedule import definition
from lifecycle_coordinator import initial_record
from lifecycle_dispatch import ROLE as MANAGER_ROLE, environment, operation
from bootstrap_provenance import PLATFORM_REPO, WORKFLOW
from run_bootstrap_publish import verified_checkout


class Conflict(Exception):
    response = dict(Error=dict(Code="PreconditionFailed"))


class ColdPublishTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.owner = "cloud-computing-microservice/techx-platform:100:1"
        self.execution = initial_record("staging-cold123", {name: "a" * 40 for name in ("techx-infra", "techx-platform", "techx-chart")},
            4, "owner@example.invalid", "deploy-window", self.now, approved_budget_usd=60)
        self.execution["costEstimate"] = dict(approvedBudgetUsd=60, upperBoundUsd=20, maximumHours=4, includesAutomation=True)
        self.execution["deploymentConfig"] = dict(schemaVersion=1, imageId="ami-1234567890abcdef0", launchTemplateId="lt-1234567890abcdef0",
            launchTemplateVersion="1", subnetId="subnet-1234567890abcdef0", securityGroupId="sg-1234567890abcdef0",
            loaderSha256="a" * 64, imageBuildSha256="c" * 64, documentVersion="1", documentSha256="b" * 64)
        manager = f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/techx-automation-lifecycle:abcdef-123"
        self.record = dict(deepcopy(self.execution), status="PROVISIONING", executionVersion="exec-v1",
            stages=dict(foundation=dict(state="APPLIED", attempt=1, stateSerial=1, stateLineage="12345678-1234-1234-1234-123456789abc", savedPlanSha256="b" * 64)),
            deploymentTask=dict(state="SUCCEEDED", mode="apply", stage="foundation", attempt=1, managerArn=manager,
                commandId="12345678-1234-1234-1234-123456789abc", instanceId="i-1234567890abcdef0"))
        self.record["lifecycleDispatch"] = dict(id="e" * 64, phase="OBSERVED", buildId=manager.split("build/")[1], operation=operation("apply", "foundation"))
        self.manager_build = dict(id=self.record["lifecycleDispatch"]["buildId"], arn=manager, projectName="techx-automation-lifecycle",
            serviceRole=MANAGER_ROLE, buildStatus="SUCCEEDED", buildComplete=True, environment=dict(environmentVariables=[
                dict(name=k, value=v, type="PLAINTEXT") for k, v in environment(self.record, operation("apply", "foundation")).items()]))
        self.workflow_run = dict(id=100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha="a" * 40, path=WORKFLOW, repository=dict(full_name=PLATFORM_REPO), head_repository=dict(full_name=PLATFORM_REPO))
        self.ci_runs = [dict(id=101, status="completed", conclusion="success", event="push", head_branch="main", head_sha="a" * 40,
            path=".github/workflows/verify.yml", repository=dict(full_name=PLATFORM_REPO), head_repository=dict(full_name=PLATFORM_REPO))]
        self.github = SimpleNamespace(get=lambda endpoint: deepcopy(self.workflow_run) if endpoint == "actions/runs/100" else
            dict(total_count=len(self.ci_runs), workflow_runs=deepcopy(self.ci_runs)))
        self.record = claim(self.record, self.owner, "bootstrap-publish", self.now)
        self.schedule = definition(self.record, "exec-v1", f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
            f"arn:aws:iam::{ACCOUNT}:role/techx-automation-scheduler", f"arn:aws:sqs:us-east-1:{ACCOUNT}:techx-automation-cleanup-dlq", self.now)
        self.schedule["Arn"] = f"arn:aws:scheduler:us-east-1:{ACCOUNT}:schedule/techx-staging/{self.schedule['Name']}"
        self.record["cleanupScheduleArn"] = self.schedule["Arn"]
        self.identity = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-bootstrap-publisher/techx-bootstrap-100")
        self.calls, self.loads, self.objects = [], [], {}
        self.etag, self.fail_ci, self.takeover, self.wrong_source, self.ssm_success = 1, False, False, False, True
        self.manifest = dict(commit="a" * 40, tag="staging-" + "a" * 12, images=[
            dict(component=name, image=f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/techx/{name}:staging-{'a' * 12}", digest="sha256:" + "c" * 64)
            for name in ("frontend", "catalog", "order")])
        def put(**kw):
            if kw["Key"] in self.objects: raise Conflict()
            self.objects[kw["Key"]] = kw["Body"]
            return dict(VersionId="manifest-v1")
        def get(**kw):
            return dict(VersionId="manifest-v1", Body=io.BytesIO(self.objects[kw["Key"]]))
        def write(value, etag):
            if etag != str(self.etag): raise Conflict()
            self.record = deepcopy(value)
            self.etag += 1
        model = botocore.session.get_session().get_service_model("ssm")
        def invocation(**kw):
            validate_parameters(kw, model.operation_model("GetCommandInvocation").input_shape)
            return dict(kw, DocumentName="techx-automation-deployment-task", DocumentVersion="1", PluginName="runTask",
                Status="Success" if self.ssm_success else "InProgress", ResponseCode=0 if self.ssm_success else -1)
        self.store = SimpleNamespace(read=lambda: (deepcopy(self.record), str(self.etag)), write=write)
        self.clients = dict(sts=SimpleNamespace(get_caller_identity=lambda: deepcopy(self.identity)),
            scheduler=SimpleNamespace(get_schedule=lambda **kw: deepcopy(self.schedule)), ssm=SimpleNamespace(get_command_invocation=invocation),
            s3=SimpleNamespace(put_object=put, get_object=get),
            codebuild=SimpleNamespace(batch_get_builds=lambda **kw: dict(builds=[deepcopy(self.manager_build)])))
        self.publisher = ColdPublisher(self.clients, self.store, "control-bucket", self.execution, "exec-v1", self.owner, 1,
            github=self.github, clock=lambda: self.now, run=self.process, load=self.load)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)

    def load(self, client, bucket, session, version, directory):
        self.loads.append(directory)
        directory.mkdir(parents=True)
        (directory / "execution.json").write_text(json.dumps(self.execution), encoding="utf-8")
        root = directory / "source"
        platform = root / "techx-platform"
        (platform / "scripts").mkdir(parents=True)
        source = deepcopy(self.execution["sourceRevisions"])
        if self.wrong_source: source["techx-platform"] = "d" * 40
        (root / "source.json").write_text(json.dumps(source), encoding="utf-8")
        (platform / "scripts/ecr-publisher-core.ps1").write_text("# Core process is stubbed in this test", encoding="utf-8")
        validator = Path(__file__).parents[2] / "techx-platform/scripts/prepare-chart-release.py"
        (platform / "scripts/prepare-chart-release.py").write_bytes(validator.read_bytes())
        return root / "techx-infra"

    def process(self, command, **kw):
        self.calls.append(command)
        if command == ["npm", "test"] and self.fail_ci:
            raise subprocess.CalledProcessError(1, command)
        if command == ["npm", "run", "build"]:
            Path(kw["cwd"], "ci-mutated.txt").write_text("Generated CI output must not enter image context")
        if "-ManifestPath" in command:
            self.assertFalse((Path(kw["cwd"]) / "ci-mutated.txt").exists())
            self.assertFalse((Path(kw["cwd"]) / ".git").exists())
            self.assertEqual(command[command.index("-SourceSha") + 1], "a" * 40)
            Path(command[command.index("-ManifestPath") + 1]).write_text(json.dumps(self.manifest), encoding="utf-8")
            if self.takeover: self.record["status"] = "DESTROYING"
        return SimpleNamespace(returncode=0)

    def perform(self, name="run"): return self.publisher.perform(self.base / name)

    def test_cold_ci_and_publish_use_separate_fresh_archives_then_record_one_immutable_release(self):
        ref = self.perform()
        self.assertEqual(self.calls[:-1], [list(command) for command in CI])
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(self.record["bootstrapRelease"], ref)
        self.assertEqual(ref["versionId"], "manifest-v1")
        self.assertEqual(self.record["status"], "PROVISIONING")
        self.perform("retry")
        self.assertEqual(self.record["bootstrapRelease"], ref)
        self.assertEqual(len(self.objects), 1)
        artifact = json.loads(next(iter(self.objects.values())))
        self.assertEqual(artifact["sourceCi"]["ciRunId"], 101)
        self.assertEqual(artifact["sourceCi"]["workflowRunId"], 100)

    def test_ci_failure_never_invokes_publish_or_writes_release_receipt(self):
        self.fail_ci = True
        with self.assertRaises(subprocess.CalledProcessError): self.perform()
        self.assertEqual(len(self.loads), 1)
        self.assertFalse(self.objects)
        self.assertNotIn("bootstrapRelease", self.record)

    def test_lost_session_cas_preserves_immutable_manifest_for_recovery(self):
        write = self.store.write
        def lost(value, etag): raise Conflict()
        self.store.write = lost
        with self.assertRaises(Conflict): self.perform()
        self.assertEqual(len(self.objects), 1)
        self.assertNotIn("bootstrapRelease", self.record)
        self.store.write = write
        ref = self.perform("recover")
        self.assertEqual(self.record["bootstrapRelease"], ref)
        self.assertEqual(len(self.objects), 1)

    def test_wrong_role_or_forged_runner_identity_fails_before_source_execution(self):
        for value in (self.identity["Arn"].replace("bootstrap-publisher", "publisher"), self.identity["Arn"].replace("-100", "-200")):
            self.identity["Arn"] = value
            with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.loads)

    def test_foundation_pending_ssm_cleanup_or_ready_release_state_blocks_cold_build(self):
        original = deepcopy(self.record)
        for change in (lambda: self.record.update(status="READY"), lambda: self.record["stages"]["foundation"].update(state="AWAITING_APPROVAL"),
                lambda: self.record.update(pendingPlan=dict()), lambda: self.record["stages"].update(edge=dict(state="APPLIED")),
                lambda: self.record["lease"].update(epoch=2)):
            self.record = deepcopy(original)
            change()
            with self.assertRaises(ValueError): self.perform()
        self.record = original
        self.ssm_success = False
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.loads)

    def test_wrong_source_or_manifest_never_becomes_a_release_reference(self):
        self.wrong_source = True
        with self.assertRaises(ValueError): self.perform()
        self.wrong_source = False
        self.manifest["images"][0]["image"] = "wrong-account/image:tag"
        with self.assertRaises(ValueError): self.perform("bad-manifest")
        self.assertFalse(self.objects)

    def test_archive_source_cannot_include_tracked_build_output(self):
        original = self.publisher.load
        def generated(*args):
            infra = original(*args)
            path = infra.parent / "techx-platform/.next"
            path.mkdir()
            (path / "old.js").write_text("Stale build output")
            return infra
        self.publisher.load = generated
        with self.assertRaisesRegex(ValueError, "Generated or linked"):
            self.perform()
        self.assertFalse(self.calls)

    def test_cleanup_during_docker_publish_prevents_receipt_and_session_publication(self):
        self.takeover = True
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.objects)
        self.assertNotIn("bootstrapRelease", self.record)

    def test_disabled_schedule_and_insufficient_window_fail_before_checkout(self):
        self.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): self.perform()
        self.schedule["State"] = "ENABLED"
        self.now = datetime.fromisoformat(self.record["cleanupStartAt"]) - timedelta(minutes=20)
        self.record["lease"]["expiresAt"] = (self.now + timedelta(minutes=5)).isoformat()
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.loads)

    def test_read_only_admission_before_lease_rejects_busy_or_unfinished_manager(self):
        idle = deepcopy(self.record)
        idle.pop("lease")
        self.publisher.admission(idle)
        with self.assertRaises(ValueError): self.publisher.admission(self.record)
        self.manager_build.update(buildStatus="IN_PROGRESS", buildComplete=False)
        with self.assertRaises(ValueError): self.publisher.admission(idle)
        self.assertFalse(self.calls)
        self.assertFalse(self.objects)

    def test_untrusted_run_rerun_or_different_source_is_rejected_before_ci(self):
        for key, value in (("event", "pull_request"), ("run_attempt", 2), ("head_branch", "feature"), ("head_sha", "f" * 40),
                ("path", ".github/workflows/other.yml"), ("status", "completed"), ("head_repository", dict(full_name="fork/repo"))):
            old = deepcopy(self.workflow_run[key])
            self.workflow_run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.perform()
            self.workflow_run[key] = old
        self.publisher.owner = self.owner[:-1] + "2"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)
        self.assertFalse(self.loads)

    def test_latest_failed_pending_or_wrong_source_ci_cannot_fall_back_to_green(self):
        original = deepcopy(self.ci_runs[0])
        for change in (dict(id=102, conclusion="failure"), dict(id=102, status="in_progress", conclusion=None)):
            self.ci_runs = [deepcopy(original), dict(original, **change)]
            with self.assertRaises(ValueError): self.perform()
        self.ci_runs = [dict(original, head_sha="f" * 40)]
        with self.assertRaises(ValueError): self.perform()
        self.ci_runs = []
        with self.assertRaises(ValueError): self.perform()
        self.ci_runs = [deepcopy(original)]
        get = self.github.get
        self.github.get = lambda endpoint: dict(total_count=2, workflow_runs=deepcopy(self.ci_runs)) if "workflows/verify.yml" in endpoint else get(endpoint)
        with self.assertRaisesRegex(ValueError, "Complete platform CI history"): self.perform()
        self.assertFalse(self.calls)

    def test_ci_deterioration_during_build_blocks_release_and_cannot_publish_a_proof(self):
        original = self.publisher.run
        def changed(command, **kw):
            result = original(command, **kw)
            if "-ManifestPath" in command: self.ci_runs[0]["conclusion"] = "failure"
            return result
        self.publisher.run = changed
        with self.assertRaisesRegex(ValueError, "Latest platform main CI"): self.perform()
        self.assertFalse(self.objects)
        self.assertNotIn("bootstrapRelease", self.record)

    def test_fresh_valid_ci_retry_preserves_original_immutable_receipt_provenance(self):
        ref = self.perform()
        self.ci_runs[0]["id"] = 102
        recovered = self.perform("retry-new-ci")
        self.assertEqual(ref, recovered)
        self.assertEqual(json.loads(next(iter(self.objects.values())))["sourceCi"]["ciRunId"], 101)

    def test_automation_checkout_must_be_own_root_exact_sha_and_unmodified(self):
        root = self.base / "automation"
        calls = []
        def git(command, **kw):
            calls.append(command)
            return SimpleNamespace(stdout=str(root) if "--show-toplevel" in command else "a" * 40)
        verified_checkout(root, "a" * 40, run=git)
        self.assertEqual(calls[-1][-4:], ["diff", "--exit-code", "HEAD", "--"])
        with self.assertRaises(ValueError): verified_checkout(root, "b" * 40, run=git)
        def dirty(command, **kw):
            if "diff" in command: raise subprocess.CalledProcessError(1, command)
            return git(command, **kw)
        with self.assertRaises(subprocess.CalledProcessError): verified_checkout(root, "a" * 40, run=dirty)
        with self.assertRaises(ValueError): verified_checkout(root / "nested", "a" * 40, run=git)


if __name__ == "__main__": unittest.main()
