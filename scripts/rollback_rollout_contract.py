"""Versioned restored-revision proof; this is not rollback runtime acceptance."""
from copy import deepcopy
import re

from active_release_contract import read_rollout_receipt
from automation_session import utc
from plan_review_store import checksum
from ready_release_contract import read_reference
from rollback_task_contract import key, original_record, read_proposal_receipt, rollout_context, validate_rollout_checkpoint


def failed_rollout(client, bucket, record, version):
    return read_rollout_receipt(client, bucket, dict(original_record(record), status="TESTING"), version)


def read_rollback_rollout(client, bucket, record, version):
    ledger = validate_rollout_checkpoint(record)
    context = rollout_context(record)
    value = read_reference(client, bucket, ledger["rolloutReference"], key(record, "rollout"))
    expected = dict(schemaVersion=1, kind="rollback-rollout", sessionId=record["sessionId"], executionVersion=version,
        rollbackContext=context, taskId=ledger["rolloutTaskId"])
    if (not isinstance(value, dict) or set(value) != set(expected) | {"chartSha", "mergeReceiptReference", "chartCi", "application", "images", "completedAt"}
            or any(value.get(name) != item for name, item in expected.items())
            or not re.fullmatch(r"[a-f0-9]{40}", value.get("chartSha", ""))
            or value["chartSha"] in (ledger["intent"]["failedChartSha"], ledger["intent"]["acceptedBaseline"]["chartSha"])
            or not utc(ledger["intent"]["createdAt"]) <= utc(value.get("completedAt", "")) < utc(ledger["intent"]["expiresAt"])
            or value.get("images") != ledger["intent"]["acceptedBaseline"]["images"]
            or not re.fullmatch(r"https://github.com/cloud-computing-microservice/techx-chart/actions/runs/[1-9][0-9]*", value.get("chartCi", ""))):
        raise ValueError("Rollback rollout does not prove its exact restored task/revision/runtime")
    proposal = read_proposal_receipt(client, bucket, record, version)["proposal"]
    prefix = f"promotions/staging/{record['sessionId']}/{ledger['approvalReference']['sha256']}"
    merge = read_reference(client, bucket, value["mergeReceiptReference"], prefix + "/receipt.json")
    if (merge.get("approvalReference") != ledger["approvalReference"] or merge.get("taskId") != ledger["rolloutTaskId"]
            or merge.get("chartSha") != value["chartSha"] or merge.get("headSha") != proposal["headSha"] or merge.get("treeSha") != proposal["treeSha"]):
        raise ValueError("Rollback has a different merge task, approval, head or tree")
    merge_intent = read_reference(client, bucket, merge.get("intentReference"), prefix + "/intent.json")
    if (merge_intent.get("taskId") != ledger["rolloutTaskId"] or merge_intent.get("approvalReference") != ledger["approvalReference"]
            or merge_intent.get("request", {}).get("kind") != "rollback-promotion"
            or merge_intent["request"].get("intent") != ledger["intent"]
            or merge_intent["request"].get("proposalReference") != ledger["proposalReference"]
            or not utc(merge_intent.get("createdAt", "")).replace(microsecond=0) <= utc(merge.get("mergedAt", "")) < utc(merge_intent.get("approvalExpiresAt", ""))):
        raise ValueError("Rollback merge lacks its same original intent and approval window")
    application = value.get("application", {})
    if not isinstance(application, dict) or set(application) != {"uid", "intentReference", "specSha256", "stamp"}:
        raise ValueError("Rollback has no exact Application observation")
    app_intent = read_reference(client, bucket, application["intentReference"], f"private/staging/{record['sessionId']}/rollback/application/intent.json")
    app_expected = dict(schemaVersion=1, kind="rollback-application-intent", sessionId=record["sessionId"], executionVersion=version,
        taskId=ledger["rolloutTaskId"], rollbackContext=context, mergeReceiptReference=value["mergeReceiptReference"],
        chartSha=value["chartSha"], uid=application["uid"])
    old = failed_rollout(client, bucket, record, version)
    if (any(app_intent.get(name) != item for name, item in app_expected.items())
            or application["stamp"] != checksum(app_intent) or application["uid"] != old["application"]["uid"]
            or checksum(app_intent.get("previousSpec")) != old["application"]["specSha256"]
            or app_intent.get("previousSpec", {}).get("source", {}).get("targetRevision") != ledger["intent"]["failedChartSha"]):
        raise ValueError("Rollback Application differs from its exact observed failed predecessor")
    selected = deepcopy(app_intent["previousSpec"])
    selected["source"]["targetRevision"] = value["chartSha"]
    if application["specSha256"] != checksum(selected): raise ValueError("Rollback Application spec has another selected revision")
    return value
