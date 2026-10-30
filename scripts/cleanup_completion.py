"""Verify cleanup evidence and recover a missing marker without workload calls."""
from datetime import datetime, timezone
import hashlib
import json
import re

from automation_session import utc, validate
from cleanup_execution import ACCOUNT, assert_inventory
from plan_approval import version
from plan_review_store import immutable_json, read_json

CONTEXT = ("sessionId", "account", "region", "environment", "sourceRevisions", "createdAt", "deadline",
           "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action")


def checkpoint_inventory(s3, bucket, execution, execution_version, current, *, inventory_version=None, metadata=False):
    validate(current); validate(execution); version(execution_version)
    if (current.get("status") != "DESTROYED" or current.get("lease") or current.get("executionVersion") != execution_version
            or any(current.get(key) != execution.get(key) for key in CONTEXT)):
        raise ValueError("Completion evidence requires the same idle DESTROYED execution checkpoint")
    evidence = current.get("cleanupEvidence", {})
    digest = evidence.get("artifactSha256", "")
    key = f"cleanup/{execution['sessionId']}/inventory-{digest}.json"
    if (not isinstance(evidence, dict) or set(evidence) != {"inventoryEmpty", "artifactSha256", "key"}
            or evidence.get("inventoryEmpty") is not True or not re.fullmatch(r"[a-f0-9]{64}", digest) or evidence.get("key") != key):
        raise ValueError("DESTROYED requires the exact content-addressed cleanup inventory checkpoint")
    args = dict(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
    if inventory_version is not None:
        version(inventory_version); args["VersionId"] = inventory_version
    response = s3.get_object(**args)
    version(response.get("VersionId"))
    if inventory_version is not None and response["VersionId"] != inventory_version:
        raise ValueError("Cleanup inventory read returned a different pinned version")
    payload = response["Body"].read(256 * 1024 + 1)
    if len(payload) > 256 * 1024 or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Cleanup inventory bytes do not match the protected checkpoint")
    assert_inventory(json.loads(payload))
    modified = response.get("LastModified")
    if metadata and (not isinstance(modified, datetime) or modified.tzinfo is None
            or not utc(execution["createdAt"]) <= modified <= datetime.now(timezone.utc)):
        raise ValueError("Marker recovery requires the original inventory's service timestamp")
    return dict(key=key, versionId=response["VersionId"], sha256=digest, modifiedAt=modified)


def completion(s3, bucket, execution, execution_version, current):
    key = f"cleanup/{execution['sessionId']}/complete.json"
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=2, ExpectedBucketOwner=ACCOUNT)
    rows = listed.get("Contents", [])
    if (listed.get("IsTruncated") is not False or not isinstance(rows, list)
            or len(rows) > 1 or any(row.get("Key") != key for row in rows)):
        raise ValueError("Cleanup marker listing is incomplete or ambiguous")
    if not rows: return None
    marker, marker_version = read_json(s3, bucket, key)
    version(marker_version)
    if (not isinstance(marker, dict) or marker.get("executionVersion") != execution_version or marker.get("inventoryEmpty") is not True
            or not re.fullmatch(r"[a-f0-9]{64}", marker.get("artifactSha256", ""))
            or not utc(execution["createdAt"]) <= utc(marker.get("checkedAt", "")) <= datetime.now(timezone.utc)):
        raise ValueError("Cleanup completion marker differs from this execution")
    pinned = marker.get("inventoryVersionId")
    if "inventoryVersionId" in marker: version(pinned)
    proof = checkpoint_inventory(s3, bucket, execution, execution_version, current, inventory_version=pinned, metadata=pinned is not None)
    if marker["artifactSha256"] != proof["sha256"] or marker.get("inventoryKey") != proof["key"]:
        raise ValueError("Cleanup marker differs from the DESTROYED inventory checkpoint")
    if pinned is not None and utc(marker["checkedAt"]) != proof["modifiedAt"]:
        raise ValueError("Pinned completion time differs from its original inventory service timestamp")
    return dict(sessionId=execution["sessionId"], executionVersion=execution_version, status="DESTROYED", inventoryEmpty=True,
        markerKey=key, markerVersion=marker_version, inventoryKey=proof["key"], inventoryVersionId=proof["versionId"],
        inventorySha256=proof["sha256"], checkedAt=marker["checkedAt"])


def publish_completion(s3, bucket, execution, execution_version, store):
    """Cleanup-only producer; no teardown, lease takeover, or fresh cloud claim.

    The project serializes cleanup builds. A repair only writes the old session's
    immutable marker after validating its already committed inventory; it never
    reopens DESTROYED or relaxes the cleanup lease's active-session gate.
    """
    current, etag = store.read()
    existing = completion(s3, bucket, execution, execution_version, current)
    if existing: return existing
    proof = checkpoint_inventory(s3, bucket, execution, execution_version, current, metadata=True)
    latest, latest_etag = store.read()
    if latest != current or latest_etag != etag:
        raise ValueError("Session changed before cleanup marker recovery")
    marker = dict(executionVersion=execution_version, inventoryEmpty=True, artifactSha256=proof["sha256"],
        inventoryKey=proof["key"], inventoryVersionId=proof["versionId"], checkedAt=proof["modifiedAt"].isoformat())
    immutable_json(s3, bucket, f"cleanup/{execution['sessionId']}/complete.json", marker)
    latest, latest_etag = store.read()
    if latest != current or latest_etag != etag:
        raise ValueError("Session changed during cleanup marker recovery; newer session was not modified")
    result = completion(s3, bucket, execution, execution_version, latest)
    if result is None: raise ValueError("Cleanup marker write lacks positive readback evidence")
    return result
