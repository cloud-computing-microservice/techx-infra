from contextlib import contextmanager
from copy import deepcopy
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import botocore.session
from botocore.validate import validate_parameters

from automation_session import claim, release
from deployment_tasks import claim_task, create_task, finish_task, read_task
from lifecycle_dispatch import Dispatcher, environment, next_operation, operation
from leased_command import execute
from private_acceptance import PrivateAcceptance
from private_acceptance_contract import ready_release, read_private_receipt
from private_http import PrivateFailure


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


public_support = load("private_public_fixture", "test-public-acceptance.py")
gitops_support = load("private_gitops_fixture", "test-gitops-bootstrap.py")


class PrivateAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.public = public_support.PublicAcceptanceTests()
        self.public.setUp()
        self.addCleanup(self.public.doCleanups)
        self.public.perform()
        f = self.f = self.public.f
        f.record = release(f.record, self.public.worker.owner, f.record["lease"]["epoch"])
        self.g = gitops_support.GitOpsTests()
        self.g.setUp()
        self.addCleanup(self.g.doCleanups)
        g = self.g
        self.runtime = self.public.edge.runtime
        self.runtime.source, self.runtime.work = g.runtime.source, g.runtime.work
        self.runtime.env = deepcopy(g.runtime.env)
        self.runtime.executor = lambda: "8.8.8.8/32"
        f.clients.update({key: g.f.clients[key] for key in ("eks", "elbv2", "resourcegroupstaggingapi")})
        self.chart_sha = f.record["stages"]["gitops"]["chartSha"]
        app = deepcopy(g.base_app)
        app["spec"]["source"]["targetRevision"] = self.chart_sha
        app["status"] = dict(sync=dict(status="Synced", revision=self.chart_sha), health=dict(status="Healthy"))
        g.objects[("application", "techx-staging", "argocd")] = app
        for pod in g.pods: pod["metadata"]["uid"] = pod["metadata"]["name"] + "-old"
        order = g.objects[("deployment", "order-api", "techx-staging")]["spec"]["template"]["spec"]
        order["serviceAccountName"] = "order-api"
        order["containers"][0]["env"] = [dict(name="AWS_REGION", value="us-east-1"), dict(name="ORDER_TABLE_NAME", value="techx-staging-orders")]
        g.objects[("serviceaccount", "order-api", "techx-staging")] = dict(metadata=dict(annotations={
            "eks.amazonaws.com/role-arn": "arn:aws:iam::058114477594:role/techx-staging-order-api"}))
        g.objects[("secret", "argocd-initial-admin-secret", "argocd")] = dict(data=dict(password=base64.b64encode(b"fixture-private-password").decode()))
        self.heal = self.restart = self.persist = self.disconnect = True
        self.auth, self.scan, self.table_mode = True, "COMPLETE", "PAY_PER_REQUEST"
        self.calls = []
        self.drift = False
        def run(command, **kw):
            args = command[command.index("--request-timeout=20s") + 1:]
            self.calls.append(args)
            if args[:2] == ["scale", "deployment/frontend"]:
                replicas = int(next(arg.split("=")[1] for arg in args if arg.startswith("--replicas=")))
                deployment = g.objects[("deployment", "frontend", "techx-staging")]
                deployment["spec"]["replicas"] = replicas
                self.drift = replicas == 2
                return SimpleNamespace(stdout=json.dumps(deployment).encode())
            if args[:3] == ["get", "deployment", "frontend"] and self.drift and self.heal:
                g.objects[("deployment", "frontend", "techx-staging")]["spec"]["replicas"] = 1
                self.drift = False
            if args[:3] == ["rollout", "restart", "deployment/order-api"]:
                if self.restart:
                    deployment = g.objects[("deployment", "order-api", "techx-staging")]
                    deployment["metadata"]["generation"] += 1
                    deployment["status"]["observedGeneration"] = deployment["metadata"]["generation"]
                    g.pods[2]["metadata"]["uid"] = "order-new"
                    if not self.persist: self.public.service.orders.clear()
                return SimpleNamespace(stdout=b"deployment restarted\n")
            if args[:2] == ["get", "pods"] and "app.kubernetes.io/component=order-api" in args:
                return SimpleNamespace(stdout=json.dumps(dict(items=[deepcopy(g.pods[2])])).encode())
            return g.fake_run(command, **kw)
        self.runtime.run = run
        commit = f.execution["sourceRevisions"]["techx-platform"]
        self.manifest = dict(commit=commit, tag="staging-" + commit[:12], images=[dict(component=name,
            image=f"058114477594.dkr.ecr.us-east-1.amazonaws.com/techx/{name}:staging-{commit[:12]}", digest="sha256:" + "e" * 64)
            for name in ("frontend", "catalog", "order")])
        sdk = botocore.session.get_session()
        def api(service, operation, callback):
            def call(**kw):
                validate_parameters(kw, sdk.get_service_model(service).operation_model(operation).input_shape)
                return callback(**kw)
            return call
        f.clients["ecr"] = SimpleNamespace(
            describe_repositories=api("ecr", "DescribeRepositories", lambda **kw: dict(repositories=[dict(imageTagMutability="IMMUTABLE")])),
            describe_images=api("ecr", "DescribeImages", lambda **kw: dict(imageDetails=[dict(imageDigest="sha256:" + "e" * 64)])),
            describe_image_scan_findings=api("ecr", "DescribeImageScanFindings", lambda **kw: dict(imageScanStatus=dict(status=self.scan), imageScanFindings=dict(findingSeverityCounts={}))))
        f.clients["dynamodb"] = SimpleNamespace(
            describe_table=api("dynamodb", "DescribeTable", lambda **kw: dict(Table=dict(TableArn="arn:aws:dynamodb:us-east-1:058114477594:table/techx-staging-orders",
                TableStatus="ACTIVE", BillingModeSummary=dict(BillingMode=self.table_mode)))),
            describe_time_to_live=api("dynamodb", "DescribeTimeToLive", lambda **kw: dict(TimeToLiveDescription=dict(TimeToLiveStatus="ENABLED", AttributeName="ttlEpochSeconds"))))
        for name, value in (("read_release", dict(manifest=self.manifest)), ("read_selection", dict(chartSha=self.chart_sha))):
            mock = patch("private_acceptance." + name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.task_id = "4" * 32
        self.task, self.pointer = create_task(f.clients["s3"], "control-bucket", f.record, "execution-v1", f.instance_id,
            f.owner, "verify", "private", 1, self.task_id, f.now)
        f.record["deploymentTask"] = self.pointer
        claim_task(f.store, f.clients["s3"], "control-bucket", self.task_id, f.instance_id, f.now)
        self.owner = self.public.edge.owner
        case = self
        class Tunnel:
            def __init__(self, *args, **kw): self.disconnected = False
            @contextmanager
            def opened(self):
                try: yield self
                finally: self.disconnected = case.disconnect
            def evidence(self):
                return dict(endpointId=case.public.edge.vpn_id, connectionId="cvpn-connection-1234567890abcdef0",
                    clientIp="172.24.0.10", commonName="techx-demo-operator", device="tx4444444444", disconnected=self.disconnected)
        class HTTP:
            def __init__(self, tunnel, guard): self.guard, self.observations = guard, []
            def request(self, method, path, *, payload=None, token=None, idempotency_key=None):
                self.guard()
                self.observations.append(dict(method=method, path=path, status=200))
                if path == "/argocd/api/v1/session":
                    case.assertEqual(payload, dict(username="admin", password="fixture-private-password"))
                    return (200, dict(token="header.payload.signature")) if case.auth else (401, {})
                if path == "/argocd/api/v1/session/userinfo": return 200, dict(loggedIn=True, username="admin")
                if path.startswith("/argocd/"):
                    return (200, deepcopy(app)) if token else (401, dict(error="unauthorized"))
                if not case.public.service.orders and method == "GET": return 404, dict(error="not found")
                response = case.public.service.request(method, path, body=json.dumps(payload).encode() if payload else None,
                    headers={"Idempotency-Key": idempotency_key} if idempotency_key else {})
                return response["status"], json.loads(response["body"])
        self.worker = PrivateAcceptance(self.runtime, f.store, "execution-v1", self.task_id, clock=lambda: f.now,
            sleep=lambda _: None, tunnel_factory=Tunnel, http_factory=HTTP)

    def perform(self):
        self.f.record = claim(self.f.record, self.owner, "deploy", now=self.f.now)
        return self.worker.perform(self.owner, self.f.record["lease"]["epoch"])

    def test_full_private_suite_has_exact_receipt_resilience_and_disconnected_vpn(self):
        self.perform()
        proof = read_private_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        self.assertTrue(proof["vpn"]["disconnected"])
        self.assertIn("order-restart-persistence", proof["checks"])
        self.assertEqual(self.f.record["status"], "PROVISIONING")
        self.assertNotIn("acceptedRelease", self.f.record)
        self.assertNotIn("fixture-private-password", json.dumps(proof))
        self.assertNotIn("header.payload.signature", json.dumps(proof))
        self.assertFalse(any(command[0] in ("apply", "create") for command in self.calls))

    def complete_parent(self, *, finish=True):
        case = self
        class Child:
            def wait(self, **kw):
                case.assertEqual(case.f.record["status"], "PROVISIONING")
                case.worker.perform(case.owner, case.f.record["lease"]["epoch"])
                case.assertNotIn("acceptedRelease", case.f.record)
                return 0
            def poll(self): return 0
        changes = []
        original = self.f.store.write
        def write(record, etag):
            changes.append(deepcopy(record))
            original(record, etag)
        self.f.store.write = write
        execute(self.f.store, self.owner, "deploy", self.f.record["sessionId"], ["private-worker"], popen=lambda *a, **kw: Child(),
            success_status="READY", ready_release=lambda record: ready_release(self.f.clients["s3"], "control-bucket", record, "execution-v1"))
        ready_writes = [record for record in changes if record["status"] == "READY"]
        self.assertEqual(len(ready_writes), 1)
        self.assertIn("acceptedRelease", ready_writes[0])
        self.assertNotIn("lease", ready_writes[0])
        if finish:
            finish_task(self.f.store, self.task, True, self.f.now)
            self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")

    def test_ready_and_accepted_release_are_published_atomically_only_after_terminal_child(self):
        self.complete_parent()

    def test_fixed_ssm_loader_runs_private_parent_and_accepts_only_atomic_ready_handoff(self):
        sys.path.insert(0, str(Path(__file__).parents[1] / "bootstrap/automation"))
        from deployment_task_loader import admitted_task, run_task
        f = self.f
        f.record["deploymentTask"]["state"] = "DISPATCHING"
        f.identity.update(Arn=f"arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-executor/{f.instance_id}")
        self.assertEqual(admitted_task(f.clients["s3"], "control-bucket", self.task_id, f.instance_id, f.now)["mode"], "verify")
        def bundled(client, bucket, session, version, directory):
            directory.mkdir(parents=True)
            (directory / "execution.json").write_text(json.dumps(f.execution))
            return directory / "source/techx-infra"
        def child(command, **kw):
            self.assertTrue(command[1].endswith("run_private_acceptance.py"))
            self.assertEqual(command[2:5], ["verify", "--stage", "private"])
            self.assertEqual(command[-2:], ["--task-id", self.task_id])
            self.complete_parent(finish=False)
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory, patch("deployment_task_loader.load", side_effect=bundled), \
                patch("automation_session.S3SessionStore", return_value=f.store), \
                patch("deployment_runtime.TerraformRuntime", return_value=self.runtime):
            run_task(f.clients, "control-bucket", "test-kms", self.task_id, directory, run=child, clock=lambda: f.now)
        self.assertEqual(f.record["status"], "READY")
        self.assertEqual(f.record["deploymentTask"]["state"], "SUCCEEDED")
        with self.assertRaises(ValueError): admitted_task(f.clients["s3"], "control-bucket", self.task_id, f.instance_id, f.now)

    def test_runtime_wrong_digest_never_passes(self):
        self.g.pods[0]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "f" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertNotIn("acceptedRelease", self.f.record)

    def test_pending_scan_and_wrong_dynamodb_config_are_rejected(self):
        self.scan = "IN_PROGRESS"
        with self.assertRaises(PrivateFailure): self.perform()
        self.assertNotIn("acceptedRelease", self.f.record)

    def test_wrong_dynamodb_billing_and_local_mode_are_rejected(self):
        for local in (False, True):
            self.table_mode = "PROVISIONED" if not local else "PAY_PER_REQUEST"
            if local:
                self.g.objects[("deployment", "order-api", "techx-staging")]["spec"]["template"]["spec"]["containers"][0]["env"].append(dict(name="ORDER_LOCAL_MODE", value="true"))
            with self.subTest(local=local), self.assertRaisesRegex(PrivateFailure, "dynamodb-"):
                self.perform()
            self.f.record = release(self.f.record, self.owner, self.f.record["lease"]["epoch"])
            self.f.record["stages"].pop("private")

    def test_private_readiness_cannot_repair_missing_gates_and_hide_the_failure(self):
        self.g.pods[0]["spec"]["readinessGates"] = []
        with self.assertRaisesRegex(PrivateFailure, "runtime-readiness"): self.perform()
        self.assertFalse(any(args[:2] == ["rollout", "restart"] for args in self.calls))

    def test_released_worker_checkpoint_alone_is_not_a_successful_remote_task(self):
        self.perform()
        self.f.record = release(self.f.record, self.owner, self.f.record["lease"]["epoch"])
        with self.assertRaises(ValueError): finish_task(self.f.store, self.task, True, self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "RUNNING")

    def test_consumed_task_or_changed_public_predecessor_cannot_be_reused(self):
        self.perform()
        self.f.record = release(self.f.record, self.owner, self.f.record["lease"]["epoch"])
        self.f.record["deploymentTask"]["state"] = "DISPATCHING"
        self.f.record["status"] = "READY"
        with self.assertRaises(ValueError): claim_task(self.f.store, self.f.clients["s3"], "control-bucket", self.task_id, self.f.instance_id, self.f.now)
        self.f.record["stages"]["publicAcceptance"]["receiptReference"]["sha256"] = "f" * 64
        with self.assertRaises(ValueError): read_task(self.f.clients["s3"], "control-bucket", self.f.record, self.task_id, self.f.now)

    def test_ready_observation_requires_terminal_manager_ssm_and_same_receipts(self):
        self.complete_parent()
        f = self.f
        f.record["deploymentTask"]["commandId"] = f.command_id
        op = operation("verify", "private")
        build_id = f.owner.split(":build/", 1)[1]
        f.record["lifecycleDispatch"] = dict(phase="OBSERVED", id="c" * 64, operation=op, buildId=build_id)
        build = dict(id=build_id, arn=f.owner, projectName="techx-automation-lifecycle",
            serviceRole="arn:aws:iam::058114477594:role/techx-automation-lifecycle-manager", buildStatus="SUCCEEDED", buildComplete=True,
            environment=dict(environmentVariables=[dict(name=key, value=value, type="PLAINTEXT") for key, value in environment(f.record, op).items()]))
        f.clients["codebuild"] = SimpleNamespace(batch_get_builds=lambda **kw: dict(builds=[deepcopy(build)]))
        f.identity.update(Arn="arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/test")
        f.statuses, f.remote_result = ["Success"], lambda: None
        dispatcher = Dispatcher(f.clients, f.store, "control-bucket", f.execution, "execution-v1", clock=lambda: f.now)
        self.assertEqual(next_operation(f.record, op), dict(waiting="READY"))
        self.assertEqual(f.manager.wait(deepcopy(f.record["deploymentTask"]), max_observations=1)["state"], "SUCCEEDED")
        self.assertEqual(dispatcher.advance()["waiting"], "READY")
        from continue_lifecycle import drive
        coordinator = SimpleNamespace(resume_approved=lambda _: self.fail("READY observation must not consume another apply"))
        self.assertEqual(drive(dispatcher, coordinator, resume_approved=True, observations=1)["waiting"], "READY")
        with self.assertRaises(ValueError): dispatcher.start(deepcopy(f.record), op)
        build["buildStatus"] = "IN_PROGRESS"
        with self.assertRaises(ValueError): dispatcher.advance()
        build["buildStatus"] = "SUCCEEDED"
        f.statuses = ["InProgress"]
        with self.assertRaises(ValueError): dispatcher.advance()
        f.statuses = ["Success"]
        f.record["acceptedRelease"]["images"]["frontend"] = f.record["acceptedRelease"]["images"]["frontend"].replace("e" * 64, "f" * 64)
        with self.assertRaises(ValueError): dispatcher.advance()

    def test_auth_failure_is_not_treated_as_network_success(self):
        self.auth = False
        with self.assertRaisesRegex(PrivateFailure, "argocd-authentication"): self.perform()
        self.assertFalse(any(command[:2] == ["scale", "deployment/frontend"] for command in self.calls))

    def test_failed_self_heal_restores_replica_but_never_accepts_it(self):
        self.heal = False
        with self.assertRaisesRegex(PrivateFailure, "self-heal-timeout"): self.perform()
        self.assertEqual(self.g.objects[("deployment", "frontend", "techx-staging")]["spec"]["replicas"], 1)
        self.assertNotIn("acceptedRelease", self.f.record)

    def test_lost_order_after_restart_and_unproven_restart_are_rejected(self):
        self.persist = False
        with self.assertRaisesRegex(PrivateFailure, "order-persistence"): self.perform()
        self.assertNotIn("acceptedRelease", self.f.record)

    def test_vpn_must_be_positively_disconnected_before_checkpoint(self):
        self.disconnect = False
        with self.assertRaisesRegex(PrivateFailure, "vpn-disconnect-unproven"): self.perform()
        self.assertNotIn("acceptedRelease", self.f.record)
        self.assertNotEqual(self.f.record["stages"]["private"]["state"], "PASSED")


if __name__ == "__main__": unittest.main()
