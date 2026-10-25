"""Versioned private review requests; only the approval bridge writes approvals."""
from copy import deepcopy
import hashlib
import json
import re

from plan_approval import MAX_PLAN_BYTES, authorize_apply, validate_request, version
from automation_budget import validate_estimate
from automation_session import utc

ACCOUNT = "058114477594"
MAX_REVIEW_BYTES = 256 * 1024


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def checksum(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def summarize_changes(plan):
    """Exclude values, outputs and sensitive markers from the reviewer summary.

    The planner must run review-plan.py first; this is a disclosure filter and
    must not replace its resource/stage policy checks.
    """
    changes = []
    for resource in plan.get("resource_changes", []):
        if resource.get("mode") == "data" or resource["change"]["actions"] == ["no-op"]:
            continue
        changes.append(dict(address=resource["address"], actions=resource["change"]["actions"]))
    validate_changes(changes)
    return sorted(changes, key=lambda row: row["address"])


def validate_changes(changes):
    if not isinstance(changes, list) or len(changes) > 500:
        raise ValueError("A bounded sanitized resource summary is required")
    seen = set()
    for change in changes:
        if (not isinstance(change, dict) or set(change) != {"address", "actions"}
                or not isinstance(change["address"], str)
                or not re.fullmatch(r'[A-Za-z0-9_.\[\]"/-]{1,256}', change["address"])
                or change["address"] in seen or change["actions"] != ["create"]):
            raise ValueError("Foundation/edge review permits distinct create-only resource addresses")
        seen.add(change["address"])


def review_key(request):
    return f"private/staging/{request['sessionId']}/plans/{request['stage']}/{request['attempt']}/request.json"


def approval_key(request, review_sha256):
    if not isinstance(review_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", review_sha256):
        raise ValueError("Exact review checksum is required")
    return f"approvals/staging/{request['sessionId']}/{request['stage']}/{request['attempt']}/{review_sha256}.json"


def read_json(client, bucket, key, object_version=None):
    args = dict(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
    if object_version is not None:
        version(object_version)
        args["VersionId"] = object_version
    response = client.get_object(**args)
    body = response["Body"].read(MAX_REVIEW_BYTES + 1)
    if len(body) > MAX_REVIEW_BYTES:
        raise ValueError("Private review object exceeds size limit")
    if object_version is not None and response.get("VersionId") != object_version:
        raise ValueError("Private review read returned an unexpected version")
    return json.loads(body), response.get("VersionId")


def immutable_json(client, bucket, key, value, equivalent=None):
    body = encoded(value)
    if len(body) > MAX_REVIEW_BYTES:
        raise ValueError("Private review object exceeds size limit")
    try:
        response = client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*",
                                    ExpectedBucketOwner=ACCOUNT, ContentType="application/json")
        object_version = response.get("VersionId")
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed":
            raise
        existing, object_version = read_json(client, bucket, key)
        if not (equivalent(existing, value) if equivalent else existing == value):
            raise ValueError("Immutable review key already contains different content") from error
    version(object_version)
    return dict(key=key, versionId=object_version)


def publish_review(client, bucket, request, changes, now, *, estimate):
    validate_request(request, now)
    validate_changes(changes)
    if request["context"]["backendBucket"] != bucket:
        raise ValueError("Review must use the session's private control bucket")
    validate_estimate(dict(request["context"], maximumHours=estimate.get("maximumHours")), estimate)
    cost = {key: estimate[key] for key in ("approvedBudgetUsd", "upperBoundUsd", "maximumHours", "includesAutomation")}
    envelope = dict(schemaVersion=1, request=deepcopy(request), resourceChanges=deepcopy(changes), costEstimate=cost)
    def equivalent(existing, proposed):
        # Recover a crash after S3 writes but before the session CAS. Preserve
        # the original creation/expiry; retry cannot extend an approval window.
        if not isinstance(existing, dict) or set(existing) != set(proposed):
            return False
        prior_request = existing.get("request", {})
        validate_request(prior_request, now)
        if utc(prior_request["createdAt"]) > utc(proposed["request"]["createdAt"]):
            return False
        comparison = deepcopy(existing)
        comparison["request"]["createdAt"] = proposed["request"]["createdAt"]
        return comparison == proposed
    ref = immutable_json(client, bucket, review_key(request), envelope, equivalent=equivalent)
    stored, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    if not equivalent(stored, envelope):
        raise ValueError("Private review readback differs from the proposed plan")
    return dict(ref, sha256=checksum(stored), sessionId=request["sessionId"], stage=request["stage"], attempt=request["attempt"])


def load_review(client, bucket, ref, now):
    if not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256", "sessionId", "stage", "attempt"}:
        raise ValueError("An exact pending review reference is required")
    if (not isinstance(ref["sessionId"], str) or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", ref["sessionId"])
            or ref["stage"] not in ("foundation", "edge") or type(ref["attempt"]) is not int or ref["attempt"] < 1):
        raise ValueError("Invalid pending review identity")
    if ref["key"] != review_key(ref):
        raise ValueError("Review reference escaped its session/stage prefix")
    approval_key(ref, ref["sha256"])
    envelope, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    if not isinstance(envelope, dict) or set(envelope) != {"schemaVersion", "request", "resourceChanges", "costEstimate"} or envelope["schemaVersion"] != 1:
        raise ValueError("Unsupported review envelope")
    if checksum(envelope) != ref["sha256"]:
        raise ValueError("Review checksum differs from the pending request")
    request = envelope["request"]
    validate_request(request, now)
    validate_changes(envelope["resourceChanges"])
    cost = envelope["costEstimate"]
    validate_estimate(dict(request["context"], maximumHours=cost.get("maximumHours")), cost)
    if any(ref[key] != request[key] for key in ("sessionId", "stage", "attempt")) or request["context"]["backendBucket"] != bucket:
        raise ValueError("Review scope differs from the requested session")
    return envelope


def read_authorized_plan(client, bucket, review_ref, approval_ref, session, context, now):
    """Worker-side read path; inputs must be observed while holding its deploy lease.

    Object keys are derived, not accepted as arbitrary downloads. IAM/bucket
    policy separates approval writers from the planner/deployer/publisher.
    """
    envelope = load_review(client, bucket, review_ref, now)
    request = envelope["request"]
    expected = dict(sessionId=review_ref["sessionId"], stage=review_ref["stage"], attempt=review_ref["attempt"],
                    reviewSha256=review_ref["sha256"], key=approval_key(request, review_ref["sha256"]))
    if (not isinstance(approval_ref, dict) or set(approval_ref) != set(expected) | {"versionId"}
            or any(approval_ref[key] != value for key, value in expected.items())
            or session.get("pendingPlan") != review_ref):
        raise ValueError("Approval reference does not match the pending private review")
    approval, _ = read_json(client, bucket, expected["key"], approval_ref["versionId"])
    if approval.get("reviewReference") != review_ref:
        raise ValueError("Approval did not review this exact summary/version")
    validate_estimate(session, envelope["costEstimate"])
    plan = request["plan"]
    response = client.get_object(Bucket=bucket, Key=plan["key"], VersionId=plan["versionId"], ExpectedBucketOwner=ACCOUNT)
    if response.get("VersionId") != plan["versionId"]:
        raise ValueError("Saved plan read returned an unexpected version")
    payload = response["Body"].read(MAX_PLAN_BYTES + 1)
    permit = authorize_apply(session, request, approval, context, payload, plan_object_version=response["VersionId"], now=now)
    return payload, permit
