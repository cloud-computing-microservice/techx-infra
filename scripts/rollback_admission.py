"""Read-only rollback preview from a positively terminal failed acceptance.

This does not reserve an attempt, claim a lease, or authorize Git mutations.
The private producer must persist the intent in the session's single-use slot
and revalidate ownership before the proposal/review/merge phases can use it.
"""
from copy import deepcopy
from datetime import timedelta
import re

from active_public_contract import active_public_context, active_result_key, read_active_public_receipt
from active_release_contract import read_rollout_receipt
from automation_session import utc, validate
from chart_selection import INFRA_REPO
from cleanup_completion import CONTEXT
from lifecycle_dispatch import completed_manager, operation
from plan_review_store import checksum, read_json
from ready_release_contract import accepted_baseline, read_reference
from rollback_proposal import validate_intent
from workflow_handoff import listed_json, load_intent, receipt_key, response_identity, verified_run

PUBLIC_FAILURES = {"http-status", "storefront-content", "health-readiness", "health-json", "catalog", "store-config", "product-detail",
                   "order-create", "order-replay", "order-conflict", "order-lookup", "api-content-type", "api-json", "http-redirect",
                   "dynamic-no-store-request-id", "static-cache-policy", "static-cache-hit", "public-argocd-blocked",
                   "static-asset-discovery", "static-asset-path"}


def public_failure(s3, bucket, record, execution, execution_version, github, now):
    projected = dict(record, status="TESTING")  # Read-only historical proof view.
    stage, handoff = record["releaseCandidate"].get("publicAcceptance", {}), record.get("workflowHandoff", {})
    context_hash = checksum(active_public_context(projected))
    if (stage.get("state") != "FAILED" or type(stage.get("attempt")) is not int or stage["attempt"] != 1 or stage.get("contextSha256") != context_hash
            or not re.fullmatch(r"[a-f0-9]{64}", stage.get("dispatchId", ""))
            or handoff.get("id") != stage["dispatchId"] or handoff.get("phase") != "OBSERVED"
            or handoff.get("operation") != "accept-active-public"):
        raise ValueError("Rollback needs the exact failed active public checkpoint")
    intent = load_intent(s3, bucket, projected, handoff.get("intentReference"))
    if (intent["target"].get("operation") != "accept-active-public"
            or intent["target"].get("repository") != INFRA_REPO
            or checksum(active_public_context(dict(projected, **intent["predecessor"]))) != context_hash):
        raise ValueError("Failed public check has a different protected predecessor")
    receipt = read_reference(s3, bucket, handoff.get("receiptReference"), receipt_key(record["sessionId"], handoff["id"]))
    if (not isinstance(receipt, dict) or set(receipt) != {"schemaVersion", "dispatchId", "intentReference", "response"}
            or type(receipt.get("schemaVersion")) is not int or receipt["schemaVersion"] != 1 or receipt.get("dispatchId") != handoff["id"]
            or receipt.get("intentReference") != handoff["intentReference"]):
        raise ValueError("Failed public check lacks its protected dispatch receipt")
    response = response_identity(receipt["response"], INFRA_REPO)
    run_id = response["workflow_run_id"]
    run = verified_run(github, intent["target"], handoff["id"], run_id, execution)
    if (run.get("status") != "completed" or run.get("conclusion") != "failure"
            or stage.get("workflowRun") != response["html_url"]
            or not utc(run["created_at"]) <= utc(run.get("updated_at", "")) <= now):
        raise ValueError("Rollback requires the same actual terminal failed public run")
    jobs = github.get(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    rows = jobs.get("jobs", [])
    if type(jobs.get("total_count")) is not int or jobs["total_count"] != len(rows) or len(rows) != 1:
        raise ValueError("Complete public failure job evidence required")
    job = rows[0]
    if (job.get("name") != "public" or job.get("status") != "completed" or job.get("conclusion") != "failure"
            or job.get("run_id") != run_id or job.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or job.get("runner_group_name") != "GitHub Actions" or job.get("labels") != ["ubuntu-24.04"]
            or type(job.get("id")) is not int or job["id"] < 1 or type(job.get("runner_id")) is not int or job["runner_id"] < 1):
        raise ValueError("Failed public probe did not run on the admitted hosted job")
    key = active_result_key(record["sessionId"], record["releaseCandidate"]["id"], handoff["id"]).replace("result.json", "failure.json")
    found = listed_json(s3, bucket, key)
    if found is None: raise ValueError("No positive public failure diagnostic exists")
    failure, reference = found
    expected = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=execution_version,
        dispatchId=handoff["id"], workflowRun=response["html_url"], contextSha256=context_hash)
    if (not isinstance(failure, dict) or set(failure) != set(expected) | {"classification"} or type(failure.get("schemaVersion")) is not int
            or any(failure.get(name) != value for name, value in expected.items())
            or failure.get("classification") not in PUBLIC_FAILURES):
        raise ValueError("Public failure is unknown or belongs to another release; cleanup is required")
    return reference


