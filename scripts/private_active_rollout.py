"""One approved private chart merge, exact main CI and pinned Application rollout."""
from copy import deepcopy
from datetime import timedelta
import json
import re
import time

from active_release_contract import ActiveReleaseController, release_context, read_rollout_receipt
from automation_budget import validate_estimate
from automation_session import utc
from chart_promotion import LIVE
from chart_selection import CHART_REPO, PLATFORM_REPO, sha
from cleanup_schedule import verify_existing
from deployment_tasks import read_task
from github_app_auth import app_secret_snapshot, installation_client
from gitops_bootstrap import APPLICATION, COMPONENTS, GitOpsBootstrap, SESSION_ANNOTATION
from plan_approval import version
from plan_review_store import ACCOUNT, checksum, encoded, immutable_json, read_json
from private_chart_promotion import PrivateChartPromotion, MergeUncertain, app_clients
from private_credentials import credential_proof
from ready_chart_promotion import ReadyPromotionChecks
from ready_release_contract import accepted_baseline, read_manifest
from ready_release_publish import CONTEXT
from workflow_handoff import listed_json

STAMP = "techx.io/release-intent"


def active_clients(runtime, policy, clock, execution_version):
    chart, infra = app_clients(runtime, policy, clock, execution_version)
    lifecycle, ref = app_secret_snapshot(runtime.clients["secretsmanager"], "lifecycle", kms_key=runtime.kms)
    proof = credential_proof(runtime.private, runtime.execution, execution_version)["lifecycle"]
    if ref != proof["secret"] or any(lifecycle[key] != proof["identity"][key] for key in ("appId", "slug", "installationId")):
        raise ValueError("Active source reader differs from the pinned private Lifecycle App")
    return chart, infra, installation_client(lifecycle, PLATFORM_REPO, runtime.work, clock)


