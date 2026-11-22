"""Control-only migration contracts and exact legacy service-denial binding.

Parsing does not retire authority or admit staging. R02/R06 must independently
collect and verify complete histories, actual human review, all terminal actors,
fresh workload zero and effective policy retirement before publishing closeout.
No function here writes v1 data, manufactures a build ID, or issues a window.
"""
from copy import deepcopy
import json
import re
from urllib.parse import quote

from execution_v2 import (ACCOUNT, REGION, SOURCES, bind_authorized_window, cost_window, decode_object,
                          fields, integer, read_legacy, reference, text, timestamp, window_reference)
from recovery_v2 import checksum, service_time

_SNAPSHOT_SEAL = object()
IMMUTABLE = ("sessionId", "account", "region", "environment", "sourceRevisions", "approvedBudgetUsd",
             "deadline", "cleanupStartAt", "maximumHours", "action", "createdAt")


class LegacySnapshot:
    """Prevents a research-summary dict from substituting for bound raw objects."""
    __slots__ = ("__payload",)

    def __init__(self, payload, *, _seal=None):
        if _seal is not _SNAPSHOT_SEAL: raise ValueError("Capture the exact legacy objects first")
        self.__payload = deepcopy(payload)

    def as_dict(self):
        return deepcopy(self.__payload)


def snapshot_value(value):
    if type(value) is not LegacySnapshot:
        raise ValueError("Untrusted JSON is not a captured legacy snapshot")
    return value.as_dict()


class MigrationExecution:
    """Bound control-only execution; parsing is not admission/launch authority."""
    __slots__ = ("__payload", "__reference")

    def __init__(self, payload, ref, *, _seal=None):
        if _seal is not _SNAPSHOT_SEAL: raise ValueError("Read and bind the control execution/window first")
        self.__payload, self.__reference = deepcopy(payload), deepcopy(ref)

    def as_dict(self):
        return deepcopy(self.__payload)

    def reference(self):
        return deepcopy(self.__reference)


def control_value(value, ref):
    if type(value) is not MigrationExecution or checksum(value.reference()) != checksum(ref):
        raise ValueError("Untrusted JSON or another version is not the captured control execution")
    return value.as_dict()


def capture_legacy(objects, *, session_id, execution_ref):
    """Read exact-version original objects supplied by the trusted S3 collector."""
    text(session_id, r"staging-(?!v2-)[A-Za-z0-9-]{1,64}")
    reference(execution_ref, key=f"executions/{session_id}.json")
    fields(objects, {"session", "execution", "pointer", "inputs"})
    keys = dict(session="automation/staging/session.json", execution=f"executions/{session_id}.json",
                pointer="control/staging/current-execution.json", inputs=f"private/staging/{session_id}/inputs.json")
    decoded, refs = {}, {}
    for name, obj in objects.items():
        fields(obj, {"reference", "body"})
        decoded[name] = decode_object(obj["body"], obj["reference"], key=keys[name], max_bytes=1024 * 1024)
        refs[name] = deepcopy(obj["reference"])
    if checksum(refs["execution"]) != checksum(execution_ref):
        raise ValueError("Legacy execution differs from the reviewed exact version")
    session = read_legacy(objects["session"]["body"])
    execution = read_legacy(objects["execution"]["body"])
    for record in (session, execution):
        if record["sessionId"] != session_id: raise ValueError("Legacy session mismatch")
    fields(execution.get("sourceRevisions"), SOURCES)
    for sha in execution["sourceRevisions"].values(): text(sha, r"[a-f0-9]{40}")
    integer(execution.get("maximumHours"), minimum=3, maximum=24)
    if (session.get("executionVersion") != execution_ref["versionId"]
            or any(key not in session or key not in execution or checksum(session[key]) != checksum(execution[key]) for key in IMMUTABLE)):
        raise ValueError("Legacy immutable scope changed")
    fields(decoded["pointer"], {"sessionId", "executionVersion"})
    if decoded["pointer"] != dict(sessionId=session_id, executionVersion=execution_ref["versionId"]):
        raise ValueError("Legacy pointer differs from the execution")
    inputs = decoded["inputs"]
    if type(inputs.get("schemaVersion")) is not int or inputs["schemaVersion"] != 1 or inputs.get("sessionId") != session_id:
        raise ValueError("Private inputs differ from the legacy session")
    dispatch = session.get("lifecycleDispatch")
    if (not isinstance(dispatch, dict) or dispatch.get("phase") not in ("PREPARED", "CALLING", "OBSERVED")):
        raise ValueError("Original legacy dispatch checkpoint required")
    text(dispatch.get("id"), r"[a-f0-9]{64}")
    fields(dispatch.get("operation"), {"mode", "stage", "attempt"})
    # Read-only use of the preserved v1 operation whitelist; never launch v1.
    from lifecycle_dispatch import operation
    operation(**dispatch["operation"])
    service_time(dispatch.get("createdAt"))
    if dispatch["phase"] == "OBSERVED":
        text(dispatch.get("buildId"), r"techx-automation-lifecycle:[a-f0-9-]+")
    elif dispatch.get("buildId") is not None:
        raise ValueError("Unknown dispatch cannot invent a build identity")
    return LegacySnapshot(dict(account=ACCOUNT, region=REGION, sessionId=session_id, executionVersion=execution_ref["versionId"],
                               references=refs, immutableScopeSha256=checksum({k: execution[k] for k in IMMUTABLE}),
                               sourceRevisions=deepcopy(execution["sourceRevisions"]), originalStatus=session["status"],
                               originalDispatch=deepcopy(dispatch)), _seal=_SNAPSHOT_SEAL)


