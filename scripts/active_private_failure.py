"""Versioned, sanitized private diagnostics; uncertainty never grants rollback."""
from copy import deepcopy
import ipaddress
import re

from active_private_contract import active_private_context, private_result_key
from active_public_contract import read_active_public_receipt
from automation_session import utc
from deployment_tasks import read_task
from edge_activation import read_activation_receipt
from plan_review_store import checksum, immutable_json
from private_acceptance_contract import evidence_record
from private_http import PrivateFailure
from ready_release_contract import read_reference

# Authentication, configuration, ECR scan, TLS/network and unknown exceptions
# require cleanup. A diagnostic is evidence, never authority by itself.
APPLICATION_FAILURES = {"runtime-readiness", "argocd-revision-health", "order-persistence",
                        "idempotency-restart-persistence", "self-heal-timeout"}


def failure_key(record, task_id):
    return private_result_key(record, task_id).removesuffix(".json") + ".failure.json"


def validate_vpn(value, endpoint, classification):
    if value == {"state": "NOT_STARTED"} and classification == "runtime-readiness": return
    if (not isinstance(value, dict) or set(value) != {"state", "endpointId", "connectionId", "clientIp", "commonName", "device", "disconnected"}
            or value.get("state") != "DISCONNECTED" or value.get("disconnected") is not True
            or value.get("endpointId") != endpoint or value.get("commonName") != "techx-demo-operator"
            or not re.fullmatch(r"cvpn-connection-[a-f0-9]{8,17}", value.get("connectionId", ""))
            or not re.fullmatch(r"tx[a-f0-9]{10}", value.get("device", ""))
            or ipaddress.ip_address(value.get("clientIp", "")) not in ipaddress.ip_network("172.24.0.0/22")):
        raise ValueError("Private failure has no positive disconnected VPN evidence; cleanup is required")


def sanitized_vpn(worker):
    tunnel = worker.failure_tunnel
    if tunnel is None: return dict(state="NOT_STARTED")
    try:
        value = dict(tunnel.evidence(), state="DISCONNECTED")
        validate_vpn(value, worker.binding["endpointId"], "order-persistence")
        return value
    except Exception:
        return dict(state="UNPROVEN")


def persist_failure(worker, error):
    """Called only while the original task still owns its live acceptance lease.

    No retry can replace a terminal checkpoint. Lost writes remain evidence for
    cleanup; they do not authorize a new probe, task or diagnostic version.
    """
    worker.binding_guard()
    current, etag = worker.controller.owned(worker.runtime.execution["sessionId"], worker.owner, worker.epoch)
    worker.validate_record(current)
    stage = worker.stage(current)
    if (stage.get("state") != "RUNNING" or stage.get("taskId") != worker.task_id
            or stage.get("contextSha256") != worker.context_hash):
        raise ValueError("Failure diagnostic requires this exact running private checkpoint")
    classification = error.classification if isinstance(error, PrivateFailure) and error.classification in APPLICATION_FAILURES else "unclassified"
    pointer = current["releaseCandidate"]
    value = dict(schemaVersion=1, kind="active-private-failure", sessionId=current["sessionId"], executionVersion=worker.version,
        taskId=worker.task_id, releaseId=pointer["id"], contextSha256=worker.context_hash, stateSha256=worker.state_hash,
        rolloutReference=deepcopy(pointer["rollout"]["receiptReference"]), publicReference=worker.public_reference(current),
        classification=classification, failedAt=worker.clock().isoformat(), vpn=sanitized_vpn(worker))
    ref = immutable_json(worker.runtime.clients["s3"], worker.runtime.bucket, failure_key(current, worker.task_id), value)
    ref["sha256"] = checksum(value)
    read_reference(worker.runtime.clients["s3"], worker.runtime.bucket, ref, failure_key(current, worker.task_id))
    worker.binding_guard()
    latest, latest_etag = worker.controller.owned(worker.runtime.execution["sessionId"], worker.owner, worker.epoch)
    if latest != current or latest_etag != etag: raise ValueError("Private failure changed before checkpoint CAS")
    updated = deepcopy(current)
    worker.set_stage(updated, dict(stage, state="FAILED", failureReference=ref))
    worker.store.write(updated, etag)


