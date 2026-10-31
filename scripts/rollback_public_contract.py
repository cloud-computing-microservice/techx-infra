"""Public acceptance of the restored revision; old failed/cold proof is not reused."""
from copy import deepcopy
import re

from active_release_contract import reference
from automation_session import utc
from plan_review_store import checksum
from public_acceptance_contract import REPO, validate_probe_result
from ready_release_contract import read_reference
from rollback_rollout_contract import read_rollback_rollout
from rollback_task_contract import rollout_context, slot, validate_rollout_checkpoint


def network_record(record):
    # Read-only original edge binding. It cannot admit a deployment worker or
    # treat the old cold acceptance as this restored release's runtime proof.
    slot(record)
    return dict(record, status="PROVISIONING")


def public_context(record):
    value = validate_rollout_checkpoint(record)
    return dict(rollbackContext=rollout_context(record), rollout=dict(taskId=value["rolloutTaskId"], receiptReference=deepcopy(value["rolloutReference"])))


def result_key(record, dispatch):
    value = slot(record)
    if not re.fullmatch(r"[a-f0-9]{64}", dispatch): raise ValueError("Exact rollback public dispatch identity required")
    return f"acceptance/staging/{record['sessionId']}/rollback/{value['intent']['rollbackId']}/public/{dispatch}/result.json"


def validate_public_checkpoint(record):
    value = slot(record)
    stage = value.get("publicAcceptance", {})
    if (stage.get("state") != "PASSED" or type(stage.get("attempt")) is not int or stage["attempt"] != 1
            or stage.get("contextSha256") != checksum(public_context(record))
            or not re.fullmatch(r"[a-f0-9]{64}", stage.get("dispatchId", ""))
            or not re.fullmatch(rf"https://github.com/{REPO}/actions/runs/[1-9][0-9]*", stage.get("workflowRun", ""))):
        raise ValueError("Rollback public checks lack their exact restored-revision checkpoint")
    reference(stage.get("receiptReference"), result_key(record, stage["dispatchId"]))
    return stage


def read_public_receipt(client, bucket, record, version):
    stage = validate_public_checkpoint(record)
    ledger = slot(record)
    value = read_reference(client, bucket, stage["receiptReference"], result_key(record, stage["dispatchId"]))
    expected = dict(schemaVersion=1, kind="rollback-public-acceptance", sessionId=record["sessionId"], executionVersion=version,
        contextSha256=stage["contextSha256"], dispatchId=stage["dispatchId"], workflowRun=stage["workflowRun"],
        rollbackId=ledger["intent"]["rollbackId"], rolloutReference=ledger["rolloutReference"])
    if (not isinstance(value, dict) or type(value.get("schemaVersion")) is not int
            or set(value) != set(expected) | {"runner", "result", "completedAt"}
            or any(value.get(name) != item for name, item in expected.items())
            or not utc(ledger["intent"]["createdAt"]) <= utc(value.get("completedAt", "")) < utc(ledger["intent"]["expiresAt"])):
        raise ValueError("Rollback public receipt differs from its original restored rollout")
    read_rollback_rollout(client, bucket, record, version)
    validate_probe_result(value["runner"], value["result"])
    return value
