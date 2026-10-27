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


def exposure_context(record):
    edge = record.get("stages", {}).get("edge", {})
    gitops = validate_gitops_checkpoint(record)
    if (record.get("status") != "PROVISIONING" or record.get("pendingPlan") is not None
            or record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED"
            or edge.get("state") != "APPLIED" or type(edge.get("stateSerial")) is not int or edge["stateSerial"] < 1
            or not re.fullmatch(r"[a-f0-9-]{36}", edge.get("stateLineage", ""))):
        raise ValueError("Exposure requires the committed edge and GitOps checkpoints")
    return dict(edge=deepcopy(edge), gitops=deepcopy(gitops))


def validate_activation(record, attempt):
    context = exposure_context(record)
    if attempt != 1 or record.get("stages", {}).get("exposure"):
        raise ValueError("Only one unconsumed exposure activation is allowed")
    return checksum(context)


def validate_exposure_checkpoint(record, task=None):
    stage = record.get("stages", {}).get("exposure", {})
    ref = stage.get("receiptReference", {})
    if (stage.get("state") != "ACTIVATED" or stage.get("attempt") != 1
            or stage.get("contextSha256") != checksum(exposure_context(record))
            or not re.fullmatch(r"[a-f0-9]{32}", stage.get("taskId", ""))
            or not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != f"private/staging/{record['sessionId']}/exposure/{stage['taskId']}.json"
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))
            or task is not None and (stage["taskId"] != task["taskId"] or stage["contextSha256"] != task.get("expectedExposureSha256"))):
        raise ValueError("Exposure has no exact durable activation receipt checkpoint")
    version(ref.get("versionId"))
    return stage


def promotion_reference(record):
    ref = record.get("bootstrapPromotionApproval", {})
    prefix = f"promotion-approvals/staging/{record['sessionId']}/"
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or not isinstance(ref.get("key"), str) or not ref["key"].startswith(prefix)
            or not re.fullmatch(r"[a-f0-9]{64}\.json", ref["key"][len(prefix):])
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Promotion requires an exact private approval reference")
    version(ref.get("versionId"))
    return ref


def validate_promotion(record, attempt):
    if (record.get("status") != "PROVISIONING" or attempt != 1 or record.get("pendingPlan") is not None
            or record.get("bootstrapChart") or record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED"
            or any(record.get("stages", {}).get(stage) for stage in ("chart", "gitops", "edge"))):
        raise ValueError("Promotion requires one unconsumed pre-GitOps cold task")
    return promotion_reference(record)


def validate_promotion_checkpoint(record, task=None):
    stage = record.get("stages", {}).get("chart", {})
    ref = stage.get("mergeReceiptReference", {})
    approval = promotion_reference(record)
    if (stage.get("state") != "PROMOTED" or stage.get("attempt") != 1 or stage.get("approvalReference") != approval
            or stage.get("releaseReference") != record.get("bootstrapRelease") or stage.get("proposalReference") != record.get("bootstrapProposal")
            or not re.fullmatch(r"[a-f0-9]{32}", stage.get("taskId", ""))
            or any(not re.fullmatch(r"[a-f0-9]{40}", stage.get(key, "")) for key in ("chartSha", "headSha", "treeSha"))
            or not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != f"promotions/staging/{record['sessionId']}/{approval['sha256']}/receipt.json"
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))
            or task is not None and (stage["taskId"] != task["taskId"] or stage["approvalReference"] != task.get("expectedPromotionApproval"))):
        raise ValueError("Promotion has no exact durable task/approval/merge receipt checkpoint")
    version(ref.get("versionId"))
    return stage


def bootstrap_reference(record):
    ref = record.get("bootstrapChart", {})
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != f"chart-selections/staging/{record['sessionId']}/bootstrap.json"
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("GitOps requires a protected immutable chart selection")
    version(ref.get("versionId"))
    return ref


def validate_bootstrap(record, attempt):
    if (record.get("status") != "PROVISIONING" or attempt != 1 or record.get("pendingPlan") is not None
            or record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED"
            or record.get("stages", {}).get("edge") or record.get("stages", {}).get("gitops")):
        raise ValueError("GitOps requires a completed foundation and one unconsumed bootstrap attempt")
    return bootstrap_reference(record)