def successful_public_predecessor(s3, bucket, record, execution, execution_version, github, now):
    """Recover the successful rollout manager through its consumed public intent."""
    projected = dict(record, status="TESTING")
    handoff = record.get("workflowHandoff", {})
    proof = read_active_public_receipt(s3, bucket, projected, execution_version)
    if (handoff.get("operation") != "accept-active-public" or handoff.get("phase") != "CONSUMED"
            or handoff.get("id") != proof["dispatchId"]):
        raise ValueError("Private failure requires its consumed successful public handoff")
    intent = load_intent(s3, bucket, projected, handoff.get("intentReference"))
    if not isinstance(intent["predecessor"].get("deploymentTask"), dict):
        raise ValueError("Consumed public intent lacks its original rollout task snapshot")
    predecessor = dict(projected, **deepcopy(intent["predecessor"]))
    if checksum(active_public_context(predecessor)) != checksum(active_public_context(projected)):
        raise ValueError("Private failure changed the successful public rollout predecessor")
    receipt = read_reference(s3, bucket, handoff.get("receiptReference"), receipt_key(record["sessionId"], handoff["id"]))
    if (not isinstance(receipt, dict) or set(receipt) != {"schemaVersion", "dispatchId", "intentReference", "response"}
            or type(receipt.get("schemaVersion")) is not int or receipt["schemaVersion"] != 1
            or receipt["dispatchId"] != handoff["id"] or receipt["intentReference"] != handoff["intentReference"]):
        raise ValueError("Successful public handoff lacks its exact dispatch receipt")
    response = response_identity(receipt["response"], INFRA_REPO)
    run_id = response["workflow_run_id"]
    run = verified_run(github, intent["target"], handoff["id"], run_id, execution)
    if (run.get("status") != "completed" or run.get("conclusion") != "success" or proof["workflowRun"] != response["html_url"]
            or not utc(run["created_at"]) <= utc(proof["completedAt"]) <= utc(run.get("updated_at", "")) <= now):
        raise ValueError("Private failure requires the actual successful terminal public workflow")
    jobs = github.get(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    rows = jobs.get("jobs", [])
    if type(jobs.get("total_count")) is not int or jobs["total_count"] != len(rows) or len(rows) != 1:
        raise ValueError("Complete successful public job evidence required")
    job, runner = rows[0], proof["runner"]
    if (job.get("name") != "public" or job.get("status") != "completed" or job.get("conclusion") != "success"
            or job.get("run_id") != run_id or job.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or job.get("id") != runner["jobId"] or job.get("runner_id") != runner["runnerId"]
            or job.get("runner_group_name") != runner["runnerGroup"] or job.get("labels") != runner["labels"]):
        raise ValueError("Successful public receipt does not match the actual hosted job")
    return predecessor


def preview_rollback(clients, bucket, store, execution, execution_version, github, *, clock, created_at=None, source=None):
    s3 = clients["s3"]
    now = clock()
    created = utc(created_at) if created_at is not None else now
    expires = created + timedelta(minutes=30)
    record, etag = store.read()
    validate(record); validate(execution)
    admitted, _ = read_json(s3, bucket, f"executions/{execution['sessionId']}.json", execution_version)
    if (admitted != execution or execution.get("action") != "deploy-window-rollback"
            or any(record.get(name) != execution.get(name) for name in CONTEXT)
            or record.get("executionVersion") != execution_version or record.get("status") != "FAILED_NEEDS_CLEANUP"
            or record.get("lease") or record.get("pendingPlan") is not None or record.get("rollbackAttempt")
            or not utc(execution["createdAt"]) <= created <= now < expires or expires >= utc(record["cleanupStartAt"])):
        raise ValueError("Rollback requires an idle opted-in failed session, an unused attempt and the original window")
    historical = dict(record, status="TESTING")
    private = record.get("lifecycleDispatch", {}).get("operation") == operation("verify", "active")
    if source is not None and source != ("private" if private else "public"):
        raise ValueError("Rollback failure source differs from the requested admission")
    if private:
        from active_private_failure import failed_private_terminal
        failure = failed_private_terminal(clients, bucket, execution, historical, execution_version, now)
        predecessor = successful_public_predecessor(s3, bucket, historical, execution, execution_version, github, now)
    else:
        predecessor = historical
        failure = public_failure(s3, bucket, record, execution, execution_version, github, now)
    if completed_manager(clients, execution, predecessor) != operation("release", "active"):
        raise ValueError("Rollback requires the actual terminal rollout manager and remote task")
    baseline = accepted_baseline(s3, bucket, historical, execution_version)
    rollout = read_rollout_receipt(s3, bucket, historical, execution_version)
    latest, latest_etag = store.read()
    if latest != record or latest_etag != etag or clock() >= expires:
        raise ValueError("Session/cleanup/window changed during rollback admission")
    value = dict(schemaVersion=1, kind="baseline-rollback-intent", sessionId=record["sessionId"], executionVersion=execution_version,
        failedReleaseId=record["releaseCandidate"]["id"], failedChartSha=rollout["chartSha"], failureReference=failure,
        rolloutReference=deepcopy(record["releaseCandidate"]["rollout"]["receiptReference"]), acceptedBaseline=baseline,
        attempt=1, createdAt=created.isoformat(), expiresAt=expires.isoformat(),
        action=execution["action"], cleanupStartAt=record["cleanupStartAt"], deadline=record["deadline"])
    value["rollbackId"] = checksum(value)
    return validate_intent(value)


def preview_public_rollback(*args, **kwargs):
    return preview_rollback(*args, **kwargs, source="public")
