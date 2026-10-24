"""Immutable task intent and single-consumer handoff for deployment SSM work."""
from copy import deepcopy
from datetime import timedelta
import re

from automation_session import utc, validate
from plan_approval import version
from plan_review_store import checksum, immutable_json, read_json

ACCOUNT = "058114477594"
DOCUMENT = "techx-automation-deployment-task"
MANAGER = rf"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/techx-automation-lifecycle:[a-f0-9-]+"
TASK_FIELDS = {"schemaVersion", "taskId", "sessionId", "executionVersion", "instanceId", "managerArn",
               "mode", "stage", "attempt", "createdAt", "expiresAt", "expectedPendingPlan"}
CONFIG_FIELDS = {"schemaVersion", "imageId", "launchTemplateId", "launchTemplateVersion", "subnetId", "securityGroupId",
                 "loaderSha256", "imageBuildSha256", "documentVersion", "documentSha256"}


def executor_config(config):
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schemaVersion"] != 1:
        raise ValueError("A complete versioned deployment executor configuration is required")
    for key, pattern in dict(imageId=r"ami-[a-f0-9]{8,17}", launchTemplateId=r"lt-[a-f0-9]{8,17}",
                             subnetId=r"subnet-[a-f0-9]{8,17}", securityGroupId=r"sg-[a-f0-9]{8,17}",
                             loaderSha256=r"[a-f0-9]{64}", imageBuildSha256=r"[a-f0-9]{64}", documentSha256=r"[a-f0-9]{64}",
                             launchTemplateVersion=r"[1-9][0-9]*", documentVersion=r"[1-9][0-9]*").items():
        if not isinstance(config[key], str) or not re.fullmatch(pattern, config[key]):
            raise ValueError(f"Invalid pinned deployment configuration: {key}")
    return config


def validate_task(task, record, now):
    validate(record)
    if not isinstance(task, dict) or set(task) != TASK_FIELDS or task["schemaVersion"] != 1:
        raise ValueError("Unsupported deployment task schema")
    if not re.fullmatch(r"[a-f0-9]{32}", task["taskId"]) or not re.fullmatch(MANAGER, task["managerArn"]):
        raise ValueError("Invalid task/manager identity")
    if not re.fullmatch(r"i-[a-f0-9]{8,17}", task["instanceId"]):
        raise ValueError("Invalid task executor identity")
    version(task["executionVersion"])
    if (record["sessionId"] != task["sessionId"] or record.get("executionVersion") != task["executionVersion"]
            or record["status"] not in ("PLANNING", "PROVISIONING")):
        raise ValueError("Task does not belong to the active admitted session")
    if not utc(task["createdAt"]) <= now < utc(task["expiresAt"]) <= utc(record["cleanupStartAt"]):
        raise ValueError("Task expired or enters the cleanup window")
    if task["mode"] not in ("plan", "apply") or task["stage"] not in ("foundation", "edge") or type(task["attempt"]) is not int or task["attempt"] < 1:
        raise ValueError("Unsupported deployment task action")
    if task["mode"] == "apply":
        pending = task["expectedPendingPlan"]
        if (not isinstance(pending, dict) or record.get("pendingPlan") != pending
                or pending.get("stage") != task["stage"] or pending.get("attempt") != task["attempt"]
                or pending.get("sessionId") != task["sessionId"]):
            raise ValueError("Apply task is stale for the pending reviewed plan")
    elif task["expectedPendingPlan"] is not None:
        raise ValueError("Planning task cannot carry apply permission")
    return task


def create_task(client, bucket, record, execution_version, instance_id, owner, mode, stage, attempt, task_id, now):
    task = dict(schemaVersion=1, taskId=task_id, sessionId=record["sessionId"], executionVersion=execution_version,
                instanceId=instance_id, managerArn=owner, mode=mode, stage=stage, attempt=attempt,
                createdAt=now.isoformat(), expiresAt=min(utc(record["cleanupStartAt"]), now + timedelta(minutes=90)).isoformat(),
                expectedPendingPlan=deepcopy(record.get("pendingPlan")) if mode == "apply" else None)
    validate_task(task, record, now)
    ref = immutable_json(client, bucket, f"control/staging/tasks/{task_id}.json", task)
    return task, dict(taskId=task_id, instanceId=instance_id, managerArn=owner, mode=mode, stage=stage, attempt=attempt,
                      reference=dict(ref, sha256=checksum(task)), state="DISPATCHING")


