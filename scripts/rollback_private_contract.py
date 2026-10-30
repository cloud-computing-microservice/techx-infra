"""Private runtime proof for one restored revision, before the READY handoff."""
from copy import deepcopy
import re

from active_release_contract import reference
from automation_session import utc
from edge_activation import read_activation_receipt
from gitops_bootstrap import COMPONENTS
from plan_review_store import checksum
from private_acceptance_contract import CHECKS, validate_private_result
from ready_release_contract import read_reference, validate_manifest
from rollback_public_contract import network_record, public_context, read_public_receipt, validate_public_checkpoint
from rollback_rollout_contract import read_rollback_rollout
from rollback_task_contract import slot


def private_context(record):
    validate_public_checkpoint(record)
    ledger = slot(record)
    if "restoration" not in ledger:
        raise ValueError("Rollback private checks require the consumed terminal public handoff")
    return dict(public_context(record), publicAcceptance=deepcopy(ledger["publicAcceptance"]),
        restorationSha256=checksum(ledger["restoration"]))


def restoration_record(record):
    """Read-only terminal predecessor; never grants a new restore task."""
    private_context(record)
    result = deepcopy(record)
    result.update(deepcopy(slot(record)["restoration"]))
    result.pop("lease", None)
    return result


def validate_private_start(record, attempt):
    ledger = slot(record)
    if (type(attempt) is not int or attempt != 1 or ledger.get("privateAcceptance")
            or record.get("deploymentTask") != ledger.get("restoration", {}).get("deploymentTask")
            or record.get("workflowHandoff") != ledger.get("restoration", {}).get("workflowHandoff")):
        raise ValueError("Only one rollback private task may consume the restored public predecessor")
    return checksum(private_context(record))


def result_key(record, task_id):
    slot(record)
    if not re.fullmatch(r"[a-f0-9]{32}", task_id): raise ValueError("Exact rollback private task identity required")
    return f"private/staging/{record['sessionId']}/rollback/acceptance/{task_id}.json"


def validate_private_checkpoint(record, task=None):
    stage = slot(record).get("privateAcceptance", {})
    if (stage.get("state") != "PASSED" or type(stage.get("attempt")) is not int or stage["attempt"] != 1
            or stage.get("contextSha256") != checksum(private_context(record))
            or not re.fullmatch(r"[a-f0-9]{32}", stage.get("taskId", ""))
            or task is not None and (task.get("taskId") != stage["taskId"] or task.get("expectedAcceptanceSha256") != stage["contextSha256"])):
        raise ValueError("Rollback private acceptance lacks its exact restored public/task checkpoint")
    reference(stage.get("receiptReference"), result_key(record, stage["taskId"]))
    return stage


def restored_manifest(client, bucket, record, version):
    rollout = read_rollback_rollout(client, bucket, record, version)
    prefix = f"promotions/staging/{record['sessionId']}/{slot(record)['approvalReference']['sha256']}"
    merge = read_reference(client, bucket, rollout["mergeReceiptReference"], prefix + "/receipt.json")
    intent = read_reference(client, bucket, merge["intentReference"], prefix + "/intent.json")
    manifest = validate_manifest(intent["request"].get("manifest"), slot(record)["intent"]["acceptedBaseline"]["platformSha"])
    images = {COMPONENTS[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in manifest["images"]}
    if images != rollout["images"]:
        raise ValueError("Restored manifest does not identify the accepted runtime images")
    return dict(manifest=manifest)


def read_private_receipt(client, bucket, record, version):
    stage, ledger = validate_private_checkpoint(record), slot(record)
    value = read_reference(client, bucket, stage["receiptReference"], result_key(record, stage["taskId"]))
    expected = dict(schemaVersion=1, kind="rollback-private-acceptance", sessionId=record["sessionId"], executionVersion=version,
        taskId=stage["taskId"], contextSha256=stage["contextSha256"], rollbackId=ledger["intent"]["rollbackId"],
        publicReference=ledger["publicAcceptance"]["receiptReference"], rolloutReference=ledger["rolloutReference"], checks=sorted(CHECKS))
    if (not isinstance(value, dict) or type(value.get("schemaVersion")) is not int
            or set(value) != set(expected) | {"stateSha256", "vpn", "privateObservations", "completedAt", "release"}
            or any(value.get(name) != item for name, item in expected.items()) or value.get("release") != stage.get("release")
            or not utc(ledger["intent"]["createdAt"]) <= utc(value.get("completedAt", "")) < utc(ledger["intent"]["expiresAt"])):
        raise ValueError("Rollback private receipt differs from its original restored public/runtime proof")
    rollout = read_rollback_rollout(client, bucket, record, version)
    read_public_receipt(client, bucket, record, version)
    validate_private_result(value, record, ledger["intent"]["acceptedBaseline"]["platformSha"], rollout["chartSha"])
    if value["release"]["images"] != rollout["images"]:
        raise ValueError("Rollback private runtime images differ from the pinned restoration")
    activation = read_activation_receipt(client, bucket, network_record(record), version)
    if value["stateSha256"] != activation["stateSha256"] or value["vpn"].get("endpointId") != activation["resources"]["endpointId"]:
        raise ValueError("Rollback private checks used a different edge or VPN endpoint")
    return value
