"""Private acceptance of one active public/rollout predecessor in TESTING."""
from copy import deepcopy
import re

from active_public_contract import active_public_context, validate_active_public_checkpoint, read_active_public_receipt
from active_release_contract import reference, read_rollout_receipt
from plan_review_store import checksum, read_json
from private_acceptance_contract import CHECKS, evidence_record, validate_private_result


def active_private_context(record):
    validate_active_public_checkpoint(record)
    return dict(active_public_context(record), publicAcceptance=deepcopy(record["releaseCandidate"]["publicAcceptance"]))


def validate_active_private_start(record, attempt):
    if attempt != 1 or record.get("releaseCandidate", {}).get("privateAcceptance"):
        raise ValueError("Only one unconsumed active private acceptance task is permitted")
    return checksum(active_private_context(record))


def private_result_key(record, task_id):
    if not re.fullmatch(r"[a-f0-9]{32}", task_id): raise ValueError("Exact private acceptance task required")
    return f"private/staging/{record['sessionId']}/active/{record['releaseCandidate']['id']}/acceptance/{task_id}.json"


def validate_active_private_checkpoint(record, task=None):
    stage = record.get("releaseCandidate", {}).get("privateAcceptance", {})
    if (stage.get("state") != "PASSED" or stage.get("attempt") != 1
            or stage.get("contextSha256") != checksum(active_private_context(record))
            or not re.fullmatch(r"[a-f0-9]{32}", stage.get("taskId", ""))
            or task is not None and (task.get("taskId") != stage["taskId"] or task.get("expectedAcceptanceSha256") != stage["contextSha256"])):
        raise ValueError("Active private acceptance lacks its exact public/rollout/task checkpoint")
    reference(stage.get("receiptReference"), private_result_key(record, stage["taskId"]))
    return stage


def read_active_private_receipt(client, bucket, record, execution_version):
    stage = validate_active_private_checkpoint(record)
    ref = stage["receiptReference"]
    value, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    pointer = record["releaseCandidate"]
    expected = dict(schemaVersion=1, kind="active-private-acceptance", sessionId=record["sessionId"], executionVersion=execution_version,
        taskId=stage["taskId"], contextSha256=stage["contextSha256"], releaseId=pointer["id"],
        publicReference=pointer["publicAcceptance"]["receiptReference"], rolloutReference=pointer["rollout"]["receiptReference"], checks=sorted(CHECKS))
    if (not isinstance(value, dict) or set(value) != set(expected) | {"stateSha256", "vpn", "privateObservations", "completedAt", "release"}
            or checksum(value) != ref["sha256"] or any(value.get(key) != item for key, item in expected.items())
            or value.get("release") != stage.get("release")):
        raise ValueError("Active private receipt differs from its exact tested candidate")
    rollout = read_rollout_receipt(client, bucket, record, execution_version)
    read_active_public_receipt(client, bucket, record, execution_version)
    validate_private_result(value, record, pointer["sourceSha"], rollout["chartSha"])
    if value["release"]["images"] != rollout["images"]:
        raise ValueError("Active private runtime images differ from the pinned rollout")
    from edge_activation import read_activation_receipt
    activation = read_activation_receipt(client, bucket, evidence_record(record), execution_version)
    if value["stateSha256"] != activation["stateSha256"] or value["vpn"].get("endpointId") != activation["resources"]["endpointId"]:
        raise ValueError("Active private acceptance used a different edge/VPN endpoint")
    return value