def read_task(client, bucket, record, task_id, now):
    pointer = record.get("deploymentTask", {})
    ref = pointer.get("reference", {})
    if (pointer.get("taskId") != task_id or not re.fullmatch(r"[a-f0-9]{32}", task_id)
            or ref.get("key") != f"control/staging/tasks/{task_id}.json"):
        raise ValueError("Task is not the current deployment handoff")
    task, _ = read_json(client, bucket, ref["key"], ref.get("versionId"))
    if checksum(task) != ref.get("sha256"):
        raise ValueError("Task intent checksum changed")
    validate_task(task, record, now)
    if any(pointer.get(key) != task[key] for key in ("taskId", "instanceId", "managerArn", "mode", "stage", "attempt")):
        raise ValueError("Task pointer differs from immutable intent")
    return task


def claim_task(store, client, bucket, task_id, instance_id, now):
    record, etag = store.read()
    task = read_task(client, bucket, record, task_id, now)
    if task["instanceId"] != instance_id or record["deploymentTask"]["state"] != "DISPATCHING" or record.get("lease"):
        raise ValueError("Task already consumed, busy or assigned to another executor")
    updated = deepcopy(record)
    updated["deploymentTask"].update(state="RUNNING", startedAt=now.isoformat())
    store.write(updated, etag)  # A duplicate SSM delivery cannot pass this CAS.
    return task


def validate_result(record, task):
    """Stage checkpoint plus released deploy lease, never SSM exit code alone."""
    if record.get("lease"):
        raise ValueError("Remote task has not released its deployment lease")
    stage = record.get("stages", {}).get(task["stage"], {})
    if stage.get("attempt") != task["attempt"]:
        raise ValueError("Remote task did not complete its expected stage attempt")
    if task["mode"] == "plan":
        ref = stage.get("reviewReference")
        if (stage.get("state") != "AWAITING_APPROVAL" or not isinstance(ref, dict) or ref != record.get("pendingPlan")
                or any(ref.get(key) != task[key] for key in ("stage", "attempt")) or ref.get("sessionId") != record["sessionId"]
                or ref.get("key") != f"private/staging/{record['sessionId']}/plans/{task['stage']}/{task['attempt']}/request.json"
                or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
            raise ValueError("Planning task did not publish a durable pending review")
        version(ref.get("versionId"))
    elif (stage.get("state") != "APPLIED" or record.get("pendingPlan") is not None
            or type(stage.get("stateSerial")) is not int or stage["stateSerial"] < 1
            or not re.fullmatch(r"[a-f0-9-]{36}", stage.get("stateLineage", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", stage.get("savedPlanSha256", ""))):
        raise ValueError("Apply task did not observe a committed stage")


def finish_task(store, task, succeeded, now):
    record, etag = store.read()
    if (record.get("sessionId") != task["sessionId"] or record.get("executionVersion") != task["executionVersion"]
            or record.get("deploymentTask", {}).get("taskId") != task["taskId"]
            or record["deploymentTask"].get("state") != "RUNNING" or record.get("status") in ("DESTROYING", "DESTROYED")):
        raise ValueError("Task completion cannot overwrite a cleanup takeover or newer task")
    if succeeded:
        # validate_task's pending-plan equality applies before consumption. A
        # successful apply has deliberately cleared it; verify scope/time here.
        if record["status"] not in ("PLANNING", "PROVISIONING") or now >= utc(record["cleanupStartAt"]):
            raise ValueError("Task completed after the deployment window closed")
        validate_result(record, task)
    updated = deepcopy(record)
    updated["deploymentTask"].update(state="SUCCEEDED" if succeeded else "FAILED", completedAt=now.isoformat())
    if not succeeded:
        updated["status"] = "FAILED_NEEDS_CLEANUP"
    store.write(updated, etag)