def control_reference(value, migration_id):
    text(migration_id, r"migration-v1-[a-z0-9-]{1,60}")
    return reference(value, key=f"migration/control/{migration_id}/execution.json")


def read_control_execution(body, ref, snapshot, *, authorization_body, authorization_ref):
    legacy = snapshot_value(snapshot)
    key = ref.get("key") if isinstance(ref, dict) else None
    text(key, r"migration/control/migration-v1-[a-z0-9-]{1,60}/execution\.json")
    value = decode_object(body, ref, key=key)
    fields(value, {"schemaVersion", "executorBackend", "purpose", "scope", "migrationId", "account", "region", "windowId",
                   "authorizationReference", "legacyReferences", "legacySnapshotSha256", "sourceRevisions", "approvedBudgetUsd",
                   "startedAt", "cleanupStartAt", "deadline", "configurationReference", "bundleReference"})
    control_reference(ref, value["migrationId"])
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["executorBackend"] != "ec2-ssm"
            or value["purpose"] != "legacy-migration" or value["scope"] != "control-only"
            or value["account"] != ACCOUNT or value["region"] != REGION
            or checksum(value["legacyReferences"]) != checksum(legacy["references"])
            or value["legacySnapshotSha256"] != checksum(legacy)):
        raise ValueError("Migration execution changed scope or original snapshot provenance")
    fields(value["sourceRevisions"], SOURCES)
    for sha in value["sourceRevisions"].values(): text(sha, r"[a-f0-9]{40}")
    cost_window(value)
    window_reference(value["authorizationReference"], value["windowId"])
    reference(value["configurationReference"], key="migration/control/config/executor.json")
    digest = value["bundleReference"].get("sha256") if isinstance(value["bundleReference"], dict) else None
    text(digest, r"[a-f0-9]{64}")
    reference(value["bundleReference"], key=f"migration/control/bundles/{digest}.tar.gz")
    if ref["sha256"] != checksum(value): raise ValueError("Control execution must use canonical private JSON")
    bind_authorized_window(value, authorization_body, authorization_ref, purpose="legacy-migration")
    return MigrationExecution(value, ref, _seal=_SNAPSHOT_SEAL)