def read_failure(s3, bucket, record, execution_version, now):
    """Read exact private task/proof, including prior public and edge bindings."""
    projected = dict(record, status="TESTING")  # Historical validation only.
    candidate = projected["releaseCandidate"]
    stage, pointer = candidate.get("privateAcceptance", {}), projected.get("deploymentTask", {})
    context_hash = checksum(active_private_context(projected))
    if (stage.get("state") != "FAILED" or type(stage.get("attempt")) is not int or stage["attempt"] != 1
            or stage.get("contextSha256") != context_hash or stage.get("taskId") != pointer.get("taskId")
            or pointer.get("state") != "FAILED" or type(pointer.get("attempt")) is not int
            or (pointer.get("mode"), pointer.get("stage"), pointer.get("attempt")) != ("verify", "active", 1)):
        raise ValueError("Rollback needs this exact failed private task/checkpoint")
    task = read_task(s3, bucket, projected, stage["taskId"], now)
    ref = stage.get("failureReference")
    value = read_reference(s3, bucket, ref, failure_key(projected, stage["taskId"]))
    expected = dict(schemaVersion=1, kind="active-private-failure", sessionId=record["sessionId"], executionVersion=execution_version,
        taskId=stage["taskId"], releaseId=candidate["id"], contextSha256=context_hash,
        rolloutReference=candidate["rollout"]["receiptReference"], publicReference=candidate["publicAcceptance"]["receiptReference"])
    if (not isinstance(value, dict) or set(value) != set(expected) | {"classification", "failedAt", "vpn", "stateSha256"}
            or type(value.get("schemaVersion")) is not int or any(value.get(k) != v for k, v in expected.items())
            or value.get("classification") not in APPLICATION_FAILURES
            or not utc(task["createdAt"]) <= utc(value.get("failedAt", "")) <= utc(pointer.get("completedAt", "")) <= now
            or utc(value["failedAt"]) >= utc(task["expiresAt"])):
        raise ValueError("Private failure is unknown or outside its admitted task/window; cleanup is required")
    read_active_public_receipt(s3, bucket, projected, execution_version)
    activation = read_activation_receipt(s3, bucket, evidence_record(projected), execution_version)
    if value["stateSha256"] != activation["stateSha256"]: raise ValueError("Private failure used another edge state")
    validate_vpn(value["vpn"], activation["resources"]["endpointId"], value["classification"])
    return ref


def failed_private_terminal(clients, bucket, execution, record, execution_version, now, *, require_manager=True):
    """A failed checkpoint alone cannot prove the remote command has stopped."""
    from deployment_manager import observe_command
    from deployment_tasks import executor_config
    from lifecycle_dispatch import BUILD, observed_build, operation
    dispatch = record.get("lifecycleDispatch", {})
    if (dispatch.get("operation") != operation("verify", "active") or dispatch.get("phase") != "OBSERVED"
            or not re.fullmatch(BUILD, dispatch.get("buildId", ""))
            or record.get("deploymentTask", {}).get("managerArn") != f"arn:aws:codebuild:us-east-1:058114477594:build/{dispatch['buildId']}"):
        raise ValueError("Private rollback predecessor must be the active acceptance manager")
    ref = read_failure(clients["s3"], bucket, record, execution_version, now)
    pointer = record["deploymentTask"]
    if require_manager:
        build = observed_build(clients["codebuild"], record)
        if (build.get("buildStatus") != "FAILED" or build.get("buildComplete") is not True
                or pointer.get("managerArn") != build["arn"]):
            raise ValueError("Private failure needs the exact positively terminal failed manager")
    result = observe_command(clients["ssm"], executor_config(execution["deploymentConfig"]), pointer)
    if result is None or result.get("Status") != "Failed" or type(result.get("ResponseCode")) is not int or result["ResponseCode"] != 1:
        raise ValueError("Private failure needs the exact positively terminal failed remote task")
    return ref
