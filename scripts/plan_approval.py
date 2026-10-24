"""Offline contract for a private saved plan and its approval.

This module does not authenticate GitHub reviewers. Only the future trusted
approval bridge may call issue_approval and write the result to its IAM-scoped
prefix. A deploy worker must fetch that object/version using AWS, never accept
an approval JSON supplied by workflow_dispatch or a pull request.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import re

from automation_budget import approved_budget
from automation_session import utc, validate

SOURCE_REPOS = {"techx-infra", "techx-chart", "techx-platform"}
SNAPSHOT_FIELDS = {
    "sourceRevisions", "approvedBudgetUsd", "deadline", "cleanupStartAt",
    "configSha256", "providerLockSha256", "terraformVersion", "backendBucket",
    "backendKey", "stateLineage", "stateSerial", "executorRoleArn", "callerPublicCidr",
}
MAX_PLAN_BYTES = 64 * 1024 * 1024


def digest(value):
    if not isinstance(value, bytes) or not value or len(value) > MAX_PLAN_BYTES:
        raise ValueError("A nonempty bounded saved plan is required")
    return hashlib.sha256(value).hexdigest()


def version(value):
    if not isinstance(value, str) or not value or value == "null" or len(value) > 1024:
        raise ValueError("An exact private S3 object version is required")


def snapshot(value):
    if not isinstance(value, dict) or set(value) != SNAPSHOT_FIELDS:
        raise ValueError("Incomplete or unexpected plan context")
    sources = value["sourceRevisions"]
    if (not isinstance(sources, dict) or set(sources) != SOURCE_REPOS
            or any(not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in sources.values())):
        raise ValueError("Exactly three full reviewed revisions are required")
    approved_budget(value["approvedBudgetUsd"])
    for key in ("configSha256", "providerLockSha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key]):
            raise ValueError("Config and provider lock checksums are required")
    if value["terraformVersion"] != "1.15.5":
        raise ValueError("Unreviewed Terraform toolchain")
    if not isinstance(value["backendBucket"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value["backendBucket"]):
        raise ValueError("Invalid backend bucket")
    if value["backendKey"] != "staging/terraform.tfstate":
        raise ValueError("Unexpected staging backend key")
    if not isinstance(value["stateLineage"], str) or not re.fullmatch(r"[a-f0-9-]{36}", value["stateLineage"]):
        raise ValueError("Observed initialized state lineage is required")
    if type(value["stateSerial"]) is not int or value["stateSerial"] < 0:
        raise ValueError("Observed state serial is required")
    if value["executorRoleArn"] != "arn:aws:iam::058114477594:role/techx-automation-lifecycle-executor":
        raise ValueError("Unexpected deployment identity")
    cidr = ipaddress.ip_network(value["callerPublicCidr"])
    if cidr.version != 4 or cidr.prefixlen != 32 or not cidr.network_address.is_global:
        raise ValueError("One observed public executor IPv4 /32 is required")
    if utc(value["cleanupStartAt"]) >= utc(value["deadline"]):
        raise ValueError("Cleanup reserve must precede the deadline")
    return value


def context_for_session(session, value, now):
    validate(session)
    snapshot(value)
    if session["status"] not in ("PLANNING", "PROVISIONING") or utc(session["cleanupStartAt"]) <= now:
        raise ValueError("Session is closed for deployment")
    for key in ("sourceRevisions", "approvedBudgetUsd", "deadline", "cleanupStartAt"):
        if session.get(key) != value[key]:
            raise ValueError(f"Plan context differs from session: {key}")


def validate_request(request, now):
    if request.get("schemaVersion") != 1 or request.get("stage") not in ("foundation", "edge"):
        raise ValueError("Unsupported plan approval schema or stage")
    if not re.fullmatch(r"staging-[a-zA-Z0-9-]{1,64}", request.get("sessionId", "")):
        raise ValueError("Invalid session ID")
    if type(request.get("attempt")) is not int or request["attempt"] < 1:
        raise ValueError("Invalid stage attempt")
    value = snapshot(request["context"])
    obj = request["plan"]
    prefix = f"private/staging/{request['sessionId']}/plans/{request['stage']}/{request['attempt']}/"
    if obj.get("bucket") != value["backendBucket"] or obj.get("key") != prefix + "saved.tfplan":
        raise ValueError("Plan object is outside the approved session/stage prefix")
    version(obj.get("versionId"))
    if not isinstance(obj.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", obj["sha256"]):
        raise ValueError("Invalid saved plan checksum")
    created, expires = utc(request["createdAt"]), utc(request["expiresAt"])
    if not created <= now < expires <= utc(value["cleanupStartAt"]):
        raise ValueError("Plan approval window is invalid or expired")


def create_request(session, stage, attempt, plan_bytes, object_version, context, *, expires_at, now=None):
    now = now or datetime.now(timezone.utc)
    context_for_session(session, context, now)
    request = dict(schemaVersion=1, sessionId=session["sessionId"], stage=stage, attempt=attempt,
                   context=deepcopy(context), createdAt=now.isoformat(), expiresAt=expires_at,
                   plan=dict(bucket=context["backendBucket"],
                             key=f"private/staging/{session['sessionId']}/plans/{stage}/{attempt}/saved.tfplan",
                             versionId=object_version, sha256=digest(plan_bytes)))
    validate_request(request, now)
    return request


def issue_approval(request, *, reviewer, review_run, now=None):
    """Trusted bridge only; caller must verify GitHub gate and reviewer identity."""
    now = now or datetime.now(timezone.utc)
    validate_request(request, now)
    if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", reviewer):
        raise ValueError("Verified reviewer login is required")
    if not isinstance(review_run, str) or not re.fullmatch(
            r"https://github\.com/cloud-computing-microservice/techx-infra/actions/runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*)?", review_run):
        raise ValueError("A trusted infra review workflow run is required")
    return dict(schemaVersion=1, request=deepcopy(request), reviewer=reviewer, reviewRun=review_run,
                approvedAt=now.isoformat())


def authorize_apply(session, request, approval, context, plan_bytes, *, plan_object_version, now=None):
    """Validate freshly observed inputs against an approval fetched from trusted storage.

    The worker must still acquire/retain the deploy lease and verify cleanup
    scheduling before mutation. Terraform's backend lock remains mandatory.
    """
    now = now or datetime.now(timezone.utc)
    validate_request(request, now)
    context_for_session(session, context, now)
    version(plan_object_version)
    if session["sessionId"] != request["sessionId"] or context != request["context"]:
        raise ValueError("Session, source, budget, state, backend or executor context changed; replan required")
    if request["plan"]["versionId"] != plan_object_version or request["plan"]["sha256"] != digest(plan_bytes):
        raise ValueError("Downloaded saved plan differs from reviewed object/version")
    if approval.get("schemaVersion") != 1 or approval.get("request") != request:
        raise ValueError("Approval is for a different saved plan")
    approved_at = utc(approval["approvedAt"])
    if not utc(request["createdAt"]) <= approved_at <= now:
        raise ValueError("Approval timestamp is invalid")
    # Validate audit fields again when reading a persisted approval record.
    issue_approval(request, reviewer=approval.get("reviewer"), review_run=approval.get("reviewRun"), now=now)
    return dict(sessionId=request["sessionId"], stage=request["stage"], attempt=request["attempt"],
                planSha256=request["plan"]["sha256"], planVersionId=plan_object_version)
