"""Private saved-plan/review wire contracts for the EC2/SSM backend.

This module cannot issue approvals or authenticate GitHub payloads. The trusted
review bridge verifies the actual API history and has exclusive write access
to the approval prefix. Workers fetch exact private versions and regenerate the
current context; runtime CAS/lease and cleanup-readiness checks remain mandatory.
"""
from copy import deepcopy
import hashlib

from execution_v2 import (BUCKET, PREFIX, decode_object, fields, integer, reference, text,
                          timestamp, validate_session)
from recovery_v2 import checksum

KMS_KEY = "arn:aws:kms:us-east-1:058114477594:key/ed23fbd2-8c91-4d93-a8f0-6835e84b7ec7"


def validate_context(context, execution, execution_ref, worker):
    fields(context, {"sourceRevisions", "approvedBudgetUsd", "cleanupStartAt", "deadline", "workerIdentitySha256",
                     "configurationReference", "backendBucket", "backendKey", "kmsKeyArn", "terraformVersion",
                     "configSha256", "providerLockSha256", "stateReference", "stateLineage", "stateSerial"})
    if (not isinstance(worker, dict) or worker.get("kind") != "ec2-worker" or worker.get("purpose") != "deployment"
            or worker.get("executionReference") != execution_ref
            or context["workerIdentitySha256"] != checksum(worker)
            or checksum(context["configurationReference"]) != checksum(worker.get("configurationReference"))):
        raise ValueError("Plan requires the exact registered deployment worker/configuration")
    for key in ("sourceRevisions", "approvedBudgetUsd", "cleanupStartAt", "deadline"):
        if checksum(context[key]) != checksum(execution[key]):
            raise ValueError("Plan changed the immutable source, budget or window")
    if (context["backendBucket"] != BUCKET or context["backendKey"] != "staging/terraform.tfstate"
            or context["kmsKeyArn"] != KMS_KEY or context["terraformVersion"] != "1.15.5"):
        raise ValueError("Plan backend, encryption or toolchain differs from the reviewed configuration")
    reference(context["configurationReference"], key=f"{PREFIX}config/deployment.json")
    reference(context["stateReference"], key="staging/terraform.tfstate")
    for key in ("configSha256", "providerLockSha256"):
        text(context[key], r"[a-f0-9]{64}")
    text(context["stateLineage"], r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
    integer(context["stateSerial"], minimum=0)
    return deepcopy(context)


def validate_request(body, request_ref, execution, execution_ref, session, worker, current_context, *, now):
    validate_session(session, execution, execution_ref)
    if session["status"] not in ("PLANNING", "PROVISIONING") or now >= timestamp(execution["cleanupStartAt"]):
        raise ValueError("Session is closed for plan review or apply")
    validate_context(current_context, execution, execution_ref, worker)
    # Key selection needs the stage/attempt. Parse only after validating the
    # exact private prefix, then enforce that the body chooses the same key.
    prefix = f"{PREFIX}reviews/{execution['sessionId']}/"
    key = request_ref.get("key") if isinstance(request_ref, dict) else None
    text(key, prefix + r"(?:foundation|edge)/[1-9][0-9]{0,5}/request\.json")
    request = decode_object(body, request_ref, key=key)
    fields(request, {"schemaVersion", "sessionId", "executionReference", "leaseEpoch", "stage", "attempt",
                     "planReference", "context", "createdAt", "expiresAt"})
    integer(request["attempt"], maximum=999999)
    integer(request["leaseEpoch"])
    if (type(request["schemaVersion"]) is not int or request["schemaVersion"] != 2
            or request["sessionId"] != execution["sessionId"] or request["executionReference"] != execution_ref
            or request["leaseEpoch"] != session["leaseEpoch"] or request["stage"] not in ("foundation", "edge")
            or key != f"{prefix}{request['stage']}/{request['attempt']}/request.json"
            or checksum(request["context"]) != checksum(current_context)):
        raise ValueError("Review request changed its execution, fence, stage or current plan context")
    reference(request["planReference"], key=f"{PREFIX}plans/{execution['sessionId']}/{request['stage']}/{request['attempt']}/saved.tfplan")
    created, expires = timestamp(request["createdAt"]), timestamp(request["expiresAt"])
    if not timestamp(execution["startedAt"]) <= created <= now < expires <= timestamp(execution["cleanupStartAt"]):
        raise ValueError("Review request is outside its admitted window")
    return request


def validate_review(review, execution, *, created_at, now):
    """Normalized, independently verified GitHub history from the trusted bridge."""
    fields(review, {"repositoryId", "ownerId", "workflow", "runId", "runAttempt", "headSha", "environment", "environmentId",
                    "reviewerId", "reviewerLogin", "reviewerType", "runCreatedAt", "verifiedAt"})
    if (review["repositoryId"] != "1322975868" or review["ownerId"] != "328679702"
            or review["workflow"] != "staging-plan-review.yml" or review["environment"] != "staging-apply"
            or review["headSha"] != execution["sourceRevisions"]["techx-infra"] or review["reviewerType"] != "User"
            or type(review["runAttempt"]) is not int or review["runAttempt"] != 1):
        raise ValueError("Approval requires the exact trusted main review workflow and a human reviewer")
    text(review["runId"], r"[1-9][0-9]{0,19}")
    text(review["reviewerLogin"], r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
    integer(review["environmentId"])
    integer(review["reviewerId"])
    if not created_at <= timestamp(review["runCreatedAt"]) <= timestamp(review["verifiedAt"]) <= now:
        raise ValueError("Review predates the request or is not yet verified")
    return deepcopy(review)


def validate_apply(request_body, request_ref, approval_body, approval_ref, plan_bytes, downloaded_plan_ref,
                   execution, execution_ref, session, worker, current_context, trusted_review, *, now):
    request = validate_request(request_body, request_ref, execution, execution_ref, session, worker, current_context, now=now)
    approval = decode_object(approval_body, approval_ref,
                             key=f"{PREFIX}approvals/{execution['sessionId']}/{request_ref['sha256']}.json")
    fields(approval, {"schemaVersion", "requestReference", "review", "approvedAt", "expiresAt"})
    validate_review(trusted_review, execution, created_at=timestamp(request["createdAt"]), now=now)
    validate_review(approval["review"], execution, created_at=timestamp(request["createdAt"]), now=now)
    # Re-querying history can yield a later verification time; it must not
    # change any stable run/environment/reviewer identity of the original grant.
    stable_review = lambda r: {k: v for k, v in r.items() if k != "verifiedAt"}
    if (type(approval["schemaVersion"]) is not int or approval["schemaVersion"] != 2
            or checksum(approval["requestReference"]) != checksum(request_ref)
            or checksum(stable_review(approval["review"])) != checksum(stable_review(trusted_review))
            or timestamp(approval["review"]["verifiedAt"]) > timestamp(trusted_review["verifiedAt"])):
        raise ValueError("Approval is not for this exact request and independently verified review")
    approved, expires = timestamp(approval["approvedAt"]), timestamp(approval["expiresAt"])
    if not timestamp(approval["review"]["verifiedAt"]) <= approved <= now < expires <= timestamp(request["expiresAt"]):
        raise ValueError("Approval timestamp is invalid or expired")
    reference(downloaded_plan_ref, key=request["planReference"]["key"])
    if (checksum(downloaded_plan_ref) != checksum(request["planReference"])
            or not isinstance(plan_bytes, bytes) or not 0 < len(plan_bytes) <= 64 * 1024 * 1024
            or hashlib.sha256(plan_bytes).hexdigest() != request["planReference"]["sha256"]):
        raise ValueError("Downloaded saved plan bytes differ from the reviewed exact version")
    # Downloading the plan version, CAS consumption, current worker service/boot
    # observation, live state/config capture and readiness are runtime duties.
    return dict(requestReference=deepcopy(request_ref), approvalReference=deepcopy(approval_ref),
                planReference=deepcopy(request["planReference"]), leaseEpoch=session["leaseEpoch"],
                workerIdentitySha256=checksum(worker))
