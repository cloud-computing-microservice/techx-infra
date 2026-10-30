"""Positive protected rollback grant consumption; no merge or workload mutation."""
from copy import deepcopy

from automation_session import utc
from chart_selection import INFRA_REPO
from plan_approval import version as object_version
from plan_review_store import read_json
from ready_release_contract import read_reference
from review_chart_promotion import (ENVIRONMENT, promotion_index_key, read_promotion_approval, verified_reviewer)
from rollback_chart_promotion import RollbackPromotionChecks
from rollback_reservation import controller
from rollback_task_contract import proposal_record, slot
from workflow_handoff import load_intent, receipt_key, response_identity, verified_run


def reviewed_grant(clients, bucket, record, execution, version, infra, reference, now):
    """Require actual terminal review, protected run receipt and human approval."""
    prepared = proposal_record(record)
    handoff = prepared.get("workflowHandoff", {})
    if handoff.get("phase") not in ("OBSERVED", "CONSUMED") or handoff.get("operation") != "review-rollback":
        raise ValueError("Rollback grant has no protected prepared-review predecessor")
    intent = load_intent(clients["s3"], bucket, prepared, handoff.get("intentReference"))
    predecessor = intent["predecessor"]
    if (intent["target"].get("operation") != "review-rollback"
            or any(predecessor.get(name) != prepared.get(name) for name in
                ("status", "rollbackAttempt", "deploymentTask", "lifecycleDispatch", "releaseCandidate", "acceptedRelease"))):
        raise ValueError("Rollback grant review changed its exact prepared snapshot")
    receipt = read_reference(clients["s3"], bucket, handoff.get("receiptReference"), receipt_key(record["sessionId"], handoff["id"]))
    if (set(receipt) != {"schemaVersion", "dispatchId", "intentReference", "response"} or type(receipt.get("schemaVersion")) is not int
            or receipt["schemaVersion"] != 1 or receipt.get("dispatchId") != handoff["id"] or receipt.get("intentReference") != handoff["intentReference"]):
        raise ValueError("Rollback review receipt differs from its protected intent")
    response = response_identity(receipt["response"], INFRA_REPO)
    number, url = response["workflow_run_id"], response["html_url"]
    run = verified_run(infra, intent["target"], handoff["id"], number, execution)
    approval = read_promotion_approval(clients["s3"], bucket, execution, version, reference, now, kind="rollback-promotion")
    approved = utc(approval["approvedAt"])
    if (run.get("status") != "completed" or run.get("conclusion") != "success"
            or approval["request"].get("workflowRun") != url
            or not utc(run["created_at"]) <= approved or not approved.replace(microsecond=0) <= utc(run.get("updated_at", "")) <= now
            or verified_reviewer(infra, number, environment=ENVIRONMENT) != approval["reviewer"]):
        raise ValueError("Rollback grant lacks the same successful review and actual human approval")
    index, index_version = read_json(clients["s3"], bucket, promotion_index_key(record["sessionId"], number))
    object_version(index_version)
    expected = dict(schemaVersion=1, kind="rollback-promotion-index", sessionId=record["sessionId"], executionVersion=version,
        sourceRevisions=execution["sourceRevisions"], dispatchId=handoff["id"], workflowRun=url, approvalReference=reference)
    if index != expected: raise ValueError("Rollback grant has no exact protected run index")
    return approval


def consume_grant(dispatcher, infra, chart, source, run_id):
    d = dispatcher
    controller(d.clients, infra, d.execution, run_id, d.clock())
    record, etag = d.current(allow_rollback=True)
    value = slot(record)
    if value["phase"] == "APPROVED":
        # A positive read of the same consumed slot recovers a lost CAS. It
        # never replaces the grant, creates an attempt or extends its expiry.
        reviewed_grant(d.clients, d.bucket, record, d.execution, d.version, infra, value["approvalReference"], d.clock())
        return deepcopy(value["approvalReference"])
    if value["phase"] != "PROPOSED": raise ValueError("Rollback is not awaiting its first protected grant")
    state = record.get("workflowHandoff", {})
    load_intent(d.clients["s3"], d.bucket, record, state.get("intentReference"))
    receipt = read_reference(d.clients["s3"], d.bucket, state.get("receiptReference"), receipt_key(record["sessionId"], state["id"]))
    number = response_identity(receipt["response"], INFRA_REPO)["workflow_run_id"]
    index, index_version = read_json(d.clients["s3"], d.bucket, promotion_index_key(record["sessionId"], number))
    object_version(index_version)
    reference = index.get("approvalReference")
    grant = reviewed_grant(d.clients, d.bucket, record, d.execution, d.version, infra, reference, d.clock())
    checks = RollbackPromotionChecks(d.clients, d.store, d.bucket, d.execution, d.version, infra=infra, clock=d.clock)
    policy = grant["request"]["branchPolicy"]
    fresh = checks.evaluate(source, chart, app_id=policy["appId"], app_slug=policy["appSlug"])
    fresh["workflowRun"] = grant["request"]["workflowRun"]
    if fresh != grant["request"]: raise ValueError("Rollback grant differs from the current exact reviewed proposal")
    reviewed_grant(d.clients, d.bucket, record, d.execution, d.version, infra, reference, d.clock())
    latest, latest_etag = d.current(allow_rollback=True)
    if latest != record or latest_etag != etag: raise ValueError("Rollback/cleanup changed before grant consumption")
    updated = deepcopy(record)
    updated["workflowHandoff"]["phase"] = "CONSUMED"
    updated["rollbackAttempt"].update(phase="APPROVED", approvalReference=deepcopy(reference),
        preparation={name: deepcopy(updated[name]) for name in ("lifecycleDispatch", "deploymentTask", "workflowHandoff")})
    slot(updated)
    d.store.write(updated, etag)
    return deepcopy(reference)