def validate_gitops_checkpoint(record):
    stage = record.get("stages", {}).get("gitops", {})
    if (stage.get("state") != "BOOTSTRAPPED" or stage.get("attempt") != 1
            or stage.get("selectionReference") != bootstrap_reference(record)
            or stage.get("releaseReference") != record.get("bootstrapRelease")
            or not re.fullmatch(r"[a-f0-9]{40}", stage.get("chartSha", ""))
            or not re.fullmatch(rf"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/[A-Za-z0-9-]+/[a-f0-9]+", stage.get("internalAlbArn", ""))):
        raise ValueError("Edge requires the durable GitOps readiness checkpoint")
    return stage


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
    fields = TASK_FIELDS | ({"expectedBootstrapChart"} if isinstance(task, dict) and task.get("mode") == "bootstrap" else set())
    if isinstance(task, dict) and task.get("mode") == "promote": fields |= {"expectedPromotionApproval"}
    if isinstance(task, dict) and task.get("mode") == "activate": fields |= {"expectedExposureSha256"}
    if isinstance(task, dict) and task.get("mode") == "verify": fields |= {"expectedAcceptanceSha256"}
    if isinstance(task, dict) and task.get("mode") == "release": fields |= {"expectedReleaseContext"}
    if not isinstance(task, dict) or set(task) != fields or task["schemaVersion"] != 1:
        raise ValueError("Unsupported deployment task schema")
    if not re.fullmatch(r"[a-f0-9]{32}", task["taskId"]) or not re.fullmatch(MANAGER, task["managerArn"]):
        raise ValueError("Invalid task/manager identity")
    if not re.fullmatch(r"i-[a-f0-9]{8,17}", task["instanceId"]):
        raise ValueError("Invalid task executor identity")
    version(task["executionVersion"])
    if (record["sessionId"] != task["sessionId"] or record.get("executionVersion") != task["executionVersion"]
            or record["status"] not in (("TESTING",) if task.get("mode") == "release" else ("PROVISIONING", "READY") if task.get("mode") == "verify" else ("PLANNING", "PROVISIONING"))):
        raise ValueError("Task does not belong to the active admitted session")
    if not utc(task["createdAt"]) <= now < utc(task["expiresAt"]) <= utc(record["cleanupStartAt"]):
        raise ValueError("Task expired or enters the cleanup window")
    if ((task["mode"], task["stage"]) not in {("plan", "foundation"), ("apply", "foundation"), ("plan", "edge"), ("apply", "edge"), ("bootstrap", "gitops"), ("promote", "chart"), ("activate", "exposure"), ("verify", "private"), ("release", "active")}
            or type(task["attempt"]) is not int or task["attempt"] < 1):
        raise ValueError("Unsupported deployment task action")
    if task["mode"] == "bootstrap" and task["expectedBootstrapChart"] != bootstrap_reference(record):
        raise ValueError("Bootstrap task selection changed after dispatch")
    if task["mode"] == "promote" and (task["attempt"] != 1 or task["expectedPromotionApproval"] != promotion_reference(record)):
        raise ValueError("Promotion task approval changed after dispatch")
    if task["mode"] == "activate" and (task["attempt"] != 1 or task["expectedExposureSha256"] != checksum(exposure_context(record))):
        raise ValueError("Exposure task predecessor changed after dispatch")
    if task["mode"] == "verify":
        from private_acceptance_contract import private_context, validate_private_checkpoint
        if task["attempt"] != 1 or task["expectedAcceptanceSha256"] != checksum(private_context(record)):
            raise ValueError("Private task predecessor changed after dispatch")
        if record["status"] == "READY": validate_private_checkpoint(record, task)
    if task["mode"] == "release":
        from active_release_contract import release_context
        if task["attempt"] != 1 or task["expectedReleaseContext"] != release_context(record):
            raise ValueError("Active task candidate/baseline/grant changed after dispatch")
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
    if mode == "bootstrap":
        task["expectedBootstrapChart"] = deepcopy(validate_bootstrap(record, attempt))
    if mode == "promote": task["expectedPromotionApproval"] = deepcopy(validate_promotion(record, attempt))
    if mode == "activate": task["expectedExposureSha256"] = validate_activation(record, attempt)
    if mode == "verify":
        from private_acceptance_contract import validate_private_start
        task["expectedAcceptanceSha256"] = validate_private_start(record, attempt)
    if mode == "release":
        from active_release_contract import validate_release_start
        task["expectedReleaseContext"] = validate_release_start(record, attempt)
    validate_task(task, record, now)
    ref = immutable_json(client, bucket, f"control/staging/tasks/{task_id}.json", task)
    pointer = dict(taskId=task_id, instanceId=instance_id, managerArn=owner, mode=mode, stage=stage, attempt=attempt,
                   reference=dict(ref, sha256=checksum(task)), state="DISPATCHING")
    if mode == "bootstrap": pointer["expectedBootstrapChart"] = deepcopy(task["expectedBootstrapChart"])
    if mode == "promote": pointer["expectedPromotionApproval"] = deepcopy(task["expectedPromotionApproval"])
    if mode == "activate": pointer["expectedExposureSha256"] = task["expectedExposureSha256"]
    if mode == "verify": pointer["expectedAcceptanceSha256"] = task["expectedAcceptanceSha256"]
    if mode == "release": pointer["expectedReleaseContext"] = deepcopy(task["expectedReleaseContext"])
    return task, pointer


