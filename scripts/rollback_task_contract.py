"""Fixed private rollback preparation task and immutable intent/proposal receipts."""
from copy import deepcopy
import re

from active_release_contract import reference
from chart_selection import CHART_REPO, sha
from plan_review_store import checksum
from ready_release_contract import read_reference
from rollback_proposal import validate_intent

BASE = {"schemaVersion", "phase", "attempt", "intent", "contextSha256", "origin"}
PREPARED = {"intentReference", "proposalReference", "prepareTaskId"}
APPROVED = PREPARED | {"approvalReference", "preparation"}


def key(record, name):
    if name not in ("intent", "proposal", "rollout"): raise ValueError("Unsupported rollback artifact")
    return f"private/staging/{record['sessionId']}/rollback/{name}.json"


def slot(record):
    value = record.get("rollbackAttempt", {})
    if not isinstance(value, dict): raise ValueError("Rollback reservation must be an object")
    extras = {"REQUESTED": set(), "PREPARING": {"intentReference"},
              "PROPOSED": PREPARED, "APPROVED": APPROVED,
              "RESTORED": APPROVED | {"rolloutTaskId", "rolloutReference"}}.get(value.get("phase"))
    if extras is not None and value.get("phase") == "RESTORED":
        extras = extras | ({"publicAcceptance", "privateAcceptance", "restoration"} & set(value))
    if (not isinstance(value, dict) or extras is None or set(value) != BASE | extras
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or type(value.get("attempt")) is not int or value["attempt"] != 1
            or not isinstance(value.get("origin"), dict) or set(value["origin"]) != {"lifecycleDispatch", "deploymentTask", "workflowHandoff"}
            or checksum(dict(intent=value.get("intent"), origin=value.get("origin"))) != value.get("contextSha256")):
        raise ValueError("Rollback has no exact unaltered preparation reservation")
    intent = validate_intent(value["intent"])
    from partial_rollout import failed_reference
    if (record.get("status") != "FAILED_NEEDS_CLEANUP" or record.get("action") != "deploy-window-rollback"
            or record.get("pendingPlan") is not None or intent["action"] != record["action"]
            or intent["sessionId"] != record.get("sessionId") or intent["executionVersion"] != record.get("executionVersion")
            or intent["acceptedBaseline"] != record.get("acceptedRelease")
            or intent["failedReleaseId"] != record.get("releaseCandidate", {}).get("id")
            or intent["rolloutReference"] != failed_reference(record)
            or any(intent[name] != record.get(name) for name in ("cleanupStartAt", "deadline"))):
        raise ValueError("Rollback task changed its failed candidate, accepted baseline or window")
    for name in extras & {"intentReference", "proposalReference", "rolloutReference"}: reference(value[name], key(record, name.removesuffix("Reference")))
    if "approvalReference" in extras:
        approval = value["approvalReference"]
        prefix = re.escape(f"promotion-approvals/staging/{record['sessionId']}/")
        if not isinstance(approval, dict) or not re.fullmatch(prefix + r"[a-f0-9]{64}\.json", approval.get("key", "")):
            raise ValueError("Rollback has no exact session-scoped promotion approval")
        reference(approval, approval["key"])
        preparation = value["preparation"]
        if (not isinstance(preparation, dict) or set(preparation) != {"lifecycleDispatch", "deploymentTask", "workflowHandoff"}
                or any(not isinstance(preparation.get(name), dict) for name in ("lifecycleDispatch", "deploymentTask", "workflowHandoff"))
                or preparation["deploymentTask"].get("taskId") != value["prepareTaskId"]
                or preparation["deploymentTask"].get("state") != "SUCCEEDED"
                or preparation["lifecycleDispatch"].get("operation") != dict(mode="rollback", stage="prepare", attempt=1)
                or preparation["lifecycleDispatch"].get("phase") != "OBSERVED"
                or preparation["workflowHandoff"].get("operation") != "review-rollback"
                or preparation["workflowHandoff"].get("phase") != "CONSUMED"):
            raise ValueError("Rollback approval lost its original prepared task or consumed review")
    if "restoration" in extras:
        restored = value["restoration"]
        if (not isinstance(restored, dict) or set(restored) != {"lifecycleDispatch", "deploymentTask", "workflowHandoff"}
                or any(not isinstance(restored.get(name), dict) for name in restored)
                or restored["deploymentTask"].get("taskId") != value["rolloutTaskId"] or restored["deploymentTask"].get("state") != "SUCCEEDED"
                or restored["lifecycleDispatch"].get("operation") != dict(mode="rollback", stage="rollout", attempt=1)
                or restored["workflowHandoff"].get("operation") != "accept-rollback-public" or restored["workflowHandoff"].get("phase") != "CONSUMED"
                or value.get("publicAcceptance", {}).get("state") != "PASSED"):
            raise ValueError("Rollback lost its terminal restore and consumed public checkpoint")
    return value