def direct_denial_binding(event, snapshot, *, now):
    """Prove one exact service denial, NOT complete call history or no-build state."""
    legacy = snapshot_value(snapshot)
    dispatch = legacy["originalDispatch"]
    run = re.fullmatch(r"staging-gh-([1-9][0-9]*)", legacy["sessionId"])
    if not run or dispatch["phase"] != "CALLING" or dispatch.get("buildId") is not None:
        raise ValueError("Direct rejection binding requires the original unknown GitHub dispatch")
    principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-{run[1]}"
    if (not isinstance(event, dict) or event.get("eventSource") != "codebuild.amazonaws.com" or event.get("eventName") != "StartBuild"
            or event.get("awsRegion") != REGION or event.get("recipientAccountId") != ACCOUNT
            or event.get("userIdentity", {}).get("arn") != principal or event.get("userIdentity", {}).get("type") != "AssumedRole"
            or event.get("userIdentity", {}).get("sessionContext", {}).get("sessionIssuer", {}).get("arn") != f"arn:aws:iam::{ACCOUNT}:role/techx-automation-lifecycle-dispatcher"
            or event.get("errorCode") != "AccountLimitExceededException"
            or event.get("responseElements") is not None):
        raise ValueError("No matching positive service denial")
    for key in ("eventID", "requestID"): text(event.get(key), r"[A-Za-z0-9-]{8,128}")
    # CloudTrail timestamps have second precision; do not order them before a
    # fractional S3 dispatch timestamp solely due to that precision difference.
    if not service_time(dispatch["createdAt"]).replace(microsecond=0) <= service_time(event.get("eventTime")) <= now:
        raise ValueError("Service event predates dispatch or is in the future")
    params = event.get("requestParameters")
    fields(params, {"projectName", "idempotencyToken", "environmentVariablesOverride"})
    if params["projectName"] != "techx-automation-lifecycle" or params["idempotencyToken"] != dispatch["id"]:
        raise ValueError("Redacted/mismatched token cannot bind the original dispatch")
    op = dispatch["operation"]
    expected = dict(TECHX_SESSION_ID=legacy["sessionId"], TECHX_EXECUTION_VERSION=legacy["executionVersion"],
                    TECHX_MODE=op["mode"], TECHX_PLAN_STAGE=op["stage"], TECHX_PLAN_ATTEMPT=str(op["attempt"]))
    variables = params["environmentVariablesOverride"]
    if not isinstance(variables, list) or len(variables) != len(expected):
        raise ValueError("Complete non-redacted environment is required")
    seen = {}
    for row in variables:
        fields(row, {"name", "value", "type"})
        if not isinstance(row["name"], str) or row["name"] in seen or row["type"] != "PLAINTEXT":
            raise ValueError("Invalid/duplicate environment binding")
        seen[row["name"]] = row["value"]
    if seen != expected: raise ValueError("Redacted/mismatched environment cannot prove dispatch identity")
    return dict(classification="EXACT_SERVICE_DENIAL_ONLY", eventId=event["eventID"], requestId=event["requestID"],
                eventSha256=checksum(event), dispatchId=dispatch["id"], environmentSha256=checksum(expected))


