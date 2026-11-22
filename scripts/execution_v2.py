"""Offline, fail-closed wire contracts for the EC2/SSM backend.

Validation is not authorization. References must be fetched through the scoped
AWS reader; approvals, migration attestation and live CAS fencing are separate
admission gates. No v1 record is upgraded or written by this module.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

ACCOUNT = "058114477594"
REGION = "us-east-1"
BUCKET = f"techx-automation-{ACCOUNT}-{REGION}"
PREFIX = "automation/v2/staging/"
SOURCES = {"techx-infra", "techx-platform", "techx-chart"}
STATUSES = {"PLANNING", "PROVISIONING", "READY", "TESTING", "FAILED_NEEDS_CLEANUP", "DESTROYING", "DESTROYED"}


def fields(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError("Missing or unexpected contract fields")


def text(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("Invalid contract identifier")
    return value


def integer(value, minimum=1, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid bounded integer")
    return value


def timestamp(value):
    # A canonical UTC wire format avoids ambiguous/local and naive deadlines.
    text(value, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError("Invalid UTC timestamp") from error


def version(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 1024
            or value.lower() in ("null", "latest") or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("An exact non-null S3 VersionId is required")


def session_id(value):
    return text(value, r"staging-v2-[a-z0-9-]{1,60}")


def reference(value, *, key):
    fields(value, {"bucket", "key", "versionId", "sha256"})
    if value["bucket"] != BUCKET or value["key"] != key:
        raise ValueError("Object reference is outside its exact v2 scope")
    version(value["versionId"])
    text(value["sha256"], r"[a-f0-9]{64}")
    return deepcopy(value)


def execution_reference(value, session):
    session_id(session)
    return reference(value, key=f"{PREFIX}executions/{session}.json")


def window_reference(value, window_id):
    text(window_id, r"window-gh-[1-9][0-9]{0,19}")
    return reference(value, key=f"automation/v2/windows/{window_id}.json")


def cost_window(value):
    """Shared format/limit check; not an authorization or cost-accounting gate."""
    text(value["approvedBudgetUsd"], r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?")
    try:
        budget = Decimal(value["approvedBudgetUsd"])
    except InvalidOperation as error:
        raise ValueError("Invalid approved budget") from error
    if not 0 < budget <= 60:
        raise ValueError("Budget must be positive and no more than USD 60")
    start, cleanup, end = (timestamp(value[k]) for k in ("startedAt", "cleanupStartAt", "deadline"))
    if not start < cleanup < end or end - start > timedelta(hours=10) or end - cleanup < timedelta(hours=2):
        raise ValueError("Window exceeds ten hours or lacks the two-hour cleanup reserve")
    return start, cleanup, end


def validate_execution(value):
    fields(value, {"schemaVersion", "executorBackend", "account", "region", "environment", "sessionId",
                   "sourceRevisions", "approvedBudgetUsd", "startedAt", "cleanupStartAt", "deadline", "initialEpoch",
                   "windowId", "authorizationReference"})
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2
            or value["executorBackend"] != "ec2-ssm" or value["account"] != ACCOUNT
            or value["region"] != REGION or value["environment"] != "staging"):
        raise ValueError("Invalid v2 backend or staging scope")
    session_id(value["sessionId"])
    fields(value["sourceRevisions"], SOURCES)
    for sha in value["sourceRevisions"].values():
        text(sha, r"[a-f0-9]{40}")
    window_reference(value["authorizationReference"], value["windowId"])
    cost_window(value)
    if integer(value["initialEpoch"]) != 1:
        raise ValueError("New execution must start at epoch one")
    return deepcopy(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Nonfinite JSON constant: {value}")


def decode_object(body, ref, *, key, max_bytes=32768):
    """Validate a private exact version's raw bytes before parsing JSON."""
    reference(ref, key=key)
    if not isinstance(body, bytes) or not 0 < len(body) <= max_bytes:
        raise ValueError("Private object must be bounded raw bytes")
    if hashlib.sha256(body).hexdigest() != ref["sha256"]:
        raise ValueError("Private object checksum mismatch")
    try:
        record = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid private object JSON") from error
    if not isinstance(record, dict):
        raise ValueError("Private object must be a JSON object")
    return record


def decode_execution(body, ref, session):
    """Validate the exact version's raw bytes, not a reserialized JSON hash."""
    execution_reference(ref, session)
    record = decode_object(body, ref, key=ref["key"])
    validate_execution(record)
    if record["sessionId"] != session:
        raise ValueError("Execution belongs to another session")
    return record


def bind_authorized_window(value, body, ref, *, purpose):
    """Bind a sub-execution to one protected, human-reviewed cloud window.

    The trusted admission bridge must authenticate the real review, fetch the
    exact object and account for aggregate migration/build/staging/recovery cost.
    This parser cannot grant permission or allocate a second budget per phase.
    Cleanup may read an expired window; runtime creation gates check live time.
    """
    window_reference(ref, value["windowId"])
    if value["authorizationReference"] != ref:
        raise ValueError("Execution changed its window authorization version")
    grant = decode_object(body, ref, key=ref["key"])
    fields(grant, {"schemaVersion", "windowId", "account", "region", "sourceRevisions", "approvedBudgetUsd",
                   "startedAt", "cleanupStartAt", "deadline", "allowedPurposes", "reviewReference"})
    if (type(grant["schemaVersion"]) is not int or grant["schemaVersion"] != 2 or grant["windowId"] != value["windowId"]
            or grant["account"] != ACCOUNT or grant["region"] != REGION or grant["sourceRevisions"] != value["sourceRevisions"]
            or grant["approvedBudgetUsd"] != value["approvedBudgetUsd"]):
        raise ValueError("Authorization changed scope, frozen revisions or shared budget")
    fields(grant["sourceRevisions"], SOURCES)
    for sha in grant["sourceRevisions"].values(): text(sha, r"[a-f0-9]{40}")
    start, cleanup, end = cost_window(grant)
    sub_start, sub_cleanup, sub_end = cost_window(value)
    allowed = grant["allowedPurposes"]
    if (not isinstance(allowed, list) or not allowed or any(p not in ("legacy-migration", "staging") for p in allowed)
            or len(set(allowed)) != len(allowed) or purpose not in ("legacy-migration", "staging") or purpose not in allowed):
        raise ValueError("This operation is outside the approved window purpose")
    if not start <= sub_start < sub_cleanup <= cleanup or sub_end > end:
        raise ValueError("Execution extends the approved cloud window")
    reference(grant["reviewReference"], key=f"automation/v2/windows/{grant['windowId']}/review.json")
    return grant


def validate_session(value, execution, ref):
    """Minimal session envelope. Future stage state requires its own contracts."""
    validate_execution(execution)
    fields(value, {"schemaVersion", "executorBackend", "sessionId", "executionReference", "status", "leaseEpoch"})
    execution_reference(ref, execution["sessionId"])
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2
            or value["executorBackend"] != "ec2-ssm" or value["sessionId"] != execution["sessionId"]
            or value["executionReference"] != ref or value["status"] not in STATUSES):
        raise ValueError("Session does not bind the exact v2 execution")
    integer(value["leaseEpoch"])
    return deepcopy(value)


def read_legacy(body):
    """Read an archival v1 record without promoting it to v2 or mutating it."""
    if not isinstance(body, bytes) or not 0 < len(body) <= 1024 * 1024:
        raise ValueError("Legacy snapshot must be bounded raw bytes")
    try:
        record = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid legacy snapshot JSON") from error
    from automation_session import validate
    validate(record)
    return deepcopy(record)
