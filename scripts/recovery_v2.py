"""Pure launch checkpoint transitions and durable SSM success contract.

The caller must fetch private exact versions and obtain service observations
through trusted clients. CAS persistence, API dispatch and runtime authorization
are deliberately outside these functions; a JSON payload is not authentication.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from execution_v2 import PREFIX, decode_object, fields, text, timestamp
from worker_identity import validate_handle, validate_launch_intent, worker_identity


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def service_time(value):
    """SSM returns ISO strings with optional fractional seconds and UTC offsets."""
    if not isinstance(value, str):
        raise ValueError("Service timestamp must be an ISO string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid service timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Service timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def wire_time(now):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("Aware observation time required")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def receipt_time(value):
    # Preserve worker precision: truncating to seconds can put a legitimate
    # worker start before SSM's fractional service start timestamp.
    text(value, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.[0-9]{1,6})?Z")
    return service_time(value)


def validate_checkpoint(value, intent):
    fields(value, {"schemaVersion", "launchToken", "phase", "callStartedAt", "instanceId", "observedAt", "workerIdentitySha256"})
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2
            or value["launchToken"] != intent["clientToken"]
            or value["phase"] not in ("PREPARED", "CALLING", "OBSERVED")):
        raise ValueError("Launch checkpoint differs from immutable intent")
    if value["phase"] == "PREPARED":
        if any(value[k] is not None for k in ("callStartedAt", "instanceId", "observedAt", "workerIdentitySha256")):
            raise ValueError("Prepared intent cannot contain a call or service result")
    else:
        timestamp(value["callStartedAt"])
        if value["phase"] == "CALLING":
            if any(value[k] is not None for k in ("instanceId", "observedAt", "workerIdentitySha256")):
                raise ValueError("Unknown launch cannot invent a positive service result")
        else:
            text(value["instanceId"], r"i-[a-f0-9]{17}")
            text(value["workerIdentitySha256"], r"[a-f0-9]{64}")
            if timestamp(value["observedAt"]) < timestamp(value["callStartedAt"]):
                raise ValueError("Launch observation predates its call")
    return deepcopy(value)


def prepare_launch(intent, execution, ref, epoch):
    validate_launch_intent(intent, execution, ref, epoch)
    return dict(schemaVersion=2, launchToken=intent["clientToken"], phase="PREPARED",
                callStartedAt=None, instanceId=None, observedAt=None, workerIdentitySha256=None)


def begin_launch(checkpoint, intent, execution, ref, *, current_epoch, now):
    validate_launch_intent(intent, execution, ref, current_epoch)
    result = validate_checkpoint(checkpoint, intent)
    current = timestamp(wire_time(now))
    if result["phase"] != "PREPARED":
        raise ValueError("Only an unconsumed prepared launch may enter CALLING; observe uncertain calls")
    if current < timestamp(intent["createdAt"]) or current < timestamp(execution["startedAt"]):
        raise ValueError("Launch predates its intent or window")
    if intent["purpose"] != "cleanup" and current >= timestamp(execution["cleanupStartAt"]):
        raise ValueError("Deployment/probe launch forbidden in cleanup reserve")
    # Persist this transition with the expected ETag BEFORE RunInstances. There
    # is intentionally no CALLING -> PREPARED or timeout reset operation.
    result.update(phase="CALLING", callStartedAt=wire_time(now))
    return result


def observe_launch(checkpoint, intent, execution, ref, *, instance, caller, now):
    validate_launch_intent(intent, execution, ref, intent["leaseEpoch"])
    result = validate_checkpoint(checkpoint, intent)
    if result["phase"] == "PREPARED":
        raise ValueError("No persisted CALLING intent to reconcile")
    identity = worker_identity(intent, instance, caller, execution, ref, intent["leaseEpoch"])
    observed = timestamp(wire_time(now))
    if not timestamp(result["callStartedAt"]) <= service_time(identity["launchedAt"]) <= observed:
        raise ValueError("Service launch time differs from the call/observation interval")
    if result["phase"] == "OBSERVED":
        if result["instanceId"] != identity["instanceId"] or result["workerIdentitySha256"] != checksum(identity):
            raise ValueError("Observed worker boot, network or identity changed")
        return result
    result.update(phase="OBSERVED", instanceId=identity["instanceId"], observedAt=wire_time(now),
                  workerIdentitySha256=checksum(identity))
    return result


def validate_successful_result(body, result_ref, handle, task, worker, *, command, invocation, now):
    """Require both terminal AWS success and an exact private durable receipt.

    This checks a result, not current lease/admission or business acceptance.
    R03 must re-read the live CAS epoch before committing any next-stage state.
    """
    validate_handle(handle, task, worker, command=command, now=now)
    key = f"{PREFIX}results/{task['sessionId']}/{task['taskId']}/{handle['commandId']}.json"
    result = decode_object(body, result_ref, key=key)
    fields(result, {"schemaVersion", "handle", "workerIdentitySha256", "outcome", "startedAt", "finishedAt", "evidenceReference"})
    if (type(result["schemaVersion"]) is not int or result["schemaVersion"] != 2 or checksum(result["handle"]) != checksum(handle)
            or result["workerIdentitySha256"] != checksum(worker) or result["outcome"] != "SUCCEEDED"):
        raise ValueError("Durable result differs from the exact command or worker")
    # Evidence is private and separately validated by the operation-specific
    # contract. A valid reference alone cannot prove business acceptance.
    from execution_v2 import reference
    reference(result["evidenceReference"], key=f"{PREFIX}evidence/{task['sessionId']}/{task['taskId']}.json")
    if (not isinstance(invocation, dict) or invocation.get("CommandId") != handle["commandId"]
            or invocation.get("InstanceId") != task["instanceId"]
            or invocation.get("DocumentName") != task["documentName"]
            or invocation.get("DocumentVersion") != task["documentVersion"]
            or invocation.get("PluginName") != "runTask" or invocation.get("Status") != "Success"
            or invocation.get("StatusDetails") != "Success" or type(invocation.get("ResponseCode")) is not int
            or invocation["ResponseCode"] != 0 or command.get("Status") != "Success"):
        raise ValueError("Positive terminal SSM invocation and command success required")
    started, finished = receipt_time(result["startedAt"]), receipt_time(result["finishedAt"])
    service_start = service_time(invocation.get("ExecutionStartDateTime"))
    service_end = service_time(invocation.get("ExecutionEndDateTime"))
    if not command["RequestedDateTime"] <= service_start <= started <= finished <= service_end <= now:
        raise ValueError("Result and service execution timestamps disagree")
    return result
