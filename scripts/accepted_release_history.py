"""Immutable accepted-release chain and the private parent's atomic READY payload.

Historical projections are read-only. They cannot admit a new worker or change
the original cold execution, network binding, or acceptance checkpoints.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re

from active_private_contract import read_active_private_receipt, validate_active_private_checkpoint
from automation_session import utc
from plan_review_store import checksum, immutable_json
from ready_release_contract import read_reference

MAX_RELEASES = 64


def history_key(record, release_id):
    if not re.fullmatch(r"[a-f0-9]{64}", release_id): raise ValueError("Exact accepted release identity required")
    return f"accepted/staging/{record['sessionId']}/{release_id}.json"


def projected(record, candidate, previous):
    result = deepcopy(record)
    result.update(status="TESTING", releaseCandidate=deepcopy(candidate), acceptedRelease=deepcopy(previous))
    result.pop("acceptedActive", None)
    result.pop("acceptedRollback", None)
    return result


def accepted_projection(record, task=None):
    snapshot = record.get("acceptedActive", {})
    if (record.get("status") not in ("READY", "TESTING") or record.get("acceptedRollback") or not isinstance(snapshot, dict)
            or set(snapshot) != {"candidate", "previousAccepted", "historyReference"}):
        raise ValueError("No accepted active release snapshot exists")
    historical = projected(record, snapshot["candidate"], snapshot["previousAccepted"])
    checkpoint = validate_active_private_checkpoint(historical, task)
    from active_release_contract import reference
    reference(snapshot["historyReference"], history_key(record, historical["releaseCandidate"]["id"]))
    if record.get("acceptedRelease") != dict(checkpoint["release"], acceptanceReference=snapshot["historyReference"]):
        raise ValueError("Accepted baseline differs from the exact active history checkpoint")
    return historical


def read_accepted_history(client, bucket, record, execution_version):
    rollback = bool(record.get("acceptedRollback"))
    if rollback:
        from accepted_rollback_history import accepted_projection as rollback_projection
        rollback_projection(record)
    else: accepted_projection(record)
    top = record["acceptedRollback" if rollback else "acceptedActive"]
    baseline = deepcopy(record["acceptedRelease"])
    seen, newer_at = set(), None
    for index in range(MAX_RELEASES):
        ref = baseline.get("acceptanceReference", {})
        prefix = f"accepted/staging/{record['sessionId']}/"
        key = ref.get("key", "")
        if not isinstance(key, str) or not key.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{64}\.json", key[len(prefix):]):
            raise ValueError("Accepted history chain has no exact scoped predecessor")
        if key in seen: raise ValueError("Accepted history cannot repeat a release")
        seen.add(key)
        value = read_reference(client, bucket, ref, key)
        kind = value.get("kind") if isinstance(value, dict) else None
        is_rollback = kind == "accepted-rollback-release"
        expected = dict(schemaVersion=1, kind=kind, sessionId=record["sessionId"], executionVersion=execution_version)
        if (not isinstance(value, dict) or kind not in ("accepted-active-release", "accepted-rollback-release")
                or type(value.get("schemaVersion")) is not int
                or set(value) != set(expected) | {"candidate", "previousAccepted", "release", "acceptedAt"} | ({"rollback"} if is_rollback else set())
                or any(value.get(name) != item for name, item in expected.items())
                or key != history_key(record, value.get("rollback", {}).get("intent", {}).get("rollbackId", "") if is_rollback else value.get("candidate", {}).get("id", ""))
                or baseline != dict(value["release"], acceptanceReference=ref)):
            raise ValueError("Accepted history differs from its exact baseline/version")
        if index == 0 and (is_rollback != rollback or any(value[name] != top[name] for name in ("candidate", "previousAccepted") + (("rollback",) if is_rollback else ()))):
            raise ValueError("Accepted snapshot differs from its immutable history")
        if is_rollback:
            from accepted_rollback_history import projected as rollback_projected
            from rollback_private_contract import read_private_receipt
            historical = rollback_projected(record, value["rollback"], value["candidate"], value["previousAccepted"])
            receipt = read_private_receipt(client, bucket, historical, execution_version)
        else:
            historical = projected(record, value["candidate"], value["previousAccepted"])
            receipt = read_active_private_receipt(client, bucket, historical, execution_version)
        accepted_at = utc(value["acceptedAt"])
        if (receipt["release"] != value["release"] or not utc(receipt["completedAt"]) <= accepted_at < utc(record["cleanupStartAt"])
                or is_rollback and accepted_at >= utc(value["rollback"]["intent"]["expiresAt"])
                or newer_at is not None and accepted_at > newer_at):
            raise ValueError("Accepted history does not prove the tested release in order")
        newer_at = accepted_at
        baseline = value["previousAccepted"]
        if not baseline.get("acceptanceReference", {}).get("key", "").startswith(prefix):
            from private_acceptance_contract import ready_release
            if baseline != ready_release(client, bucket, record, execution_version):
                raise ValueError("Accepted history is not rooted in the original cold acceptance")
            return deepcopy(record["acceptedRelease"])
    raise ValueError("Accepted history exceeds the bounded session release limit")


def finalize_active_release(client, bucket, record, execution_version, *, now=None):
    from ready_release_contract import accepted_baseline
    task = record.get("deploymentTask", {})
    if (record.get("status") != "TESTING" or record.get("lease", {}).get("operation") != "acceptance"
            or (task.get("mode"), task.get("stage"), task.get("state")) != ("verify", "active", "RUNNING")):
        raise ValueError("Only the terminal private task parent may publish its accepted baseline")
    validate_active_private_checkpoint(record, task)
    previous = accepted_baseline(client, bucket, record, execution_version)
    receipt = read_active_private_receipt(client, bucket, record, execution_version)
    now = now or datetime.now(timezone.utc)
    if not utc(receipt["completedAt"]) <= now < utc(record["cleanupStartAt"]):
        raise ValueError("Active acceptance cannot publish outside its window")
    value = dict(schemaVersion=1, kind="accepted-active-release", sessionId=record["sessionId"], executionVersion=execution_version,
        candidate=deepcopy(record["releaseCandidate"]), previousAccepted=previous, release=deepcopy(receipt["release"]), acceptedAt=now.isoformat())
    ref = immutable_json(client, bucket, history_key(record, value["candidate"]["id"]), value)
    ref["sha256"] = checksum(value)
    update = dict(acceptedRelease=dict(value["release"], acceptanceReference=ref),
        acceptedActive=dict(candidate=value["candidate"], previousAccepted=previous, historyReference=ref))
    completed = dict(record, **update, status="READY")
    completed.pop("releaseCandidate", None)
    completed.pop("acceptedRollback", None)
    read_accepted_history(client, bucket, completed, execution_version)
    return update