class PrivateActiveRollout(PrivateChartPromotion):
    def __init__(self, runtime, store, execution_version, task_id, *, clock, sleep=time.sleep, clients=active_clients):
        self.runtime, self.store, self.version, self.task_id = runtime, store, execution_version, task_id
        self.clock, self.sleep, self.client_factory = clock, sleep, clients
        self.control = ActiveReleaseController(store, clock)
        self.checks = ReadyPromotionChecks(runtime.clients, store, runtime.bucket, runtime.execution, execution_version,
            clock=clock, platform=None, testing_task=task_id)

    def clients(self, runtime, policy, clock, execution_version):
        chart, infra, platform = self.client_factory(runtime, policy, clock, execution_version)
        self.checks.platform = platform
        return chart, infra

    def approval_reference(self, record): return release_context(record)["approvalReference"]

    def guard(self):
        record, _ = self.control.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        if (any(record.get(key) != self.runtime.execution.get(key) for key in CONTEXT)
                or record.get("executionVersion") != self.version):
            raise ValueError("Active rollout differs from immutable execution")
        context = release_context(record)
        task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
        instance = self.runtime.private["deploymentExecutor"]["instanceId"]
        if ((task["mode"], task["stage"], task["attempt"]) != ("release", "active", 1)
                or task["instanceId"] != instance or self.owner != f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{instance}"
                or record["deploymentTask"].get("state") != "RUNNING" or task["expectedReleaseContext"] != context
                or hasattr(self, "context") and self.context != context
                or hasattr(self, "approval_ref") and self.approval_ref != context["approvalReference"]):
            raise ValueError("Active rollout is not this executor's unchanged claimed task")
        validate_estimate(record, self.runtime.execution["costEstimate"])
        verify_existing(self.runtime.clients["scheduler"], record, self.version, self.clock())
        accepted_baseline(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)
        return record

    def current_chart(self, chosen):
        self.guard()
        if self.chart.get("git/ref/heads/main").get("object", {}).get("sha") != chosen:
            raise ValueError("Chart main moved from this exact promoted release")

    def main_ci(self, chosen):
        end = min(self.clock() + timedelta(minutes=10), utc(self.guard()["cleanupStartAt"]) - timedelta(seconds=60))
        for _ in range(120):
            if self.clock() >= end: break
            self.current_chart(chosen)
            result = self.chart.get(f"actions/workflows/verify.yml/runs?head_sha={chosen}&per_page=100")
            rows = result.get("workflow_runs")
            if not isinstance(rows, list) or len(rows) > 100 or type(result.get("total_count")) is not int or result["total_count"] != len(rows):
                raise ValueError("Complete merged chart CI history required")
            valid = [row for row in rows if row.get("head_sha") == chosen and row.get("head_branch") == "main" and row.get("event") in ("push", "workflow_dispatch")
                and row.get("path") in (".github/workflows/verify.yml", ".github/workflows/verify.yml@main")
                and all(row.get(key, {}).get("full_name") == CHART_REPO for key in ("repository", "head_repository"))]
            if rows and not valid: raise ValueError("Merged chart CI has no trusted main run")
            if any(type(row.get("id")) is not int or row["id"] < 1 for row in valid): raise ValueError("Invalid merged CI run identity")
            if valid:
                latest = max(valid, key=lambda row: row["id"])
                actual = self.chart.get(f"actions/runs/{latest['id']}")
                if any(actual.get(key) != latest.get(key) for key in ("id", "head_sha", "head_branch", "event", "path", "repository", "head_repository")):
                    raise ValueError("Actual merged CI differs from history")
                if actual.get("status") == "completed":
                    if actual.get("conclusion") != "success": raise ValueError("Latest merged CI failed; do not change the Application")
                    self.current_chart(chosen)
                    return f"https://github.com/{CHART_REPO}/actions/runs/{actual['id']}"
                if actual.get("status") not in LIVE: raise ValueError("Unknown merged CI state")
            self.sleep(5)
        raise TimeoutError("The same promoted chart CI did not pass within the original window")

    def app_observation(self, app, expected_spec, uid, stamp):
        metadata = (app or {}).get("metadata", {})
        if (metadata.get("uid") != uid or metadata.get("name") != APPLICATION or metadata.get("namespace") != "argocd"
                or metadata.get("annotations", {}).get(SESSION_ANNOTATION) != self.runtime.execution["sessionId"]
                or metadata.get("annotations", {}).get(STAMP) != stamp or app.get("spec") != expected_spec):
            raise MergeUncertain("Application mutation lacks its exact UID/session/spec/intent stamp")

    def application(self, receipt, merge_ref, ci_run):
        record = self.guard()
        self.context = release_context(record)
        key = f"private/staging/{record['sessionId']}/active/{self.context['releaseId']}/application/intent.json"
        self.kube = GitOpsBootstrap(self.runtime, self.store, self.version, self.task_id, clock=self.clock, sleep=self.sleep)
        self.kube.guard = self.guard
        self.kube.cluster()
        template = self.kube.kubectl("create", "--dry-run=client", "--validate=false", "-f", str(self.runtime.source / "techx-chart/gitops/clusters/staging/application.yaml"), "-o", "json")
        previous_spec = deepcopy(template.get("spec", {}))
        previous_spec.setdefault("source", {})["targetRevision"] = self.context["acceptedBaseline"]["chartSha"]
        if (previous_spec.get("source") != dict(repoURL=f"https://github.com/{CHART_REPO}.git", targetRevision=self.context["acceptedBaseline"]["chartSha"], path=".", helm=dict(valueFiles=["values-staging.yaml", "values-domain-vpn.yaml"]))
                or previous_spec.get("destination") != dict(server="https://kubernetes.default.svc", namespace="techx-staging")):
            raise ValueError("Bundled Application is outside the admitted chart/namespace")
        expected_spec = deepcopy(previous_spec)
        expected_spec["source"]["targetRevision"] = receipt["chartSha"]
        app = self.kube.get("application", APPLICATION, "argocd") or {}
        metadata = app.get("metadata", {})
        if (not isinstance(metadata.get("uid"), str) or not 1 <= len(metadata["uid"]) <= 128
                or metadata.get("name") != APPLICATION or metadata.get("namespace") != "argocd"
                or metadata.get("annotations", {}).get(SESSION_ANNOTATION) != record["sessionId"]):
            raise ValueError("Existing Application does not belong to this session")
        expected = dict(schemaVersion=1, kind="active-application-intent", sessionId=record["sessionId"], executionVersion=self.version,
            taskId=self.task_id, releaseContext=self.context, mergeReceiptReference=merge_ref, chartSha=receipt["chartSha"],
            uid=metadata["uid"], previousSpec=previous_spec)
        existing = listed_json(self.runtime.clients["s3"], self.runtime.bucket, key)
        if existing:
            intent, intent_ref = existing
            if {name: value for name, value in intent.items() if name != "createdAt"} != expected:
                raise ValueError("Application intent was superseded or belongs to another task")
        else:
            if app.get("spec") != previous_spec: raise ValueError("Application no longer selects the accepted baseline")
            intent = dict(expected, createdAt=self.clock().isoformat())
            intent_ref = self.put(key, intent)
        stamp = checksum(intent)
        calling_key = key.replace("intent.json", "calling.json")
        marker = dict(schemaVersion=1, intentReference=intent_ref)
        prior = listed_json(self.runtime.clients["s3"], self.runtime.bucket, calling_key)
        if prior and prior[0] != marker: raise ValueError("Application call marker changed")
        if prior is None:
            self.runtime.executor()
            self.main_ci(receipt["chartSha"])
            try:
                response = self.runtime.clients["s3"].put_object(Bucket=self.runtime.bucket, Key=calling_key, Body=encoded(marker), IfNoneMatch="*", ExpectedBucketOwner=ACCOUNT, ContentType="application/json")
                version(response.get("VersionId"))
            except Exception:
                # A lost marker response cannot authorize a second patch.
                self.app_observation(self.kube.get("application", APPLICATION, "argocd"), expected_spec, intent["uid"], stamp)
            else:
                self.guard()
                self.current_chart(receipt["chartSha"])
                operations = [dict(op="test", path="/metadata/uid", value=intent["uid"]), dict(op="test", path="/spec", value=previous_spec),
                    dict(op="test", path="/metadata/annotations/techx.io~1session-id", value=record["sessionId"]),
                    dict(op="add", path="/metadata/annotations/techx.io~1release-intent", value=stamp),
                    dict(op="replace", path="/spec/source/targetRevision", value=receipt["chartSha"])]
                try:
                    self.kube.kubectl("patch", "application", APPLICATION, "-n", "argocd", "--type=json", "--patch-file=/dev/stdin", "-o", "json", value=operations)
                except Exception:
                    self.app_observation(self.kube.get("application", APPLICATION, "argocd"), expected_spec, intent["uid"], stamp)
        self.app_observation(self.kube.get("application", APPLICATION, "argocd"), expected_spec, intent["uid"], stamp)
        manifest = read_manifest(self.runtime.clients["s3"], self.runtime.bucket, self.guard(), self.version)["manifest"]
        self.kube.selected = dict(chartSha=receipt["chartSha"])
        self.kube.images = {COMPONENTS[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in manifest["images"]}
        end = min(self.clock() + timedelta(minutes=10), utc(record["cleanupStartAt"]) - timedelta(seconds=60))
        for _ in range(120):
            if self.clock() >= end: break
            self.guard()
            if self.kube.observe_ready(allow_readiness_restart=False):
                self.current_chart(receipt["chartSha"])
                return dict(uid=intent["uid"], intentReference=intent_ref, specSha256=checksum(expected_spec), stamp=stamp), deepcopy(self.kube.images)
            self.sleep(5)
        raise TimeoutError("Argo/runtime did not reach this exact promoted revision in the original window")

    def checkpoint(self, receipt, ref, request):
        chosen = sha(receipt["chartSha"])
        ci_run = self.main_ci(chosen)
        application, images = self.application(receipt, ref, ci_run)
        ci_run = self.main_ci(chosen)
        current, etag = self.control.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.guard()
        value = dict(schemaVersion=1, kind="active-rollout", sessionId=current["sessionId"], executionVersion=self.version,
            releaseContext=release_context(current), taskId=self.task_id, chartSha=chosen, mergeReceiptReference=ref, chartCi=ci_run,
            application=application, images=images, completedAt=self.clock().isoformat())
        key = f"private/staging/{current['sessionId']}/active/{self.context['releaseId']}/rollout.json"
        result = self.put(key, value)
        current, etag = self.control.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.guard()
        updated = deepcopy(current)
        updated["releaseCandidate"]["state"] = "ROLLED_OUT"
        updated["releaseCandidate"]["rollout"] = dict(state="ROLLED_OUT", taskId=self.task_id, contextSha256=checksum(release_context(current)), chartSha=chosen, receiptReference=result)
        read_rollout_receipt(self.runtime.clients["s3"], self.runtime.bucket, updated, self.version)
        self.store.write(updated, etag)
        return dict(state="ACTIVE_ROLLED_OUT", chartSha=chosen, receiptReference=result)
