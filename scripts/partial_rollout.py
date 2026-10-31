"""Positive Application mutation with bounded unready runtime, never acceptance."""
from copy import deepcopy
from datetime import timedelta
import re

from active_release_contract import reference, release_context
from automation_session import utc
from plan_review_store import checksum
from ready_release_contract import read_manifest, read_proposal, read_reference


class PartialRolloutFailed(TimeoutError):
    pass


def result_key(record):
    return f"private/staging/{record['sessionId']}/active/{release_context(record)['releaseId']}/partial-rollout.json"


def failed_reference(record):
    candidate = record.get("releaseCandidate", {})
    if candidate.get("partialRollout"):
        if candidate.get("rollout"): raise ValueError("A partial rollout cannot also claim a completed rollout")
        return candidate["partialRollout"].get("receiptReference")
    return candidate.get("rollout", {}).get("receiptReference")


def read_partial_rollout(client, bucket, record, execution_version):
    """Validate only the failed mutation proof; no ready runtime is implied."""
    context = release_context(record)
    candidate = record["releaseCandidate"]
    checkpoint = candidate.get("partialRollout", {})
    if (record.get("status") != "TESTING" or candidate.get("state") != "PROPOSED" or candidate.get("rollout")
            or candidate.get("publicAcceptance") or candidate.get("privateAcceptance")
            or not isinstance(checkpoint, dict) or set(checkpoint) != {"state", "taskId", "contextSha256", "chartSha", "receiptReference"}
            or checkpoint.get("state") != "FAILED" or checkpoint.get("contextSha256") != checksum(context)
            or not re.fullmatch(r"[a-f0-9]{32}", checkpoint.get("taskId", ""))
            or not re.fullmatch(r"[a-f0-9]{40}", checkpoint.get("chartSha", ""))):
        raise ValueError("Partial rollout lacks its exact failed candidate/task checkpoint")
    value = read_reference(client, bucket, checkpoint.get("receiptReference"), result_key(record))
    expected = dict(schemaVersion=1, kind="active-partial-rollout", sessionId=record["sessionId"], executionVersion=execution_version,
        releaseContext=context, taskId=checkpoint["taskId"], chartSha=checkpoint["chartSha"])
    if (not isinstance(value, dict) or set(value) != set(expected) | {"mergeReceiptReference", "chartCi", "application", "expectedImages", "readiness"}
            or type(value.get("schemaVersion")) is not int or any(value.get(k) != v for k, v in expected.items())
            or not re.fullmatch(r"https://github.com/cloud-computing-microservice/techx-chart/actions/runs/[1-9][0-9]*", value.get("chartCi", ""))):
        raise ValueError("Partial rollout receipt belongs to another admitted mutation")
    readiness = value.get("readiness", {})
    if (not isinstance(readiness, dict) or set(readiness) != {"classification", "startedAt", "failedAt", "observations"}
            or readiness.get("classification") != "runtime-readiness-timeout"
            or type(readiness.get("observations")) is not int or not 1 <= readiness["observations"] <= 120
            or not utc(record["createdAt"]) <= utc(readiness.get("startedAt", "")) <= utc(readiness.get("failedAt", "")) < utc(record["cleanupStartAt"])
            or readiness["observations"] != 120 and utc(readiness["failedAt"]) < utc(readiness["startedAt"]) + timedelta(minutes=10)):
        raise ValueError("Partial rollout did not positively exhaust its bounded readiness observations")
    manifest = read_manifest(client, bucket, record, execution_version)["manifest"]
    names = dict(frontend="frontend", catalog="catalog-api", order="order-api")
    expected_images = {names[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in manifest["images"]}
    if value["expectedImages"] != expected_images: raise ValueError("Partial rollout has different intended artifacts")
    merge = read_reference(client, bucket, value["mergeReceiptReference"], f"promotions/staging/{record['sessionId']}/{context['approvalReference']['sha256']}/receipt.json")
    proposal = read_proposal(client, bucket, record, execution_version)
    if (merge.get("approvalReference") != context["approvalReference"] or merge.get("taskId") != checkpoint["taskId"]
            or merge.get("chartSha") != checkpoint["chartSha"] or merge.get("headSha") != proposal["headSha"] or merge.get("treeSha") != proposal["treeSha"]):
        raise ValueError("Partial rollout has another private merge task/head/tree")
    app = value.get("application", {})
    if not isinstance(app, dict) or set(app) != {"uid", "intentReference", "specSha256", "stamp"}:
        raise ValueError("Partial rollout has no positive Application mutation identity")
    intent = read_reference(client, bucket, app["intentReference"], f"private/staging/{record['sessionId']}/active/{context['releaseId']}/application/intent.json")
    wanted = dict(schemaVersion=1, kind="active-application-intent", sessionId=record["sessionId"], executionVersion=execution_version,
        taskId=checkpoint["taskId"], releaseContext=context, mergeReceiptReference=value["mergeReceiptReference"], chartSha=checkpoint["chartSha"], uid=app["uid"])
    if (any(intent.get(k) != v for k, v in wanted.items()) or app["stamp"] != checksum(intent)
            or not isinstance(app["uid"], str) or not 1 <= len(app["uid"]) <= 128
            or intent.get("previousSpec", {}).get("source", {}).get("targetRevision") != context["acceptedBaseline"]["chartSha"]):
        raise ValueError("Partial rollout changed its original Application intent or baseline")
    spec = deepcopy(intent["previousSpec"]); spec["source"]["targetRevision"] = checkpoint["chartSha"]
    if checksum(spec) != app["specSha256"]: raise ValueError("Partial Application spec differs from the observed selected revision")
    return value


def persist_partial(worker, receipt, merge_ref, application, images, started_at, observations):
    failed_at = worker.clock()
    if (type(observations) is not int or not 1 <= observations <= 120 or started_at > failed_at
            or observations != 120 and failed_at < started_at + timedelta(minutes=10)):
        raise TimeoutError("Readiness observation ended without a bounded failure proof")
    current = worker.guard()
    if current["releaseCandidate"].get("partialRollout") or current["releaseCandidate"].get("rollout"):
        raise ValueError("Partial rollout evidence is single-use")
    ci = worker.main_ci(receipt["chartSha"])
    worker.current_chart(receipt["chartSha"])
    intent = read_reference(worker.runtime.clients["s3"], worker.runtime.bucket, application["intentReference"], worker.application_key(current))
    spec = deepcopy(intent["previousSpec"]); spec["source"]["targetRevision"] = receipt["chartSha"]
    worker.app_observation(worker.kube.get("application", "techx-staging", "argocd"), spec, application["uid"], application["stamp"])
    value = dict(schemaVersion=1, kind="active-partial-rollout", sessionId=current["sessionId"], executionVersion=worker.version,
        releaseContext=release_context(current), taskId=worker.task_id, chartSha=receipt["chartSha"], mergeReceiptReference=merge_ref,
        chartCi=ci, application=application, expectedImages=images,
        readiness=dict(classification="runtime-readiness-timeout", startedAt=started_at.isoformat(),
            failedAt=failed_at.isoformat(), observations=observations))
    ref = worker.put(result_key(current), value)
    current, etag = worker.control.owned(worker.runtime.execution["sessionId"], worker.owner, worker.epoch)
    worker.guard()
    updated = deepcopy(current)
    updated["releaseCandidate"]["partialRollout"] = dict(state="FAILED", taskId=worker.task_id,
        contextSha256=checksum(release_context(current)), chartSha=receipt["chartSha"], receiptReference=ref)
    read_partial_rollout(worker.runtime.clients["s3"], worker.runtime.bucket, updated, worker.version)
    worker.store.write(updated, etag)
    raise PartialRolloutFailed("The exact selected Application did not reach runtime readiness")


def failed_partial_terminal(clients, bucket, execution, record, execution_version, now, *, require_manager=True, allow_pending=False):
    from deployment_manager import observe_command
    from deployment_tasks import executor_config, read_task
    from lifecycle_dispatch import BUILD, observed_build, operation
    projected = dict(record, status="TESTING")
    value = read_partial_rollout(clients["s3"], bucket, projected, execution_version)
    pointer, dispatch = record.get("deploymentTask", {}), record.get("lifecycleDispatch", {})
    if (pointer.get("state") != "FAILED" or type(pointer.get("attempt")) is not int
            or (pointer.get("mode"), pointer.get("stage"), pointer["attempt"]) != ("release", "active", 1)
            or pointer.get("taskId") != value["taskId"] or dispatch.get("operation") != operation("release", "active")
            or dispatch.get("phase") != "OBSERVED" or not re.fullmatch(BUILD, dispatch.get("buildId", ""))
            or pointer.get("managerArn") != f"arn:aws:codebuild:us-east-1:058114477594:build/{dispatch['buildId']}"):
        raise ValueError("Partial rollout requires this exact failed release task/manager")
    task = read_task(clients["s3"], bucket, projected, value["taskId"], now)
    if not utc(task["createdAt"]) <= utc(value["readiness"]["startedAt"]) <= utc(value["readiness"]["failedAt"]) <= utc(pointer.get("completedAt", "")) <= now:
        raise ValueError("Partial failure was not observed within its original task")
    pending = False
    if require_manager:
        build = observed_build(clients["codebuild"], record)
        pending = allow_pending and build.get("buildStatus") == "IN_PROGRESS" and build.get("buildComplete") is False
        if not pending and (build.get("buildStatus") != "FAILED" or build.get("buildComplete") is not True):
            raise ValueError("Partial rollback needs its positively terminal failed manager")
    result = observe_command(clients["ssm"], executor_config(execution["deploymentConfig"]), pointer)
    if result is None or result.get("Status") != "Failed" or type(result.get("ResponseCode")) is not int or result["ResponseCode"] != 1:
        raise ValueError("Partial rollback needs its positively terminal failed SSM invocation")
    if pending:
        from rollback_pending import RollbackPending
        raise RollbackPending("ROLLBACK_FAILED_MANAGER_RUNNING")
    return projected["releaseCandidate"]["partialRollout"]["receiptReference"]
