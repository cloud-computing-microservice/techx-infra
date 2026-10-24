from copy import deepcopy
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from automation_session import claim, release
from deployment_tasks import ACCOUNT, claim_task, create_task, finish_task, validate_gitops_checkpoint
from gitops_bootstrap import GitOpsBootstrap, NAMESPACE_LABELS, READINESS, SESSION_ANNOTATION

spec = importlib.util.spec_from_file_location("chart_selection_fixture", Path(__file__).with_name("test-chart-selection.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class GitOpsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.SelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = f = self.fixture
        f.select()
        self.instance = "i-1234567890abcdef0"
        self.owner = f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{self.instance}"
        self.task_id = "3" * 32
        _, pointer = create_task(f.clients["s3"], "control-bucket", f.record, "exec-v1", self.instance,
            f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/techx-automation-lifecycle:abc-def", "bootstrap", "gitops", 1, self.task_id, f.now)
        f.record["deploymentTask"] = pointer
        claim_task(f.store, f.clients["s3"], "control-bucket", self.task_id, self.instance, f.now)
        f.record = claim(f.record, self.owner, "deploy", now=f.now)
        self.epoch = f.record["lease"]["epoch"]
        self.session = f.record["sessionId"]
        self.work = f.source / "work"
        self.work.mkdir()
        self.runtime = SimpleNamespace(source=f.source, work=self.work, execution=f.execution, clients=f.clients, bucket="control-bucket",
            env=dict(KUBECONFIG="untrusted", KUBERNETES_MASTER="http://untrusted", HELM_KUBECONTEXT="other", AWS_REGION="us-east-1"),
            executor=lambda: "8.8.8.8/32", private=dict(schemaVersion=1, sessionId=self.session,
            deploymentExecutor=dict(instanceId=self.instance), terraformVars={}))
        self.cluster = dict(name="techx-staging", arn=f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/techx-staging", version="1.35", status="ACTIVE",
            endpoint="https://TEST.gr7.us-east-1.eks.amazonaws.com", tags=dict(Project="techx", Environment="staging"),
            certificateAuthority=dict(data=base64.b64encode(b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----").decode()),
            resourcesVpcConfig=dict(vpcId="vpc-1234567890abcdef0", endpointPublicAccess=True, publicAccessCidrs=["8.8.8.8/32"]))
        self.alb_arn = f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/techx/123abc"
        self.alb = dict(LoadBalancerArn=self.alb_arn, DNSName="internal-techx.us-east-1.elb.amazonaws.com", Scheme="internal",
            Type="application", VpcId="vpc-1234567890abcdef0", State=dict(Code="active"))
        self.albs = [self.alb]
        self.tag_result = dict(ResourceTagMappingList=[dict(ResourceARN=self.alb_arn)])
        f.clients.update(eks=SimpleNamespace(describe_cluster=lambda **kw: dict(cluster=deepcopy(self.cluster))),
            elbv2=SimpleNamespace(describe_load_balancers=lambda **kw: dict(LoadBalancers=deepcopy(self.albs))),
            resourcegroupstaggingapi=SimpleNamespace(get_resources=lambda **kw: deepcopy(self.tag_result)))
        self.assets = SimpleNamespace(secret=lambda purpose: {"order-api-key": "s" * 64}, save=self.save)
        self.calls, self.objects = [], {}
        self.base_app = dict(apiVersion="argoproj.io/v1alpha1", kind="Application", metadata=dict(name="techx-staging", namespace="argocd"),
            spec=dict(project="default", source=dict(repoURL="https://github.com/cloud-computing-microservice/techx-chart.git", targetRevision="main", path=".",
                helm=dict(valueFiles=["values-staging.yaml", "values-domain-vpn.yaml"])), destination=dict(server="https://kubernetes.default.svc", namespace="techx-staging")))
        self.pods = []
        for component, name in {"frontend": "frontend", "catalog": "catalog-api", "order": "order-api"}.items():
            image = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/techx/{component}@sha256:" + "e" * 64
            self.objects[("deployment", name, "techx-staging")] = dict(metadata=dict(generation=1),
                spec=dict(replicas=1, template=dict(spec=dict(containers=[dict(name=name, image=image)]))),
                status=dict(observedGeneration=1, replicas=1, updatedReplicas=1, readyReplicas=1, availableReplicas=1))
            conditions = [dict(type="Ready", status="True")]
            gate = READINESS + "frontend"
            conditions += [dict(type=gate, status="True")] if component == "frontend" else []
            self.pods.append(dict(metadata=dict(name=name, labels={"app.kubernetes.io/component": name}),
                spec=dict(containers=[dict(name=name, image=image)], readinessGates=[dict(conditionType=gate)] if component == "frontend" else []),
                status=dict(phase="Running", conditions=conditions, containerStatuses=[dict(name=name, ready=True, imageID="containerd://sha256:" + "e" * 64)])))
        for name, namespace in [("frontend", "techx-staging"), ("argocd-server", "argocd")]:
            self.objects[("ingress", name, namespace)] = dict(metadata=dict(annotations={"alb.ingress.kubernetes.io/group.name": "techx-private"}),
                status=dict(loadBalancer=dict(ingress=[dict(hostname=self.alb["DNSName"])])))
        self.restart_adds_gate = True
        self.cancel_on_sleep = False
        self.runtime.run = self.fake_run
        self.worker = GitOpsBootstrap(self.runtime, f.store, "exec-v1", self.task_id, clock=lambda: f.now, sleep=self.sleep, assets=self.assets)

    def save(self, change):
        self.worker.guard()
        change(self.runtime.private)

    def sleep(self, seconds):
        if self.cancel_on_sleep: self.f.record["status"] = "DESTROYING"

    def fake_run(self, command, **kw):
        self.assertEqual(command[:2], ["kubectl", "--kubeconfig"])
        self.assertEqual(command[command.index("--context") + 1], "techx-staging")
        self.assertEqual(kw["timeout"], 90)
        self.assertNotIn("KUBERNETES_MASTER", kw["env"])
        self.assertNotIn("HELM_KUBECONTEXT", kw["env"])
        args = command[command.index("--request-timeout=20s") + 1:]
        self.calls.append((args, kw.get("input")))
        value = None
        if args[0] == "rollout":
            if args[1] == "restart" and self.restart_adds_gate:
                self.pods[0]["spec"]["readinessGates"] = [dict(conditionType=READINESS + "frontend")]
            return SimpleNamespace(stdout=b"rollout complete\n")
        if args[:2] == ["create", "--dry-run=client"]: value = self.base_app
        elif args[0] in ("apply", "create"):
            value = json.loads(kw["input"])
            kind = value["kind"].lower()
            metadata = value["metadata"]
            self.objects[(kind, metadata["name"], metadata.get("namespace"))] = deepcopy(value)
            if kind == "application":
                self.objects[(kind, metadata["name"], metadata.get("namespace"))]["status"] = dict(
                    sync=dict(status="Synced", revision="b" * 40), health=dict(status="Healthy"))
        elif args[:2] == ["get", "pods"]: value = dict(items=self.pods)
        elif args[:2] == ["get", "targetgroupbindings.elbv2.k8s.aws"]:
            value = dict(items=[dict(spec=dict(serviceRef=dict(name="frontend"), targetType="ip"))])
        elif args[0] == "get":
            value = self.objects.get((args[1], args[2], args[args.index("-n") + 1] if "-n" in args else None))
        else: raise AssertionError(args)
        return SimpleNamespace(stdout=json.dumps(value).encode() if value else b"")

    def perform(self, observations=2): return self.worker.perform(self.owner, self.epoch, observations=observations)

    def test_full_bootstrap_pins_chart_injects_secret_and_records_internal_alb(self):
        result = self.perform()
        self.assertEqual(result["internalAlbArn"], self.alb_arn)
        self.assertEqual(self.runtime.private["terraformVars"]["internal_alb_arn"], self.alb_arn)
        stage = validate_gitops_checkpoint(self.f.record)
        self.assertEqual(stage["chartSha"], "b" * 40)
        app = self.objects[("application", "techx-staging", "argocd")]
        self.assertEqual(app["spec"]["source"]["targetRevision"], "b" * 40)
        secret = self.objects[("secret", "techx-staging-secrets", "techx-staging")]
        self.assertTrue(secret["immutable"])
        self.assertNotIn("kubectl.kubernetes.io/last-applied-configuration", secret["metadata"]["annotations"])
        self.assertTrue(all("s" * 64 not in json.dumps(args) for args, _ in self.calls))
        ns = self.objects[("namespace", "techx-staging", None)]
        self.assertTrue(all(ns["metadata"]["labels"][key] == value for key, value in NAMESPACE_LABELS.items()))

    def test_cannot_start_without_selected_chart_or_after_edge(self):
        self.f.record.pop("bootstrapChart")
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)

    def test_changed_selection_after_dispatch_is_rejected(self):
        self.f.record["bootstrapChart"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)

    def test_foreign_namespace_or_changed_secret_is_not_overwritten(self):
        self.objects[("namespace", "techx-staging", None)] = dict(metadata=dict(annotations={SESSION_ANNOTATION: "another-session"}))
        with self.assertRaisesRegex(ValueError, "not owned"): self.perform()
        self.assertFalse(any(args[0] == "apply" for args, _ in self.calls))

    def test_owned_secret_with_another_key_cannot_be_rotated(self):
        self.objects[("secret", "techx-staging-secrets", "techx-staging")] = dict(type="Opaque", immutable=True,
            metadata=dict(annotations={SESSION_ANNOTATION: self.session}), data={"order-api-key": "wrong"})
        with self.assertRaisesRegex(ValueError, "never rotate"): self.perform()
        self.assertFalse(any(args[0] == "create" and args[1] == "-f" for args, _ in self.calls))

    def test_wrong_cluster_or_open_egress_never_mutates_kubernetes(self):
        self.cluster["resourcesVpcConfig"]["publicAccessCidrs"] = ["0.0.0.0/0"]
        with self.assertRaisesRegex(ValueError, "EKS identity"): self.perform()
        self.assertFalse(self.calls)

    def test_wrong_pod_digest_never_publishes_ready(self):
        self.pods[1]["status"]["containerStatuses"][0]["imageID"] = "containerd://sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "container digest"): self.perform()
        self.assertEqual(self.f.record["stages"]["gitops"]["state"], "BOOTSTRAPPING")
        self.assertNotIn("internal_alb_arn", self.runtime.private["terraformVars"])

    def test_cold_pods_get_one_durable_restart_after_target_binding_exists(self):
        self.pods[0]["spec"]["readinessGates"] = []
        self.perform()
        self.assertTrue(self.f.record["stages"]["gitops"]["readinessRestartIssued"])
        self.assertEqual(sum(args[:2] == ["rollout", "restart"] for args, _ in self.calls), 1)

    def test_missing_gate_cannot_trigger_repeated_restarts_or_fake_success(self):
        self.pods[0]["spec"]["readinessGates"] = []
        self.restart_adds_gate = False
        with self.assertRaises(TimeoutError): self.perform(3)
        self.assertEqual(sum(args[:2] == ["rollout", "restart"] for args, _ in self.calls), 1)
        with self.assertRaises(ValueError): self.perform()

    def test_external_or_ambiguous_alb_cannot_be_handed_to_edge(self):
        self.alb["Scheme"] = "internet-facing"
        with self.assertRaisesRegex(ValueError, "internal staging"): self.perform()
        self.assertNotIn("internal_alb_arn", self.runtime.private["terraformVars"])

    def test_cleanup_takeover_stops_polling_without_completion(self):
        self.pods[0]["status"]["containerStatuses"][0]["ready"] = False
        self.cancel_on_sleep = True
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["stages"]["gitops"]["state"], "BOOTSTRAPPING")

    def test_loader_result_needs_checkpoint_matching_the_dispatched_selection(self):
        with self.assertRaises(ValueError):
            finish_task(self.f.store, {**self.f.record["deploymentTask"], "sessionId": self.session, "executionVersion": "exec-v1"}, True, self.f.now)
        self.perform()
        task = {**self.f.record["deploymentTask"], "sessionId": self.session, "executionVersion": "exec-v1"}
        self.f.record = release(self.f.record, self.owner, self.epoch)
        finish_task(self.f.store, task, True, self.f.now)
        self.assertEqual(self.f.record["deploymentTask"]["state"], "SUCCEEDED")


if __name__ == "__main__": unittest.main()
