"""Leased cold acceptance through a real Client VPN client on the fixed EC2."""
import base64
from copy import deepcopy
import importlib.util
import re
import time

from automation_budget import validate_estimate
from cleanup_schedule import verify_existing
from deployment_tasks import read_task
from edge_activation import edge_binding, read_activation_receipt
from gitops_bootstrap import COMPONENTS, GitOpsBootstrap
from chart_selection import read_release, read_selection
from lifecycle_stage import StageController
from plan_review_store import checksum, immutable_json
from private_acceptance_contract import CHECKS, private_context, read_private_receipt, validate_private_start
from private_http import PrivateFailure, PrivateHTTP
from public_acceptance_contract import read_public_receipt
from public_probe import checkout_payload
from vpn_probe import VpnTunnel
from workload_state import read_state


class PrivateAcceptance:
    def __init__(self, runtime, store, version, task_id, *, clock, sleep=time.sleep, tunnel_factory=VpnTunnel, http_factory=PrivateHTTP):
        self.runtime, self.store, self.version, self.task_id = runtime, store, version, task_id
        self.clock, self.sleep = clock, sleep
        self.controller = StageController(runtime.clients["s3"], store, runtime.bucket, clock)
        self.tunnel_factory, self.http_factory = tunnel_factory, http_factory

    def validate_record(self, record):
        if (any(record.get(key) != self.runtime.execution.get(key) for key in ("sessionId", "account", "region", "environment",
                "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"))
                or record.get("executionVersion") != self.version or record["status"] != "PROVISIONING" or record.get("pendingPlan") is not None):
            raise PrivateFailure("private-session-context")
        task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
        if ((task["mode"], task["stage"], task["attempt"]) != ("verify", "private", 1)
                or record["deploymentTask"].get("state") != "RUNNING"
                or task["instanceId"] != self.runtime.private["deploymentExecutor"]["instanceId"]
                or task["expectedAcceptanceSha256"] != checksum(private_context(record))
                or hasattr(self, "context_hash") and task["expectedAcceptanceSha256"] != self.context_hash):
            raise PrivateFailure("private-task-context")
        return record

    def guard(self):
        record, _ = self.controller.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.validate_record(record)
        validate_estimate(record, self.runtime.execution["costEstimate"])
        verify_existing(self.runtime.clients["scheduler"], record, self.version, self.clock())
        return record

    def binding_guard(self):
        record = self.guard()
        state = read_state(self.runtime.clients["s3"], self.runtime.bucket)
        if checksum(state) != self.state_hash or edge_binding(state, record, self.runtime.execution, self.runtime.private) != self.binding:
            raise PrivateFailure("private-state-changed")
        return record

    def checkpoint(self, **fields):
        self.binding_guard()
        current, etag = self.controller.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.validate_record(current)
        stage = current.get("stages", {}).get("private", {})
        if stage.get("state") == "PASSED": raise PrivateFailure("private-task-consumed")
        updated = deepcopy(current)
        updated["stages"].setdefault("private", {}).update(fields)
        self.store.write(updated, etag)

    def wait_ready(self, *, observations=60):
        for _ in range(observations):
            self.binding_guard()
            if self.kube.observe_ready(allow_readiness_restart=False) and self.kube.observe_alb() == self.binding["albArn"]:
                return
            self.sleep(5)
        raise PrivateFailure("runtime-readiness")

    def artifacts(self, release):
        client = self.runtime.clients["ecr"]
        for image in release["manifest"]["images"]:
            name = "techx/" + image["component"]
            self.binding_guard()
            repositories = client.describe_repositories(repositoryNames=[name]).get("repositories", [])
            if len(repositories) != 1 or repositories[0].get("imageTagMutability") != "IMMUTABLE":
                raise PrivateFailure("ecr-immutability")
            self.binding_guard()
            images = client.describe_images(repositoryName=name, imageIds=[dict(imageTag=release["manifest"]["tag"])]).get("imageDetails", [])
            if len(images) != 1 or images[0].get("imageDigest") != image["digest"]: raise PrivateFailure("ecr-digest")
            self.binding_guard()
            scan = client.describe_image_scan_findings(repositoryName=name, imageId=dict(imageDigest=image["digest"]))
            counts = scan.get("imageScanFindings", {}).get("findingSeverityCounts", {})
            if (scan.get("imageScanStatus", {}).get("status") != "COMPLETE" or not isinstance(scan.get("imageScanFindings"), dict)
                    or any(type(counts.get(level, 0)) is not int or counts.get(level, 0) != 0 for level in ("HIGH", "CRITICAL"))):
                raise PrivateFailure("ecr-scan")

    def persistence(self, state):
        outputs = {key: value.get("value") for key, value in state.get("outputs", {}).items()}
        table = "techx-staging-orders"
        role = outputs.get("order_api_role_arn", "")
        if (outputs.get("order_table_name") != table or outputs.get("order_table_arn") != f"arn:aws:dynamodb:us-east-1:058114477594:table/{table}"
                or not re.fullmatch(r"arn:aws:iam::058114477594:role/techx-staging-order-api", role)):
            raise PrivateFailure("dynamodb-state")
        deployment = self.kube.get("deployment", "order-api", "techx-staging")
        template = deployment["spec"]["template"]["spec"]
        env = {row["name"]: row for row in template["containers"][0].get("env", [])}
        if (template.get("serviceAccountName") != "order-api" or env.get("AWS_REGION", {}).get("value") != "us-east-1"
                or env.get("ORDER_TABLE_NAME", {}).get("value") != table or "DYNAMODB_ENDPOINT" in env
                or env.get("ORDER_LOCAL_MODE", {}).get("value") == "true" or template["containers"][0].get("envFrom")
                or any(name in env for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"))):
            raise PrivateFailure("dynamodb-runtime-config")
        account = self.kube.get("serviceaccount", "order-api", "techx-staging")
        if account.get("metadata", {}).get("annotations", {}).get("eks.amazonaws.com/role-arn") != role:
            raise PrivateFailure("dynamodb-irsa")
        self.binding_guard()
        observed = self.runtime.clients["dynamodb"].describe_table(TableName=table)["Table"]
        if observed.get("TableArn") != outputs["order_table_arn"] or observed.get("TableStatus") != "ACTIVE" or observed.get("BillingModeSummary", {}).get("BillingMode") != "PAY_PER_REQUEST":
            raise PrivateFailure("dynamodb-table")
        self.binding_guard()
        ttl = self.runtime.clients["dynamodb"].describe_time_to_live(TableName=table).get("TimeToLiveDescription", {})
        if ttl.get("AttributeName") != "ttlEpochSeconds" or ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
            raise PrivateFailure("dynamodb-ttl")

    def argocd(self, http):
        status, _ = http.request("GET", "/argocd/api/v1/applications/techx-staging")
        if status not in (401, 403): raise PrivateFailure("argocd-auth-required")
        secret = self.kube.get("secret", "argocd-initial-admin-secret", "argocd") or {}
        encoded = secret.get("data", {}).get("password", "")
        try: password = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeError): raise PrivateFailure("argocd-credential") from None
        if not 8 <= len(password) <= 256: raise PrivateFailure("argocd-credential")
        status, session = http.request("POST", "/argocd/api/v1/session", payload=dict(username="admin", password=password))
        del password
        token = session.get("token", "") if isinstance(session, dict) else ""
        if status != 200 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise PrivateFailure("argocd-authentication")
        status, info = http.request("GET", "/argocd/api/v1/session/userinfo", token=token)
        if status != 200 or info.get("loggedIn") is not True or info.get("username") != "admin":
            raise PrivateFailure("argocd-authentication")
        status, app = http.request("GET", "/argocd/api/v1/applications/techx-staging", token=token)
        if status != 200: raise PrivateFailure("argocd-authorization")
        source, observed = app.get("spec", {}).get("source", {}), app.get("status", {})
        if (source.get("targetRevision") != self.selected["chartSha"] or source.get("repoURL") != "https://github.com/cloud-computing-microservice/techx-chart.git"
                or observed.get("sync", {}).get("status") != "Synced" or observed.get("sync", {}).get("revision") != self.selected["chartSha"]
                or observed.get("health", {}).get("status") != "Healthy"):
            raise PrivateFailure("argocd-revision-health")

    def resilience(self, http, public):
        summary = public["result"].get("orderSummary", {})
        if (not re.fullmatch(r"ord_[a-f0-9-]{8,64}", summary.get("id", ""))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", summary.get("productId", ""))
                or any(type(summary.get(key)) is not int for key in ("subtotalCents", "shippingCents", "totalCents"))):
            raise PrivateFailure("public-order-proof")
        def lookup():
            status, value = http.request("GET", "/api/orders/" + summary["id"])
            order = value.get("order", {})
            if status != 200 or any(order.get(key) != summary[key] for key in ("id", "subtotalCents", "shippingCents", "totalCents")):
                raise PrivateFailure("order-persistence")
        lookup()
        self.checkpoint(resilience=dict(state="CALLING", test="frontend-replica-drift"))
        try:
            changed = self.kube.kubectl("scale", "deployment/frontend", "--replicas=2", "-n", "techx-staging", "-o", "json")
            if changed.get("spec", {}).get("replicas") != 2: raise PrivateFailure("self-heal-drift-unproven")
            for _ in range(30):
                current = self.kube.get("deployment", "frontend", "techx-staging")
                if current.get("spec", {}).get("replicas") == 1: break
                self.sleep(5)
            else: raise PrivateFailure("self-heal-timeout")
            self.wait_ready()
        except BaseException:
            try: self.kube.kubectl("scale", "deployment/frontend", "--replicas=1", "-n", "techx-staging", json_output=False)
            except Exception: pass  # Cleanup/lease fencing must win.
            raise
        self.checkpoint(resilience=dict(state="CALLING", test="order-restart"))
        before = self.kube.get("deployment", "order-api", "techx-staging")["metadata"]["generation"]
        pods = self.kube.kubectl("get", "pods", "-n", "techx-staging", "-l", "app.kubernetes.io/component=order-api", "-o", "json")["items"]
        previous = {row["metadata"]["uid"] for row in pods if not row["metadata"].get("deletionTimestamp")}
        if len(previous) != 1: raise PrivateFailure("order-restart-baseline")
        self.kube.kubectl("rollout", "restart", "deployment/order-api", "-n", "techx-staging", json_output=False)
        self.wait_ready()
        after = self.kube.get("deployment", "order-api", "techx-staging")["metadata"]["generation"]
        pods = self.kube.kubectl("get", "pods", "-n", "techx-staging", "-l", "app.kubernetes.io/component=order-api", "-o", "json")["items"]
        current = {row["metadata"]["uid"] for row in pods if not row["metadata"].get("deletionTimestamp")}
        if after <= before or len(current) != 1 or current & previous: raise PrivateFailure("order-restart-unproven")
        lookup()
        status, value = http.request("POST", "/api/orders", payload=checkout_payload(summary["productId"]),
            idempotency_key="acceptance-" + public["dispatchId"])
        if status != 200 or value.get("idempotentReplay") is not True or value.get("order", {}).get("id") != summary["id"]:
            raise PrivateFailure("idempotency-restart-persistence")
        self.checkpoint(resilience=dict(state="PASSED"))

    def perform(self, owner, epoch):
        self.owner, self.epoch = owner, epoch
        record = self.guard()
        self.context_hash = validate_private_start(record, 1)
        public = read_public_receipt(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)
        activation = read_activation_receipt(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)
        state = read_state(self.runtime.clients["s3"], self.runtime.bucket)
        self.state_hash = checksum(state)
        if self.state_hash != activation["stateSha256"]: raise PrivateFailure("private-state-changed")
        self.binding = edge_binding(state, record, self.runtime.execution, self.runtime.private)
        self.selected = read_selection(self.runtime.clients["s3"], self.runtime.bucket, record, self.runtime.execution, self.version)
        release = read_release(self.runtime.clients["s3"], self.runtime.bucket, record, self.runtime.execution, self.version)
        spec = importlib.util.spec_from_file_location("private_manifest", self.runtime.source / "techx-platform/scripts/prepare-chart-release.py")
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        validator.validate_manifest(release["manifest"])
        images = {COMPONENTS[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in release["manifest"]["images"]}
        self.checkpoint(state="RUNNING", attempt=1, taskId=self.task_id, contextSha256=self.context_hash)
        self.kube = GitOpsBootstrap(self.runtime, self.store, self.version, self.task_id, clock=self.clock, sleep=self.sleep)
        self.kube.guard = self.binding_guard
        self.kube.selected, self.kube.images = self.selected, images
        self.kube.cluster()
        self.wait_ready()
        self.artifacts(release)
        self.persistence(state)
        tunnel = self.tunnel_factory(self.runtime, self.binding_guard, self.binding, self.task_id, sleep=self.sleep)
        with tunnel.opened():
            http = self.http_factory(tunnel, self.binding_guard)
            self.argocd(http)
            self.resilience(http, public)
            self.argocd(http)
            self.wait_ready()
        if not tunnel.disconnected: raise PrivateFailure("vpn-disconnect-unproven")
        self.binding_guard()
        value = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=self.version, taskId=self.task_id,
            contextSha256=self.context_hash, stateSha256=self.state_hash, publicReference=record["stages"]["publicAcceptance"]["receiptReference"],
            checks=sorted(CHECKS), vpn=tunnel.evidence(), privateObservations=http.observations,
            completedAt=self.clock().isoformat(),
            release=dict(platformSha=release["manifest"]["commit"], chartSha=self.selected["chartSha"], images=images))
        self.binding_guard()
        ref = immutable_json(self.runtime.clients["s3"], self.runtime.bucket,
            f"private/staging/{record['sessionId']}/acceptance/{self.task_id}.json", value)
        ref["sha256"] = checksum(value)
        proposed = deepcopy(self.guard())
        proposed["stages"]["private"].update(state="PASSED", release=value["release"], receiptReference=ref)
        read_private_receipt(self.runtime.clients["s3"], self.runtime.bucket, proposed, self.version)
        self.checkpoint(state="PASSED", release=value["release"], receiptReference=ref)
        return dict(state="PRIVATE_PASSED", receiptReference=ref)
