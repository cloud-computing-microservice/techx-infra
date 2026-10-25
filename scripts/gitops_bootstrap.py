"""Leased, immutable cold-start GitOps bootstrap on the dedicated EC2 executor."""
import base64
from copy import deepcopy
import json
import importlib.util
import re
import time

from automation_budget import validate_estimate
from bootstrap_assets import AssetBootstrap
from chart_selection import CHART_REPO, read_release, read_selection
from cleanup_schedule import verify_existing
from deployment_tasks import read_task, validate_bootstrap
from lifecycle_stage import StageController
from plan_review_store import ACCOUNT

CLUSTER = NAMESPACE = APPLICATION = "techx-staging"
SESSION_ANNOTATION = "techx.io/session-id"
READINESS = "target-health.elbv2.k8s.aws/"
ALB = rf"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/[A-Za-z0-9-]+/[a-f0-9]+"
COMPONENTS = {"frontend": "frontend", "catalog": "catalog-api", "order": "order-api"}
NAMESPACE_LABELS = {"elbv2.k8s.aws/pod-readiness-gate-inject": "enabled",
    **{f"pod-security.kubernetes.io/{key}": "restricted" for key in ("enforce", "warn", "audit")}}


class GitOpsBootstrap:
    def __init__(self, runtime, store, execution_version, task_id, *, clock, sleep=time.sleep, assets=None):
        self.runtime, self.store, self.version, self.task_id = runtime, store, execution_version, task_id
        self.clock, self.sleep = clock, sleep
        self.controller = StageController(runtime.clients["s3"], store, runtime.bucket, clock)
        self.assets = assets

    def guard(self):
        record, _ = self.controller.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        for key in ("sessionId", "account", "region", "environment", "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"):
            if record.get(key) != self.runtime.execution.get(key):
                raise ValueError("GitOps session differs from immutable execution")
        if (record.get("executionVersion") != self.version or record["status"] != "PROVISIONING"
                or record.get("pendingPlan") is not None or record.get("stages", {}).get("edge")
                or record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED"):
            raise ValueError("GitOps requires the admitted pre-edge foundation session")
        task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
        if ((task["mode"], task["stage"], task["attempt"]) != ("bootstrap", "gitops", 1)
                or record["deploymentTask"].get("state") != "RUNNING"
                or task["instanceId"] != self.runtime.private["deploymentExecutor"]["instanceId"]):
            raise ValueError("GitOps is not the current claimed remote task")
        validate_estimate(record, self.runtime.execution["costEstimate"])
        verify_existing(self.runtime.clients["scheduler"], record, self.version, self.clock())
        if hasattr(self, "selection_ref") and (record.get("bootstrapChart") != self.selection_ref or record.get("bootstrapRelease") != self.release_ref):
            raise ValueError("GitOps immutable selection/release changed")
        return record

    def checkpoint(self, **fields):
        self.guard()
        record, etag = self.controller.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        # Revalidate the same CAS snapshot, not just an earlier guard read.
        lease = record.get("lease", {})
        if (record.get("sessionId") != self.runtime.execution["sessionId"] or lease.get("owner") != self.owner
                or lease.get("epoch") != self.epoch or record.get("status") != "PROVISIONING"
                or record.get("bootstrapChart") != self.selection_ref or record.get("bootstrapRelease") != self.release_ref):
            raise ValueError("GitOps checkpoint lost its lease/session")
        updated = deepcopy(record)
        updated.setdefault("stages", {}).setdefault("gitops", {}).update(fields)
        self.store.write(updated, etag)

    def kubectl(self, *args, value=None, json_output=True):
        self.guard()
        command = ["kubectl", "--kubeconfig", str(self.kubeconfig), "--context", CLUSTER, "--request-timeout=20s", *args]
        # Secret payloads travel on stdin, never argv/last-applied annotations.
        result = self.runtime.run(command, env=self.env, check=True, capture_output=True, timeout=90,
            **({"input": json.dumps(value).encode()} if value is not None else {}))
        output = result.stdout
        if len(output) > 2 * 1024 * 1024: raise ValueError("Kubernetes response exceeds bootstrap policy")
        return (json.loads(output) if output.strip() else None) if json_output else output

    def get(self, kind, name, namespace=None):
        args = ["get", kind, name, "--ignore-not-found", "-o", "json"]
        if namespace: args += ["-n", namespace]
        return self.kubectl(*args)

    def apply(self, value):
        return self.kubectl("apply", "--server-side", "--field-manager=techx-bootstrap", "-f", "-", "-o", "json", value=value)

    def cluster(self):
        cidr = self.runtime.executor()
        value = self.runtime.clients["eks"].describe_cluster(name=CLUSTER)["cluster"]
        network = value.get("resourcesVpcConfig", {})
        if (value.get("name") != CLUSTER or value.get("arn") != f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/{CLUSTER}"
                or value.get("status") != "ACTIVE" or value.get("version") != "1.35"
                or not re.fullmatch(r"https://[A-Za-z0-9.-]+\.eks\.amazonaws\.com", value.get("endpoint", ""))
                or network.get("endpointPublicAccess") is not True or network.get("publicAccessCidrs") != [cidr]
                or not re.fullmatch(r"vpc-[a-f0-9]{8,17}", network.get("vpcId", ""))
                or any(value.get("tags", {}).get(key) != val for key, val in {"Project": "techx", "Environment": "staging"}.items())):
            raise ValueError("EKS identity, ownership or executor access differs from foundation")
        ca = value.get("certificateAuthority", {}).get("data", "")
        decoded = base64.b64decode(ca, validate=True)
        if len(decoded) > 16 * 1024 or not decoded.startswith(b"-----BEGIN CERTIFICATE-----"):
            raise ValueError("EKS certificate authority is invalid")
        self.vpc = network["vpcId"]
        self.kubeconfig = self.runtime.work / "gitops.private.kubeconfig.json"
        config = {"apiVersion": "v1", "kind": "Config", "current-context": CLUSTER,
            "clusters": [{"name": CLUSTER, "cluster": {"server": value["endpoint"], "certificate-authority-data": ca}}],
            "contexts": [{"name": CLUSTER, "context": {"cluster": CLUSTER, "user": CLUSTER}}],
            "users": [{"name": CLUSTER, "user": {"exec": {"apiVersion": "client.authentication.k8s.io/v1beta1",
                "command": "aws", "args": ["eks", "get-token", "--region", "us-east-1", "--cluster-name", CLUSTER], "interactiveMode": "Never"}}}]}
        self.kubeconfig.write_text(json.dumps(config), encoding="utf-8")
        self.kubeconfig.chmod(0o600)
        self.env = {key: val for key, val in self.runtime.env.items() if not key.upper().startswith(("KUBE", "HELM"))}
        self.env["KUBECONFIG"] = str(self.kubeconfig)

    def owned(self, value):
        if value and value.get("metadata", {}).get("annotations", {}).get(SESSION_ANNOTATION) != self.runtime.execution["sessionId"]:
            raise ValueError("Existing Kubernetes object is not owned by this session")

    def namespace_and_secret(self):
        existing = self.get("namespace", NAMESPACE)
        self.owned(existing)
        self.apply(dict(apiVersion="v1", kind="Namespace", metadata=dict(name=NAMESPACE,
            labels=NAMESPACE_LABELS, annotations={SESSION_ANNOTATION: self.runtime.execution["sessionId"]})))
        observed = self.get("namespace", NAMESPACE)
        if not observed or any(observed.get("metadata", {}).get("labels", {}).get(key) != value for key, value in NAMESPACE_LABELS.items()):
            raise ValueError("Namespace policy/readiness labels must exist before pods")
        material = self.assets.secret("order-api-key")
        data = {key: base64.b64encode(value.encode()).decode() for key, value in material.items()}
        existing = self.get("secret", "techx-staging-secrets", NAMESPACE)
        self.owned(existing)
        if existing:
            if existing.get("type") != "Opaque" or existing.get("immutable") is not True or existing.get("data") != data:
                raise ValueError("Existing Order Secret differs; bootstrap must never rotate it")
        else:
            self.kubectl("create", "-f", "-", "-o", "json", value=dict(apiVersion="v1", kind="Secret", type="Opaque", immutable=True,
                metadata=dict(name="techx-staging-secrets", namespace=NAMESPACE, annotations={SESSION_ANNOTATION: self.runtime.execution["sessionId"]}), data=data))

    def application(self):
        filename = self.runtime.source / "techx-chart/gitops/clusters/staging/application.yaml"
        app = self.kubectl("create", "--dry-run=client", "--validate=false", "-f", str(filename), "-o", "json")
        source = app.get("spec", {}).get("source", {})
        if (app.get("kind") != "Application" or app.get("apiVersion") != "argoproj.io/v1alpha1"
                or app.get("metadata", {}).get("name") != APPLICATION or app["metadata"].get("namespace") != "argocd"
                or source != dict(repoURL=f"https://github.com/{CHART_REPO}.git", targetRevision="main", path=".",
                    helm=dict(valueFiles=["values-staging.yaml", "values-domain-vpn.yaml"]))
                or app["spec"].get("destination") != dict(server="https://kubernetes.default.svc", namespace=NAMESPACE)
                or app["spec"].get("project") != "default"):
            raise ValueError("Bundled Application differs from the reviewed staging contract")
        app["spec"]["source"]["targetRevision"] = self.selected["chartSha"]
        app["metadata"].setdefault("annotations", {})[SESSION_ANNOTATION] = self.runtime.execution["sessionId"]
        existing = self.get("application", APPLICATION, "argocd")
        self.owned(existing)
        if existing and existing.get("spec") != app["spec"]:
            raise ValueError("Existing Application selects different GitOps configuration")
        self.apply(app)

    def observe_ready(self):
        app = self.get("application", APPLICATION, "argocd") or {}
        if app.get("spec", {}).get("source", {}).get("targetRevision") != self.selected["chartSha"]:
            raise ValueError("Application revision changed during bootstrap")
        status = app.get("status", {})
        if (status.get("sync", {}).get("status") != "Synced" or status.get("sync", {}).get("revision") != self.selected["chartSha"]
                or status.get("health", {}).get("status") != "Healthy"):
            return False
        for name, image in self.images.items():
            deployment = self.get("deployment", name, NAMESPACE)
            if not deployment: return False
            containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            if len(containers) != 1 or containers[0].get("name") != name or containers[0].get("image") != image:
                raise ValueError("Deployment image differs from the cold release digest")
            state, desired = deployment.get("status", {}), deployment["spec"].get("replicas")
            if (desired != 1 or state.get("observedGeneration", 0) < deployment["metadata"]["generation"]
                    or any(state.get(key, 0) != desired for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))
                    or state.get("unavailableReplicas", 0)):
                return False
        pods = self.kubectl("get", "pods", "-n", NAMESPACE, "-l", "app.kubernetes.io/instance=techx-staging", "-o", "json")["items"]
        active = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
        if len(active) != 3: return False
        seen = set()
        frontend = None
        for pod in active:
            name = pod["metadata"].get("labels", {}).get("app.kubernetes.io/component")
            if name not in self.images or name in seen: raise ValueError("Unexpected or duplicate workload pod")
            seen.add(name)
            spec, status = pod.get("spec", {}), pod.get("status", {})
            if len(spec.get("containers", [])) != 1 or spec["containers"][0].get("image") != self.images[name]:
                raise ValueError("Pod image differs from the selected digest")
            containers = status.get("containerStatuses", [])
            if len(containers) != 1 or containers[0].get("ready") is not True: return False
            digest = self.images[name].split("@", 1)[1]
            if not containers[0].get("imageID", "").endswith("@" + digest) and containers[0].get("imageID") != "containerd://" + digest:
                raise ValueError("Observed running container digest differs from the release")
            if status.get("phase") != "Running" or not any(row.get("type") == "Ready" and row.get("status") == "True" for row in status.get("conditions", [])):
                return False
            if name == "frontend": frontend = pod
        gates = [row["conditionType"] for row in frontend["spec"].get("readinessGates", []) if row.get("conditionType", "").startswith(READINESS)]
        if not gates:
            bindings = self.kubectl("get", "targetgroupbindings.elbv2.k8s.aws", "-n", NAMESPACE, "-o", "json")["items"]
            if not any(row.get("spec", {}).get("serviceRef", {}).get("name") == "frontend" and row["spec"].get("targetType") == "ip" for row in bindings):
                return False
            stage = self.guard().get("stages", {}).get("gitops", {})
            if not stage.get("readinessRestartIssued"):
                self.checkpoint(readinessRestartIssued=True)
                self.kubectl("rollout", "restart", "deployment/frontend", "-n", NAMESPACE, json_output=False)
            return False
        if any(not any(row.get("type") == gate and row.get("status") == "True" for row in frontend["status"].get("conditions", [])) for gate in gates):
            return False
        return True

    def observe_alb(self):
        ingresses = [self.get("ingress", "frontend", NAMESPACE), self.get("ingress", "argocd-server", "argocd")]
        hosts = []
        for ingress in ingresses:
            if not ingress: return None
            if ingress.get("metadata", {}).get("annotations", {}).get("alb.ingress.kubernetes.io/group.name") != "techx-private":
                raise ValueError("Application/management ingress group changed")
            rows = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if len(rows) != 1 or not rows[0].get("hostname"): return None
            hosts.append(rows[0]["hostname"])
        if len(set(hosts)) != 1: return None
        result = self.runtime.clients["resourcegroupstaggingapi"].get_resources(ResourceTypeFilters=["elasticloadbalancing:loadbalancer"],
            TagFilters=[dict(Key=key, Values=[value]) for key, value in {"elbv2.k8s.aws/cluster": CLUSTER, "ingress.k8s.aws/stack": "techx-private"}.items()])
        rows = result.get("ResourceTagMappingList", [])
        if result.get("PaginationToken") or len(rows) > 1: raise ValueError("Ambiguous staging ALB inventory")
        if not rows: return None
        arn = rows[0].get("ResourceARN", "")
        if not re.fullmatch(ALB, arn): raise ValueError("ALB is outside the admitted account/region")
        observed = self.runtime.clients["elbv2"].describe_load_balancers(LoadBalancerArns=[arn])["LoadBalancers"]
        if len(observed) != 1: raise ValueError("ALB identity is ambiguous")
        value = observed[0]
        if (value.get("LoadBalancerArn") != arn or value.get("DNSName") != hosts[0] or value.get("Scheme") != "internal"
                or value.get("Type") != "application" or value.get("VpcId") != self.vpc):
            raise ValueError("Observed ALB differs from the internal staging ingress")
        return arn if value.get("State", {}).get("Code") == "active" else None

    def perform(self, owner, epoch, *, observations=180):
        if type(observations) is not int or not 1 <= observations <= 180: raise ValueError("Bounded readiness observation is required")
        self.owner, self.epoch = owner, epoch
        record = self.guard()
        validate_bootstrap(record, 1)
        self.selected = read_selection(self.runtime.clients["s3"], self.runtime.bucket, record, self.runtime.execution, self.version)
        release = read_release(self.runtime.clients["s3"], self.runtime.bucket, record, self.runtime.execution, self.version)
        # The renderer's manifest validator checks account/repository/all three components.
        spec = importlib.util.spec_from_file_location("bootstrap_chart_manifest", self.runtime.source / "techx-platform/scripts/prepare-chart-release.py")
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        validator.validate_manifest(release["manifest"])
        self.images = {COMPONENTS[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in release["manifest"]["images"]}
        self.selection_ref, self.release_ref = deepcopy(record["bootstrapChart"]), deepcopy(record["bootstrapRelease"])
        self.checkpoint(state="BOOTSTRAPPING", attempt=1, selectionReference=self.selection_ref, releaseReference=self.release_ref,
            chartSha=self.selected["chartSha"], startedAt=self.clock().isoformat())
        self.cluster()
        self.assets = self.assets or AssetBootstrap(self.runtime, self.guard)
        self.kubectl("rollout", "status", "deployment/aws-load-balancer-controller", "-n", "kube-system", "--timeout=60s", json_output=False)
        self.kubectl("rollout", "status", "statefulset/argocd-application-controller", "-n", "argocd", "--timeout=60s", json_output=False)
        self.kubectl("rollout", "status", "deployment/argocd-repo-server", "-n", "argocd", "--timeout=60s", json_output=False)
        self.namespace_and_secret()
        self.application()
        for _ in range(observations):
            self.guard()
            if self.observe_ready():
                arn = self.observe_alb()
                if arn:
                    self.assets.save(lambda private: private["terraformVars"].update(internal_alb_arn=arn))
                    self.checkpoint(state="BOOTSTRAPPED", internalAlbArn=arn, images=self.images, completedAt=self.clock().isoformat())
                    return dict(chartSha=self.selected["chartSha"], internalAlbArn=arn)
            self.sleep(5)
        raise TimeoutError("GitOps readiness was not proven before the bounded observation ended")