def read_manifest(body, ref, control, control_ref, snapshot):
    legacy = snapshot_value(snapshot)
    control = control_value(control, control_ref)
    control_reference(control_ref, control["migrationId"])
    value = decode_object(body, ref, key=f"migration/control/{control['migrationId']}/manifest.json")
    fields(value, {"schemaVersion", "migrationId", "controlExecutionReference", "legacySnapshotSha256", "sourceRevisions",
                   "windowId", "authorizationReference", "policy", "retiredPrincipals", "disabledTriggers", "reviewReference"})
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["migrationId"] != control["migrationId"]
            or checksum(value["controlExecutionReference"]) != checksum(control_ref) or value["legacySnapshotSha256"] != checksum(legacy)
            or value["sourceRevisions"] != control["sourceRevisions"] or value["windowId"] != control["windowId"]
            or value["authorizationReference"] != control["authorizationReference"]
            or value["policy"] not in ("REQUIRE_DIRECT_DENIAL", "RETIRE_UNKNOWN_DISPATCH")):
        raise ValueError("Manifest changed the reviewed control scope/policy")
    roles = value["retiredPrincipals"]
    triggers = value["disabledTriggers"]
    for items in (roles, triggers):
        if not isinstance(items, list) or not 1 <= len(items) <= 128 or any(not isinstance(i, str) for i in items) or len(set(items)) != len(items):
            raise ValueError("Bounded explicit nonempty retirement inventory required")
    for arn in roles: text(arn, rf"arn:aws:iam::{ACCOUNT}:role/techx-automation-(?!v2(?:-|$))[a-z0-9-]+")
    for arn in triggers:
        text(arn, rf"arn:aws:(?:lambda:{REGION}:{ACCOUNT}:function:techx-automation-(?!v2(?:-|$))[a-z0-9-]+|"
                  rf"events:{REGION}:{ACCOUNT}:rule/techx-automation-(?!v2(?:-|$))[a-z0-9-]+|"
                  rf"scheduler:{REGION}:{ACCOUNT}:schedule/techx-automation/(?!v2-|staging-v2-|cleanup-staging-v2-)[a-zA-Z0-9-]+)")
    reference(value["reviewReference"], key=f"migration/control/{control['migrationId']}/review.json")
    return value


def read_closeout(body, ref, control, control_ref, snapshot, manifest, manifest_ref, *, now):
    """Wire validation only. R02 verifier must check every referenced fact first.

    This return value is never staging admission authority by itself. A reviewed
    retirement of unknown authority is distinct from proving rejected creation;
    neither branch rewrites a v1 session or produces a v1 completion marker.
    """
    legacy = snapshot_value(snapshot)
    if manifest_ref["sha256"] != checksum(manifest): raise ValueError("Manifest differs from its canonical private version")
    # Recheck the full manifest, not just a caller-supplied policy selector.
    manifest = read_manifest(json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
                             manifest_ref, control, control_ref, snapshot)
    control = control_value(control, control_ref)
    component = quote(legacy["executionVersion"], safe="")
    value = decode_object(body, ref, key=f"migration/v1/{legacy['sessionId']}/{component}/closeout.json")
    fields(value, {"schemaVersion", "account", "region", "migrationId", "controlExecutionReference", "manifestReference",
                   "legacyReferences", "legacySnapshotSha256", "originalDispatchId", "originalPhase", "outcome", "evidenceReferences", "sealedAt"})
    outcome = {"REQUIRE_DIRECT_DENIAL": "REJECTED_BEFORE_BUILD_CREATION", "RETIRE_UNKNOWN_DISPATCH": "UNKNOWN_DISPATCH_AUTHORITY_RETIRED"}
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["account"] != ACCOUNT or value["region"] != REGION
            or value["migrationId"] != control["migrationId"] or value["controlExecutionReference"] != control_ref
            or value["manifestReference"] != manifest_ref or checksum(value["legacyReferences"]) != checksum(legacy["references"])
            or value["legacySnapshotSha256"] != checksum(legacy) or value["originalDispatchId"] != legacy["originalDispatch"]["id"]
            or value["originalPhase"] != legacy["originalDispatch"]["phase"] or value["originalPhase"] != "CALLING"
            or value["outcome"] != outcome.get(manifest["policy"])):
        raise ValueError("Closeout changed original history or the explicitly reviewed reconciliation branch")
    evidence = value["evidenceReferences"]
    names = {"call-history", "dispatch-binding", "authority-retirement", "actors", "workload", "recovery-owner", "worker-terminal"}
    fields(evidence, names)
    for name in names:
        reference(evidence[name], key=f"migration/control/{control['migrationId']}/evidence/{name}.json")
    if not timestamp(control["startedAt"]) <= timestamp(value["sealedAt"]) <= now:
        raise ValueError("Migration seal predates its admitted execution or is in the future")
    return value