def read_task(client, bucket, record, task_id, now):
    pointer = record.get("deploymentTask", {})
    ref = pointer.get("reference", {})
    if (pointer.get("taskId") != task_id or not re.fullmatch(r"[a-f0-9]{32}", task_id)
            or ref.get("key") != f"control/staging/tasks/{task_id}.json"):
        raise ValueError("Task is not the current deployment handoff")
    version(ref.get("versionId"))
    task, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    if checksum(task) != ref.get("sha256"):
        raise ValueError("Task intent checksum changed")
    validate_task(task, record, now)
    if any(pointer.get(key) != task[key] for key in ("taskId", "instanceId", "managerArn", "mode", "stage", "attempt")):
        raise ValueError("Task pointer differs from immutable intent")
    if task["mode"] == "bootstrap" and pointer.get("expectedBootstrapChart") != task["expectedBootstrapChart"]:
        raise ValueError("Bootstrap pointer differs from immutable selection intent")
    if task["mode"] == "promote" and pointer.get("expectedPromotionApproval") != task["expectedPromotionApproval"]:
        raise ValueError("Promotion pointer differs from immutable approval intent")
    if task["mode"] == "activate" and pointer.get("expectedExposureSha256") != task["expectedExposureSha256"]:
        raise ValueError("Exposure pointer differs from immutable predecessor intent")
    if task["mode"] == "verify" and pointer.get("expectedAcceptanceSha256") != task["expectedAcceptanceSha256"]:
        raise ValueError("Private pointer differs from immutable predecessor intent")
    if task["mode"] == "release" and pointer.get("expectedReleaseContext") != task["expectedReleaseContext"]:
        raise ValueError("Active task pointer differs from its immutable candidate/grant")
    return task


def claim_task(store, client, bucket, task_id, instance_id, now):
    record, etag = store.read()
    task = read_task(client, bucket, record, task_id, now)
    if (task["instanceId"] != instance_id or record["status"] not in (("TESTING",) if task["mode"] == "release" else ("PLANNING", "PROVISIONING"))
            or record["deploymentTask"]["state"] != "DISPATCHING" or record.get("lease")):
        raise ValueError("Task already consumed, busy or assigned to another executor")
    updated = deepcopy(record)
    updated["deploymentTask"].update(state="RUNNING", startedAt=now.isoformat())
    store.write(updated, etag)  # A duplicate SSM delivery cannot pass this CAS.
    return task


def validate_result(record, task):
    """Stage checkpoint plus released deploy lease, never SSM exit code alone."""
    if record.get("lease"):
        raise ValueError("Remote task has not released its deployment lease")
    if task["mode"] == "release":
        from active_release_contract import validate_rollout_checkpoint
        validate_rollout_checkpoint(record, task)
        return
    stage = record.get("stages", {}).get(task["stage"], {})
    if stage.get("attempt") != task["attempt"]:
        raise ValueError("Remote task did not complete its expected stage attempt")
    if task["mode"] == "verify":
        from private_acceptance_contract import validate_private_checkpoint
        completed = validate_private_checkpoint(record, task)
        # The cold task remains a readable accepted predecessor while a new
        # rollout is TESTING. finish_task still requires READY to complete a
        # verify task; this does not make the active runtime accepted.
        predecessor = record["status"] == "TESTING" and record.get("lifecycleDispatch", {}).get("operation") == dict(mode="release", stage="active", attempt=1)
        if (record["status"] != "READY" and not predecessor) or record.get("acceptedRelease") != dict(completed["release"], acceptanceReference=completed["receiptReference"]):
            raise ValueError("Private task has not atomically handed off its accepted release to READY")
    elif task["mode"] == "activate":
        validate_exposure_checkpoint(record, task)
    elif task["mode"] == "promote":
        validate_promotion_checkpoint(record, task)
    elif task["mode"] == "bootstrap":
        validate_gitops_checkpoint(record)
        if stage.get("selectionReference") != task.get("expectedBootstrapChart"):
            raise ValueError("Bootstrap completed a different chart selection")
    elif task["mode"] == "plan":
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
        allowed = ("TESTING",) if task["mode"] == "release" else ("READY",) if task["mode"] == "verify" else ("PLANNING", "PROVISIONING")
        if record["status"] not in allowed or now >= utc(record["cleanupStartAt"]):
            raise ValueError("Task completed after the deployment window closed")
        validate_result(record, task)
    updated = deepcopy(record)
    updated["deploymentTask"].update(state="SUCCEEDED" if succeeded else "FAILED", completedAt=now.isoformat())
    if not succeeded:
        updated["status"] = "FAILED_NEEDS_CLEANUP"
    store.write(updated, etag)
