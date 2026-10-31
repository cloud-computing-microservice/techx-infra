"""Versioned rollback history and the terminal private parent's READY payload."""
from copy import deepcopy
from datetime import datetime, timezone

from active_release_contract import reference
from automation_session import utc
from plan_review_store import checksum, immutable_json
from rollback_private_contract import read_private_receipt, validate_private_checkpoint
from rollback_task_contract import slot


def projected(record, ledger, candidate, previous):
    result = deepcopy(record)
    result.update(status="FAILED_NEEDS_CLEANUP", rollbackAttempt=deepcopy(ledger),
        releaseCandidate=deepcopy(candidate), acceptedRelease=deepcopy(previous))
    result.pop("acceptedActive", None)
    result.pop("acceptedRollback", None)
    return result


def accepted_projection(record, task=None):
    from accepted_release_history import history_key
    snapshot = record.get("acceptedRollback", {})
    if (record.get("status") not in ("READY", "TESTING") or record.get("acceptedActive")
            or not isinstance(snapshot, dict) or set(snapshot) != {"rollback", "candidate", "previousAccepted", "historyReference"}):
        raise ValueError("No exact accepted rollback snapshot exists")
    historical = projected(record, snapshot["rollback"], snapshot["candidate"], snapshot["previousAccepted"])
    checkpoint = validate_private_checkpoint(historical, task)
    reference(snapshot["historyReference"], history_key(record, slot(historical)["intent"]["rollbackId"]))
    if (record.get("rollbackAttempt") != snapshot["rollback"]
            or record.get("acceptedRelease") != dict(checkpoint["release"], acceptanceReference=snapshot["historyReference"])):
        raise ValueError("Accepted rollback differs from its exact single-use history checkpoint")
    return historical


def finalize_rollback(client, bucket, record, execution_version, *, now=None):
    from accepted_release_history import history_key, read_accepted_history
    from ready_release_contract import accepted_baseline
    task = record.get("deploymentTask", {})
    if (record.get("status") != "FAILED_NEEDS_CLEANUP" or record.get("lease", {}).get("operation") != "rollback"
            or (task.get("mode"), task.get("stage"), task.get("state")) != ("verify", "rollback", "RUNNING")):
        raise ValueError("Only the terminal rollback private parent may publish READY")
    validate_private_checkpoint(record, task)
    previous = accepted_baseline(client, bucket, dict(record, status="TESTING"), execution_version)
    receipt = read_private_receipt(client, bucket, record, execution_version)
    now = now or datetime.now(timezone.utc)
    if not utc(receipt["completedAt"]) <= now < utc(slot(record)["intent"]["expiresAt"]):
        raise ValueError("Rollback READY handoff is outside its original intent window")
    ledger = deepcopy(slot(record))
    value = dict(schemaVersion=1, kind="accepted-rollback-release", sessionId=record["sessionId"], executionVersion=execution_version,
        rollback=ledger, candidate=deepcopy(record["releaseCandidate"]), previousAccepted=previous,
        release=deepcopy(receipt["release"]), acceptedAt=now.isoformat())
    ref = immutable_json(client, bucket, history_key(record, ledger["intent"]["rollbackId"]), value)
    ref["sha256"] = checksum(value)
    update = dict(acceptedRelease=dict(value["release"], acceptanceReference=ref),
        acceptedRollback=dict(rollback=ledger, candidate=value["candidate"], previousAccepted=previous, historyReference=ref))
    completed = dict(record, **update, status="READY")
    completed.pop("releaseCandidate", None)
    completed.pop("acceptedActive", None)
    read_accepted_history(client, bucket, completed, execution_version)
    return update
