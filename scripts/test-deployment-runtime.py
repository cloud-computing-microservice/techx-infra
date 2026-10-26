"""Exercise runtime observations and fixed Terraform commands without AWS calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation_inputs import FIXED
from automation_session import claim, release
from cleanup_schedule import definition
from deployment_runtime import ACCOUNT, PROFILE, ROLE, TerraformRuntime, encoded
from lifecycle_coordinator import initial_record
from plan_approval import issue_approval
from plan_review_store import approval_key, immutable_json, read_json
from run_lifecycle_stage import LifecycleStageWorker
from deployment_tasks import claim_task, create_task, finish_task


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source = Path(self.directory.name) / "source"
        self.root = self.source / "techx-infra/environments/staging"
        self.root.mkdir(parents=True)
        self.module = self.source / "techx-infra/modules/vpc/main.tf"
        self.module.parent.mkdir(parents=True)
        self.module.write_text('# module\n')
        (self.root / "main.tf").write_text('# root\n')
        (self.root / ".terraform.lock.hcl").write_text('# locked provider\n')
        sources = {key: "a" * 40 for key in ("techx-infra", "techx-chart", "techx-platform")}
        (self.source / "source.json").write_bytes(encoded(sources))
        self.execution = initial_record("staging-runtime123", sources, 4, "owner@example.invalid", "deploy-window",
                                        datetime.now(timezone.utc), approved_budget_usd=60)
        self.state = dict(version=4, lineage="12345678-1234-1234-1234-123456789abc", serial=0, resources=[])
        self.execution["initialState"] = dict(lineage=self.state["lineage"], serial=0)
        self.bucket = "test-control-bucket"
        self.kms = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789abc"
        self.instance = dict(InstanceId="i-1234567890abcdef0", ImageId="ami-1234567890abcdef0", InstanceType="t3.small", State=dict(Name="running"),
            IamInstanceProfile=dict(Arn=PROFILE), PublicIpAddress="8.8.8.8",
            MetadataOptions=dict(HttpTokens="required", HttpPutResponseHopLimit=1),
            Tags=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="staging", Role="deployment-executor",
                                                      SessionId=self.execution["sessionId"]).items()])
        self.identity = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-executor/{self.instance['InstanceId']}")
        variables = dict(FIXED, budget_limit_usd=60, budget_alert_email="owner@example.invalid", public_access_cidrs=["1.1.1.1/32"],
                         enable_domain_vpn_foundation=False, enable_domain_vpn_edge=False,
                         public_certificate_arn=f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/12345678-1234-1234-1234-123456789abc")
        self.private = dict(schemaVersion=1, sessionId=self.execution["sessionId"], terraformVars=variables,
                            dnsMutationAttempted=False, validationDnsPlans=[],
                            deploymentExecutor=dict(instanceId=self.instance["InstanceId"], imageId=self.instance["ImageId"]))
        self.current_private = deepcopy(self.private)
        self.backend_config = dict(bucket=self.bucket, key="staging/terraform.tfstate", region="us-east-1", encrypt=True,
            use_lockfile=True, kms_key_id=self.kms, allowed_account_ids=[ACCOUNT])
        self.calls, self.writes = [], []
        self.version, self.workspace, self.egress = "1.15.5", "default", "8.8.8.8"
        self.plan_json = dict(variables=dict(budget_limit_usd=dict(value=60)), resource_changes=[
            dict(address="module.eks.aws_eks_cluster.this", change=dict(actions=["create"]))])
        def put_object(**kwargs):
            self.assertEqual(kwargs["IfMatch"], "etag-1")
            self.assertEqual(kwargs["ExpectedBucketOwner"], ACCOUNT)
            self.current_private = json.loads(kwargs["Body"])
            self.writes.append(kwargs)
        self.clients = dict(sts=SimpleNamespace(get_caller_identity=lambda: deepcopy(self.identity)),
            ec2=SimpleNamespace(describe_instances=lambda **kw: dict(Reservations=[dict(OwnerId=ACCOUNT, Instances=[deepcopy(self.instance)])])),
            s3=SimpleNamespace(get_object=lambda **kw: dict(Body=io.BytesIO(encoded(self.current_private)), ETag="etag-1"), put_object=put_object))
        self.runtime = self.create()

    def create(self):
        return TerraformRuntime(self.source, self.execution, self.private, self.bucket, self.kms, self.clients,
                                Path(self.directory.name) / "work", run=self.run_command,
                                open_url=lambda *a, **kw: io.BytesIO(self.egress.encode()))

    def run_command(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "pwsh":
            self.assertEqual(Path(command[3]).name, "cost-estimate.ps1")
            return SimpleNamespace(stdout=encoded(dict(approvedBudgetUsd=60, upperBoundUsd=17.7, maximumHours=4, includesAutomation=True)))
        if command[0] != "terraform":
            self.assertEqual(Path(command[1]).name, "review-plan.py")
            self.assertTrue(kwargs["capture_output"])
            return SimpleNamespace(stdout=b"")
        self.assertEqual(command[1], f"-chdir={self.root}")
        verb = command[2]
        if verb == "init":
            data = Path(kwargs["env"]["TF_DATA_DIR"])
            data.mkdir(parents=True, exist_ok=True)
            (data / "terraform.tfstate").write_bytes(encoded(dict(backend=dict(type="s3", config=self.backend_config))))
        if verb == "version": return SimpleNamespace(stdout=encoded(dict(terraform_version=self.version)))
        if verb == "workspace": return SimpleNamespace(stdout=self.workspace.encode())
        if verb == "state": return SimpleNamespace(stdout=encoded(self.state))
        if verb == "plan":
            Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("-out="))).write_bytes(b"private plan bytes")
        if verb == "show": return SimpleNamespace(stdout=encoded(self.plan_json))
        if verb == "apply": self.state["serial"] += 1
        return SimpleNamespace(stdout=b"")

    def initialized(self):
        self.runtime.initialize()
        self.runtime.configure("foundation", planning=True)

    def test_plan_reads_real_context_then_reviews_exact_binary_and_persists_cleanup_inputs(self):
        self.initialized()
        self.runtime.persist_variables()
        payload, context, changes = self.runtime.plan()
        self.assertEqual(payload, b"private plan bytes")
        self.assertEqual(context["stateSerial"], 0)
        self.assertEqual(context["executorRoleArn"], ROLE)
        self.assertEqual(context["callerPublicCidr"], "8.8.8.8/32")
        self.assertEqual(self.current_private["terraformVars"]["public_access_cidrs"], ["8.8.8.8/32"])
        self.assertEqual(self.current_private["deploymentExecutor"], self.private["deploymentExecutor"])
        self.assertEqual(changes, [dict(address="module.eks.aws_eks_cluster.this", actions=["create"])])
        plan = next(command for command, _ in self.calls if command[2:3] == ["plan"])
        self.assertIn("-refresh=false", plan)
        init = next(command for command, _ in self.calls if command[2:3] == ["init"])
        self.assertIn("-lockfile=readonly", init)
        self.assertIn(f"-backend-config=bucket={self.bucket}", init)
        review = next(kwargs for command, kwargs in self.calls if command[0] != "terraform")
        self.assertEqual(json.loads(review["input"]), self.plan_json)

    def test_apply_reuses_fixed_saved_plan_and_private_output_without_replanning(self):
        self.initialized()
        path = Path(self.directory.name) / "approved.tfplan"
        path.write_bytes(b"approved")
        self.calls.clear()
        self.runtime.apply(path, "foundation")
        self.assertEqual([command[2] if command[0] == "terraform" else "review" for command, _ in self.calls], ["show", "review", "apply"])
        self.assertTrue(all(kwargs["capture_output"] for _, kwargs in self.calls))
        self.assertEqual(self.state["serial"], 1)
        with self.assertRaises(ValueError): self.runtime.apply(path, "edge")

    def test_environment_is_isolated_and_unsafe_overrides_refused(self):
        self.assertEqual(self.runtime.env["AWS_CONFIG_FILE"], os.devnull)
        self.assertEqual(self.runtime.env["TF_WORKSPACE"], "default")
        for key, value in dict(TF_CLI_ARGS_plan="-destroy", TF_WORKSPACE="other", TF_CLI_CONFIG_FILE="override.rc",
                TF_REATTACH_PROVIDERS="untrusted-plugin", TF_LOG_PATH="leaked-state.log", AWS_CA_BUNDLE="untrusted.pem",
                AWS_PROFILE="default", AWS_ACCESS_KEY_ID="untrusted", AWS_ENDPOINT_URL_S3="https://elsewhere.invalid",
                AWS_CONTAINER_CREDENTIALS_FULL_URI="http://localhost", AWS_EC2_METADATA_DISABLED="true", https_proxy="http://proxy.invalid").items():
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), self.assertRaises(ValueError): self.create()
        with patch.dict(os.environ, {"TF_VAR_budget_limit_usd": "80"}):
            self.assertNotIn("TF_VAR_budget_limit_usd", self.create().env)

    def test_source_manifest_and_budget_cannot_be_substituted(self):
        self.private["terraformVars"]["budget_limit_usd"] = 80
        with self.assertRaises(ValueError): self.create()
        self.private["terraformVars"]["budget_limit_usd"] = 60
        (self.source / "source.json").write_bytes(encoded({"techx-infra": "b" * 40}))
        with self.assertRaises(ValueError): self.create()

    def test_instance_identity_ownership_ami_imds_and_network_must_match(self):
        original = deepcopy(self.instance)
        bad_values = dict(ImageId="ami-00000000", InstanceType="m5.24xlarge", State=dict(Name="stopped"), PublicIpAddress="10.52.1.2",
            IamInstanceProfile=dict(Arn="other"), MetadataOptions=dict(HttpTokens="optional", HttpPutResponseHopLimit=1), Tags=[])
        for key, value in bad_values.items():
            self.instance = dict(original, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError): self.runtime.executor()
        self.instance = original
        self.egress = "1.1.1.1"
        with self.assertRaises(ValueError): self.runtime.executor()
        self.egress = "8.8.8.8"
        self.identity["Arn"] = f"arn:aws:sts::{ACCOUNT}:assumed-role/other/{self.instance['InstanceId']}"
        with self.assertRaises(ValueError): self.runtime.executor()

    def test_toolchain_checked_before_init_and_each_observation(self):
        self.version = "1.16.0"
        with self.assertRaises(ValueError): self.runtime.initialize()
        self.assertFalse(any(command[2:3] == ["init"] for command, _ in self.calls))
        self.version = "1.15.5"
        self.initialized()
        self.version = "1.16.0"
        with self.assertRaises(ValueError): self.runtime.observe()

    def test_backend_config_workspace_and_state_lineage_are_observed(self):
        self.initialized()
        metadata = self.runtime.work / ".terraform/terraform.tfstate"
        for key, value in dict(bucket="another-bucket", key="other.tfstate", kms_key_id="other-key", allowed_account_ids=["123456789012"],
                              assume_role=dict(role_arn="other"), endpoints=dict(s3="https://elsewhere.invalid"), skip_requesting_account_id=True).items():
            metadata.write_bytes(encoded(dict(backend=dict(type="s3", config=dict(self.backend_config, **{key: value})))))
            with self.subTest(key=key), self.assertRaises(ValueError): self.runtime.observe()
        metadata.write_bytes(encoded(dict(backend=dict(type="s3", config=self.backend_config))))
        self.workspace = "other"
        with self.assertRaises(ValueError): self.runtime.observe()
        self.workspace = "default"
        self.state["lineage"] = "abcdefab-1234-1234-1234-123456789abc"
        with self.assertRaises(ValueError): self.runtime.observe()

    def test_hash_covers_json_modules_variables_and_provider_lock(self):
        self.initialized()
        baseline = self.runtime.observe()
        extra = self.module.with_name("extra.tf.json")
        extra.write_text('{"locals":{"example":1}}')
        self.assertNotEqual(self.runtime.observe()["configSha256"], baseline["configSha256"])
        extra.unlink()
        self.module.write_text('# changed module\n')
        self.assertNotEqual(self.runtime.observe()["configSha256"], baseline["configSha256"])
        self.module.write_text('# module\n')
        self.runtime.variables["budget_alert_email"] = "changed@example.invalid"
        self.assertNotEqual(self.runtime.observe()["configSha256"], baseline["configSha256"])
        (self.root / ".terraform.lock.hcl").write_text('# changed lock\n')
        self.assertNotEqual(self.runtime.observe()["providerLockSha256"], baseline["providerLockSha256"])

    def test_automatic_variable_files_are_rejected_before_init_or_hashing(self):
        for name in ("terraform.tfvars", "terraform.tfvars.json", "secret.auto.tfvars", "secret.auto.tfvars.json"):
            path = self.root / name
            path.write_text('{}')
            with self.subTest(name=name), self.assertRaises(ValueError): self.runtime.initialize()
            path.unlink()
        self.assertFalse(any(command[2:3] == ["init"] for command, _ in self.calls))

    def test_apply_configuration_requires_same_ip_flags_and_edge_dependencies(self):
        with self.assertRaises(ValueError): self.runtime.configure("foundation", planning=False)
        self.runtime.configure("foundation", planning=True)
        self.runtime.persist_variables()
        self.instance["PublicIpAddress"] = self.egress = "1.1.1.1"
        with self.assertRaises(ValueError): self.runtime.configure("foundation", planning=False)
        self.instance["PublicIpAddress"] = self.egress = "8.8.8.8"
        self.runtime.configure("foundation", planning=False)
        with self.assertRaises(ValueError): self.runtime.configure("edge", planning=True)
        self.runtime.private["terraformVars"].update(internal_alb_arn=f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/techx/1234",
            client_vpn_server_certificate_arn=self.private["terraformVars"]["public_certificate_arn"],
            client_vpn_root_certificate_arn=self.private["terraformVars"]["public_certificate_arn"])
        self.runtime.configure("edge", planning=True)
        self.assertTrue(self.runtime.variables["enable_domain_vpn_edge"])

    def test_private_snapshot_conflict_preserves_newer_cleanup_information(self):
        self.runtime.configure("foundation", planning=True)
        self.current_private["dnsMutationAttempted"] = True
        with self.assertRaises(ValueError): self.runtime.persist_variables()
        self.assertFalse(self.writes)

    def test_plan_rejects_budget_mismatch_review_failure_or_changed_state(self):
        self.initialized()
        self.plan_json["variables"]["budget_limit_usd"]["value"] = 80
        with self.assertRaises(ValueError): self.runtime.plan()
        self.plan_json["variables"]["budget_limit_usd"]["value"] = 60
        original = self.runtime.run
        def failed_review(command, **kwargs):
            if command[0] != "terraform": raise subprocess.CalledProcessError(1, command)
            return original(command, **kwargs)
        self.runtime.run = failed_review
        with self.assertRaises(subprocess.CalledProcessError): self.runtime.plan()
        def concurrent_state_change(command, **kwargs):
            if command[2:3] == ["plan"]: self.state["serial"] += 1
            return original(command, **kwargs)
        self.runtime.run = concurrent_state_change
        with self.assertRaises(ValueError): self.runtime.plan()

    def flow(self):
        """Use actual runtime, stage worker, approval store and controller together."""
        # Asset SDK/PKI behavior is exercised by test-bootstrap-assets.py.
        # Here keep the existing certificate fixture and prove worker ordering.
        bootstrap = patch("deployment_runtime.AssetBootstrap")
        self.asset_bootstrap = bootstrap.start()
        self.addCleanup(bootstrap.stop)
        credentials = patch("deployment_runtime.verify_private_credentials")
        self.credential_verifier = credentials.start()
        self.addCleanup(credentials.stop)
        self.now = datetime.now(timezone.utc)
        self.owner = f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{self.instance['InstanceId']}"
        self.record = claim(self.execution, self.owner, "deploy", now=self.now)
        self.record["executionVersion"] = "execution-version"
        self.schedule = definition(self.record, "execution-version",
            f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
            f"arn:aws:iam::{ACCOUNT}:role/techx-automation-scheduler",
            f"arn:aws:sqs:us-east-1:{ACCOUNT}:techx-automation-cleanup-dlq", self.now)
        self.schedule["Arn"] = f"arn:aws:scheduler:us-east-1:{ACCOUNT}:schedule/techx-staging/{self.schedule['Name']}"
        self.record["cleanupScheduleArn"] = self.schedule["Arn"]
        self.etag, self.objects = 1, {}
        class Missing(Exception): pass
        class Conflict(Exception): response = dict(Error=dict(Code="PreconditionFailed"))
        def get_object(**kwargs):
            self.assertEqual(kwargs["ExpectedBucketOwner"], ACCOUNT)
            if kwargs["Key"].endswith("inputs.json"):
                return dict(Body=io.BytesIO(encoded(self.current_private)), ETag="inputs-etag")
            if kwargs["Key"] not in self.objects: raise Missing()
            payload, version = self.objects[kwargs["Key"]]
            if "VersionId" in kwargs: self.assertEqual(kwargs["VersionId"], version)
            return dict(Body=io.BytesIO(payload), VersionId=version)
        def put_object(**kwargs):
            self.assertEqual(kwargs["ExpectedBucketOwner"], ACCOUNT)
            if kwargs["Key"].endswith("inputs.json"):
                self.assertEqual(kwargs["IfMatch"], "inputs-etag")
                self.current_private = json.loads(kwargs["Body"])
                return dict(VersionId="private-version")
            self.assertEqual(kwargs["IfNoneMatch"], "*")
            if kwargs["Key"] in self.objects: raise Conflict()
            self.objects[kwargs["Key"]] = kwargs["Body"], f"version-{len(self.objects) + 1}"
            return dict(VersionId=self.objects[kwargs["Key"]][1])
        self.clients["s3"] = SimpleNamespace(get_object=get_object, put_object=put_object, exceptions=SimpleNamespace(NoSuchKey=Missing))
        self.clients["scheduler"] = SimpleNamespace(get_schedule=lambda **kw: deepcopy(self.schedule))
        def write(value, etag):
            if etag != self.etag: raise ValueError("CAS conflict")
            self.record, self.etag = deepcopy(value), self.etag + 1
        self.store = SimpleNamespace(read=lambda: (deepcopy(self.record), self.etag), write=write)
        self.worker = LifecycleStageWorker(self.runtime, self.store, "execution-version", clock=lambda: self.now)

    def test_bad_private_credentials_stop_before_terraform_and_bootstrap_side_effects(self):
        self.flow()
        self.credential_verifier.side_effect = ValueError("private key unavailable")
        with self.assertRaisesRegex(ValueError, "private key unavailable"): self.perform()
        self.assertFalse(self.calls)
        self.asset_bootstrap.assert_not_called()
        self.assertNotIn("pendingPlan", self.record)

    def perform(self, mode="plan", attempt=1, stage="foundation"):
        return self.worker.perform(mode, stage, attempt, self.owner, self.record["lease"]["epoch"])

    def approve(self, ref):
        envelope, _ = read_json(self.clients["s3"], self.bucket, ref["key"], ref["versionId"])
        approval = issue_approval(envelope["request"], reviewer="operator",
            review_run="https://github.com/cloud-computing-microservice/techx-infra/actions/runs/123", now=self.now)
        approval["reviewReference"] = ref
        immutable_json(self.clients["s3"], self.bucket, approval_key(envelope["request"], ref["sha256"]), approval)

    def test_flow_plan_exits_for_review_then_fresh_worker_applies_only_approved_bytes(self):
        self.flow()
        ref = self.perform()
        self.assertEqual(self.record["status"], "PROVISIONING")
        self.assertFalse(any(command[2:3] == ["apply"] for command, _ in self.calls))
        epoch = self.record["lease"]["epoch"]
        self.record = release(self.record, self.owner, epoch)
        self.assertNotIn("lease", self.record)  # Approval wait holds no deploy lease.
        self.approve(ref)
        self.private = deepcopy(self.current_private)
        self.runtime = self.create()  # Model a new task on the same persistent VM.
        self.worker = LifecycleStageWorker(self.runtime, self.store, "execution-version", clock=lambda: self.now)
        self.record = claim(self.record, self.owner, "deploy", now=self.now)
        result = self.perform("apply")
        self.assertEqual(result["state"], "APPLIED")
        self.assertEqual(result["stateSerial"], 1)
        self.assertNotIn("pendingPlan", self.record)
        self.assertEqual(self.record["status"], "PROVISIONING")  # Bootstrap/acceptance still required before READY.
        applied = [command for command, _ in self.calls if command[2:3] == ["apply"]]
        self.assertEqual(len(applied), 1)
        with self.assertRaises(ValueError): self.perform("apply")

    def test_flow_no_approval_or_changed_network_never_calls_apply(self):
        self.flow()
        ref = self.perform()
        with self.assertRaises(self.clients["s3"].exceptions.NoSuchKey): self.perform("apply")
        self.approve(ref)
        self.instance["PublicIpAddress"] = self.egress = "1.1.1.1"
        with self.assertRaises(ValueError): self.perform("apply")
        self.assertEqual(self.record["stages"]["foundation"]["state"], "AWAITING_APPROVAL")
        self.assertFalse(any(command[2:3] == ["apply"] for command, _ in self.calls))

    def test_flow_disabled_cleanup_or_stale_execution_prevents_even_init(self):
        self.flow()
        self.schedule["State"] = "DISABLED"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)
        self.schedule["State"] = "ENABLED"
        self.record["executionVersion"] = "new-version"
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)

    def test_flow_bootstrap_failure_cannot_publish_a_foundation_plan(self):
        self.flow()
        self.asset_bootstrap.return_value.prepare.side_effect = TimeoutError("ACM receipt unresolved")
        with self.assertRaises(TimeoutError): self.perform()
        self.asset_bootstrap.return_value.prepare.assert_called_once()
        self.assertNotIn("pendingPlan", self.record)
        self.assertFalse(any(command[2:3] == ["plan"] for command, _ in self.calls))

    def test_flow_publish_cas_failure_recovers_original_review_without_replan_or_expiry_extension(self):
        self.flow()
        write = self.store.write
        def failed_write(value, etag): raise ValueError("lost CAS")
        self.store.write = failed_write
        with self.assertRaises(ValueError): self.perform()
        self.assertNotIn("pendingPlan", self.record)
        original = deepcopy(self.objects)
        self.now += timedelta(seconds=10)
        self.store.write = write
        self.perform()
        self.assertEqual(self.objects, original)
        self.assertEqual(len([command for command, _ in self.calls if command[2:3] == ["plan"]]), 1)
        # A lost response after a successful CAS is also an idempotent return.
        ref = self.perform()
        self.assertEqual(ref, self.record["pendingPlan"])
        self.assertEqual(len([command for command, _ in self.calls if command[2:3] == ["plan"]]), 1)

    def test_flow_lost_apply_response_becomes_uncertain_and_cannot_replay(self):
        self.flow()
        self.approve(self.perform())
        original = self.runtime.run
        def lost_response(command, **kwargs):
            result = original(command, **kwargs)
            if command[2:3] == ["apply"]: raise subprocess.CalledProcessError(1, command)
            return result
        self.runtime.run = lost_response
        with self.assertRaises(subprocess.CalledProcessError): self.perform("apply")
        self.assertEqual(self.state["serial"], 1)
        self.assertEqual(self.record["stages"]["foundation"]["state"], "UNCERTAIN")
        self.assertEqual(self.record["status"], "FAILED_NEEDS_CLEANUP")
        with self.assertRaises(ValueError): self.perform("apply")

    def test_flow_edge_requires_foundation_then_its_own_plan_and_approval(self):
        self.flow()
        with self.assertRaises(ValueError): self.perform(stage="edge")
        self.assertFalse(self.calls)
        self.approve(self.perform())
        self.perform("apply")
        with self.assertRaisesRegex(ValueError, "GitOps readiness"): self.perform(stage="edge")
        # Model the separate bootstrap producer persisting its observed ALB/PKI
        # outputs. This fixture is not evidence that cloud bootstrap is built.
        self.current_private["terraformVars"].update(
            internal_alb_arn=f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/techx/1234",
            client_vpn_server_certificate_arn=self.private["terraformVars"]["public_certificate_arn"],
            client_vpn_root_certificate_arn=self.private["terraformVars"]["public_certificate_arn"])
        self.record["bootstrapRelease"] = dict(key="release", versionId="release-v1", sha256="d" * 64)
        self.record["bootstrapChart"] = dict(key=f"chart-selections/staging/{self.record['sessionId']}/bootstrap.json", versionId="chart-v1", sha256="e" * 64)
        self.record["stages"]["gitops"] = dict(state="BOOTSTRAPPED", attempt=1, chartSha="f" * 40,
            selectionReference=deepcopy(self.record["bootstrapChart"]), releaseReference=deepcopy(self.record["bootstrapRelease"]),
            internalAlbArn=self.current_private["terraformVars"]["internal_alb_arn"])
        self.private = deepcopy(self.current_private)
        self.runtime = self.create()
        self.worker = LifecycleStageWorker(self.runtime, self.store, "execution-version", clock=lambda: self.now)
        self.plan_json["resource_changes"][0]["address"] = "module.domain_vpn_edge.aws_cloudfront_distribution.this"
        ref = self.perform(stage="edge")
        self.assertEqual(ref["stage"], "edge")
        with self.assertRaises(self.clients["s3"].exceptions.NoSuchKey): self.perform("apply", stage="edge")
        self.approve(ref)
        result = self.perform("apply", stage="edge")
        self.assertEqual(result["state"], "APPLIED")
        self.assertEqual(result["stateSerial"], 2)
        plans = [command for command, _ in self.calls if command[2:3] == ["plan"]]
        self.assertEqual(len(plans), 2)
        self.assertNotIn("-refresh=false", plans[-1])
        self.assertNotIn("pendingPlan", self.record)

    def test_flow_managed_task_must_be_claimed_and_cannot_bypass_its_identity(self):
        self.flow()
        self.record = release(self.record, self.owner, self.record["lease"]["epoch"])
        task_id = "f" * 32
        _, pointer = create_task(self.clients["s3"], self.bucket, self.record, "execution-version", self.instance["InstanceId"],
            "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:abcdef-1234",
            "plan", "foundation", 1, task_id, self.now)
        self.record["deploymentTask"] = pointer
        task = claim_task(self.store, self.clients["s3"], self.bucket, task_id, self.instance["InstanceId"], self.now)
        self.record = claim(self.record, self.owner, "deploy", now=self.now)
        with self.assertRaises(ValueError): self.perform()
        self.worker = LifecycleStageWorker(self.runtime, self.store, "execution-version", clock=lambda: self.now, task_id=task_id)
        ref = self.perform()
        self.assertEqual(ref, self.record["pendingPlan"])
        self.record = release(self.record, self.owner, self.record["lease"]["epoch"])
        finish_task(self.store, task, True, self.now)
        self.assertEqual(self.record["deploymentTask"]["state"], "SUCCEEDED")

    def test_flow_managed_apply_completes_only_with_observed_state_receipt(self):
        self.flow()
        self.approve(self.perform())
        self.record = release(self.record, self.owner, self.record["lease"]["epoch"])
        task_id = "e" * 32
        _, pointer = create_task(self.clients["s3"], self.bucket, self.record, "execution-version", self.instance["InstanceId"],
            "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:abcdef-1234",
            "apply", "foundation", 1, task_id, self.now)
        self.record["deploymentTask"] = pointer
        task = claim_task(self.store, self.clients["s3"], self.bucket, task_id, self.instance["InstanceId"], self.now)
        self.record = claim(self.record, self.owner, "deploy", now=self.now)
        self.worker = LifecycleStageWorker(self.runtime, self.store, "execution-version", clock=lambda: self.now, task_id=task_id)
        self.perform("apply")
        self.record = release(self.record, self.owner, self.record["lease"]["epoch"])
        finish_task(self.store, task, True, self.now)
        self.assertEqual(self.record["deploymentTask"]["state"], "SUCCEEDED")
        self.assertEqual(self.record["stages"]["foundation"]["stateSerial"], 1)


if __name__ == "__main__": unittest.main()