def context(record):
    value = slot(record)
    return dict(rollbackId=value["intent"]["rollbackId"], intentSha256=checksum(value["intent"]), reservationSha256=value["contextSha256"])


def original_record(record):
    value = slot(record)
    result = deepcopy(record)
    result.update(value["origin"])
    result.pop("rollbackAttempt"); result.pop("lease", None)
    return result


def proposal_record(record):
    """Read-only projection of the exact task that produced the reviewed PR."""
    value = validate_prepare_checkpoint(record)
    result = deepcopy(record)
    if value["phase"] != "PROPOSED":
        result.update(deepcopy(value["preparation"]))
        result["rollbackAttempt"] = {name: deepcopy(value[name]) for name in BASE | PREPARED}
        result["rollbackAttempt"]["phase"] = "PROPOSED"
    result.pop("lease", None)
    return result


def rollout_context(record):
    value = slot(record)
    if value["phase"] not in ("APPROVED", "RESTORED"): raise ValueError("Rollback rollout requires its consumed grant")
    return dict(context(record), proposalReference=deepcopy(value["proposalReference"]),
        approvalReference=deepcopy(value["approvalReference"]), preparationSha256=checksum(value["preparation"]))


def validate_rollout_start(record, attempt):
    value = slot(record)
    if (attempt != 1 or value["phase"] != "APPROVED"
            or record.get("deploymentTask") != value["preparation"]["deploymentTask"]
            or record.get("workflowHandoff") != value["preparation"]["workflowHandoff"]):
        raise ValueError("Rollback rollout has already consumed its prepared predecessor")
    return rollout_context(record)


def validate_rollout_checkpoint(record, task=None):
    value = slot(record)
    if (value["phase"] != "RESTORED" or not re.fullmatch(r"[a-f0-9]{32}", value.get("rolloutTaskId", ""))
            or task is not None and (task.get("taskId") != value["rolloutTaskId"] or task.get("expectedRollbackContext") != rollout_context(record))):
        raise ValueError("Rollback has no exact restored runtime task checkpoint")
    return value


def validate_prepare_start(record, attempt):
    value = slot(record)
    if (attempt != 1 or value["phase"] != "REQUESTED"
            or record.get("deploymentTask") != value["origin"]["deploymentTask"]
            or record.get("workflowHandoff") != value["origin"]["workflowHandoff"]):
        raise ValueError("Rollback preparation has already consumed its original predecessor")
    return context(record)


def validate_prepare_checkpoint(record, task=None):
    value = slot(record)
    if (value["phase"] not in ("PROPOSED", "APPROVED", "RESTORED") or not re.fullmatch(r"[a-f0-9]{32}", value.get("prepareTaskId", ""))
            or task is not None and (task.get("taskId") != value["prepareTaskId"] or task.get("expectedRollbackContext") != context(record))):
        raise ValueError("Rollback task has no exact versioned proposal checkpoint")
    return value


def read_proposal_receipt(client, bucket, record, execution_version):
    value = validate_prepare_checkpoint(record)
    intent = read_reference(client, bucket, value["intentReference"], key(record, "intent"))
    if intent != value["intent"]: raise ValueError("Immutable private rollback intent changed")
    receipt = read_reference(client, bucket, value["proposalReference"], key(record, "proposal"))
    expected = dict(schemaVersion=1, kind="private-rollback-proposal", sessionId=record["sessionId"], executionVersion=execution_version,
        taskId=value["prepareTaskId"], rollbackContext=context(record), intentReference=value["intentReference"])
    if (not isinstance(receipt, dict) or set(receipt) != set(expected) | {"proposal"}
            or any(receipt.get(name) != item for name, item in expected.items())):
        raise ValueError("Private rollback proposal belongs to another task or intent")
    proposal = receipt["proposal"]
    if (not isinstance(proposal, dict) or set(proposal) != {"schemaVersion", "kind", "rollbackId", "branch", "baselineSha", "headSha", "treeSha", "acceptedBaseline", "prNumber", "pullRequest"}
            or proposal.get("schemaVersion") != 1 or proposal.get("kind") != "baseline-rollback-proposal"
            or proposal.get("rollbackId") != intent["rollbackId"] or proposal.get("acceptedBaseline") != intent["acceptedBaseline"]
            or proposal.get("baselineSha") != intent["failedChartSha"]
            or proposal.get("branch") != f"rollback/{record['sessionId']}-{intent['rollbackId'][:24]}"
            or type(proposal.get("prNumber")) is not int or proposal["prNumber"] < 1
            or proposal.get("pullRequest") != f"https://github.com/{CHART_REPO}/pull/{proposal['prNumber']}"):
        raise ValueError("Rollback receipt has no exact reviewable baseline-restoration PR")
    sha(proposal.get("headSha")); sha(proposal.get("treeSha"))
    return receipt
