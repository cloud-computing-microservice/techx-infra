"""Versioned public acceptance evidence, separate from private/VPN readiness."""
from copy import deepcopy
import re

from automation_session import utc
from deployment_tasks import validate_exposure_checkpoint
from plan_approval import version
from plan_review_store import checksum, read_json
from public_probe import CHECKS

REPO = "cloud-computing-microservice/techx-infra"
OWNER = r"arn:aws:sts::058114477594:assumed-role/techx-automation-public-acceptance/techx-public-[1-9][0-9]*"


def public_context(record):
    validate_exposure_checkpoint(record)
    return {key: deepcopy(record[key]) for key in ("bootstrapChart", "bootstrapRelease")} | dict(
        exposure=deepcopy(record["stages"]["exposure"]))


def result_key(session, dispatch):
    if not re.fullmatch(r"[a-f0-9]{64}", dispatch): raise ValueError("Invalid public acceptance dispatch")
    return f"acceptance/staging/{session}/public/{dispatch}/result.json"


def validate_public_checkpoint(record):
    stage = record.get("stages", {}).get("publicAcceptance", {})
    ref = stage.get("receiptReference", {})
    if (stage.get("state") != "PASSED" or stage.get("attempt") != 1
            or stage.get("contextSha256") != checksum(public_context(record))
            or not re.fullmatch(r"[a-f0-9]{64}", stage.get("dispatchId", ""))
            or not re.fullmatch(rf"https://github.com/{REPO}/actions/runs/[1-9][0-9]*", stage.get("workflowRun", ""))
            or not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != result_key(record["sessionId"], stage["dispatchId"])
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Public acceptance has no exact completed checkpoint")
    version(ref.get("versionId"))
    return stage


def read_public_receipt(client, bucket, record, execution_version):
    stage = validate_public_checkpoint(record)
    ref = stage["receiptReference"]
    value, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    expected = dict(schemaVersion=1, kind="public-acceptance", sessionId=record["sessionId"], executionVersion=execution_version,
        contextSha256=stage["contextSha256"], dispatchId=stage["dispatchId"], workflowRun=stage["workflowRun"])
    if (not isinstance(value, dict) or set(value) != set(expected) | {"runner", "result", "completedAt"}
            or checksum(value) != ref["sha256"] or any(value.get(key) != val for key, val in expected.items())
            or not utc(record["createdAt"]) <= utc(value["completedAt"]) < utc(record["cleanupStartAt"])):
        raise ValueError("Public acceptance receipt differs from this immutable execution")
    validate_probe_result(value["runner"], value["result"])
    return value


def validate_probe_result(runner, result):
    if (not isinstance(runner, dict) or set(runner) != {"jobId", "runnerId", "runnerGroup", "labels"}
            or type(runner["jobId"]) is not int or runner["jobId"] < 1 or type(runner["runnerId"]) is not int or runner["runnerId"] < 1
            or runner["runnerGroup"] != "GitHub Actions" or runner["labels"] != ["ubuntu-24.04"]
            or not isinstance(result, dict) or result.get("status") != "passed" or result.get("networkView") != "public"
            or result.get("checks") != sorted(CHECKS) or result.get("private") != "pending" or result.get("vpn") != "pending"
            or not isinstance(result.get("observations"), list) or not 1 <= len(result["observations"]) <= 40):
        raise ValueError("Public probe did not prove the complete hosted-runner public suite")
