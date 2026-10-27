"""Active public evidence is bound to a rollout, never to the old cold runtime."""
from copy import deepcopy
import re

from active_release_contract import release_context, validate_rollout_checkpoint
from automation_session import utc
from plan_approval import version
from plan_review_store import checksum, read_json
from public_acceptance_contract import REPO, validate_probe_result


def active_public_context(record):
    if record.get("status") != "TESTING": raise ValueError("Active public checks require TESTING")
    rollout = validate_rollout_checkpoint(record)
    return dict(releaseContext=release_context(record), rollout=deepcopy(rollout))


def active_result_key(session, release_id, dispatch):
    if not re.fullmatch(r"[a-f0-9]{64}", release_id) or not re.fullmatch(r"[a-f0-9]{64}", dispatch):
        raise ValueError("Exact active release and public dispatch identities required")
    return f"acceptance/staging/{session}/active/{release_id}/public/{dispatch}/result.json"


def validate_active_public_checkpoint(record):
    stage = record.get("releaseCandidate", {}).get("publicAcceptance", {})
    ref = stage.get("receiptReference", {})
    if (stage.get("state") != "PASSED" or stage.get("attempt") != 1
            or stage.get("contextSha256") != checksum(active_public_context(record))
            or not re.fullmatch(r"[a-f0-9]{64}", stage.get("dispatchId", ""))
            or not re.fullmatch(rf"https://github.com/{REPO}/actions/runs/[1-9][0-9]*", stage.get("workflowRun", ""))
            or not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != active_result_key(record["sessionId"], record["releaseCandidate"]["id"], stage["dispatchId"])
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Active public acceptance lacks its exact rollout/dispatch receipt")
    version(ref.get("versionId"))
    return stage


def read_active_public_receipt(client, bucket, record, execution_version):
    stage = validate_active_public_checkpoint(record)
    ref = stage["receiptReference"]
    value, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    expected = dict(schemaVersion=1, kind="active-public-acceptance", sessionId=record["sessionId"], executionVersion=execution_version,
        contextSha256=stage["contextSha256"], dispatchId=stage["dispatchId"], workflowRun=stage["workflowRun"],
        releaseId=record["releaseCandidate"]["id"], rolloutReference=record["releaseCandidate"]["rollout"]["receiptReference"])
    if (not isinstance(value, dict) or set(value) != set(expected) | {"runner", "result", "completedAt"}
            or checksum(value) != ref["sha256"] or any(value.get(key) != item for key, item in expected.items())
            or not utc(record["createdAt"]) <= utc(value.get("completedAt", "")) < utc(record["cleanupStartAt"])):
        raise ValueError("Active public receipt differs from this exact rollout")
    validate_probe_result(value["runner"], value["result"])
    return value
