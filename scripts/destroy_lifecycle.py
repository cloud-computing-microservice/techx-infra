"""Request independent cleanup for one immutable session; never run Terraform here.

Cleanup remains available when deployment flags are disabled or its deadline has
passed. GitHub only requests/observes the AWS-owned cleanup protocol and cannot
write session state, manufacture completion evidence or start deployment work.
"""
import argparse
import json
import os
import re
import time

from automation_session import validate
from chart_selection import GitHub, INFRA_REPO, sha
from cleanup_execution import ACCOUNT, CLEANUP_ROLE
from cleanup_completion import checkpoint_inventory, completion
from lifecycle_dispatch import CONTEXT
from plan_approval import version
from plan_review_store import encoded, read_json

WORKFLOW = ".github/workflows/staging-destroy.yml"
LAUNCHER = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher"
PROJECT = "techx-automation-cleanup"
BUILD = rf"{PROJECT}:[a-f0-9-]+"


class DispatchUncertain(ValueError):
    """Do not automatically invoke again after an ambiguous API response."""


def verified_caller(github, run_id, source_sha):
    sha(source_sha)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)):
        raise ValueError("A real cleanup workflow run is required")
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("status") != "in_progress"
            or run.get("event") != "workflow_dispatch" or type(run.get("run_attempt")) is not int
            or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != source_sha or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
            or any(run.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Cleanup requires a first-attempt dispatch from trusted infra main")
    if github.get("git/ref/heads/main").get("object", {}).get("sha") != source_sha:
        raise ValueError("Cleanup caller must use current main; admitted workload source may be older")


def current_session(s3, bucket, execution, execution_version):
    current, _ = read_json(s3, bucket, "automation/staging/session.json")
    validate(current)
    validate(execution)
    if (current.get("executionVersion") != execution_version or current["status"] == "OFF"
            or any(current.get(key) != execution.get(key) for key in CONTEXT)):
        raise ValueError("Cleanup cannot target a stale, inactive or different staging execution")
    return current


def observed_build(codebuild, build_id, execution, execution_version, bucket):
    response = codebuild.batch_get_builds(ids=[build_id])
    rows = response.get("builds", [])
    if response.get("buildsNotFound") or len(rows) != 1:
        raise ValueError("Cleanup build observation is missing or ambiguous")
    build = rows[0]
    kms = build.get("encryptionKey", "")
    if not re.fullmatch(rf"arn:aws:kms:us-east-1:{ACCOUNT}:key/[a-f0-9-]{{36}}", kms):
        raise ValueError("Cleanup build has no scoped KMS encryption identity")
    expected = dict(TECHX_SESSION_ID=execution["sessionId"], TECHX_EXECUTION_VERSION=execution_version,
                    TECHX_STATE_BUCKET=bucket, TECHX_STATE_KMS_KEY_ARN=kms)
    env = [item for item in build.get("environment", {}).get("environmentVariables", [])
           if item.get("name", "").startswith("TECHX_")]
    if (build.get("id") != build_id or build.get("arn") != f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/{build_id}"
            or build.get("projectName") != PROJECT or build.get("serviceRole") != CLEANUP_ROLE
            or len(env) != 4 or {item["name"]: item.get("value") for item in env} != expected
            or [item.get("value") for item in build.get("environment", {}).get("environmentVariables", [])
                if item.get("name") == "CONTROL_BUCKET"] != [bucket]
            or any(item.get("type") != "PLAINTEXT" for item in env)):
        raise ValueError("Cleanup build belongs to a different scope, role or execution")
    return build


def request_cleanup(clients, github, *, bucket, session_id, execution_version, run_id, source_sha,
                    observations=240, sleep=time.sleep, monotonic=time.monotonic):
    if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session_id)):
        raise ValueError("A fixed control bucket and staging session are required")
    version(execution_version)
    if type(observations) is not int or not 1 <= observations <= 240:
        raise ValueError("Cleanup observation must be bounded to 40 minutes")
    identity = clients["sts"].get_caller_identity()
    if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
            f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-destroy-dispatcher/techx-destroy-{run_id}"):
        raise ValueError("Only this protected cleanup dispatcher's exact run identity is accepted")
    verified_caller(github, run_id, source_sha)
    execution, _ = read_json(clients["s3"], bucket, f"executions/{session_id}.json", execution_version)
    if execution.get("sessionId") != session_id: raise ValueError("Execution belongs to a different session")
    current = current_session(clients["s3"], bucket, execution, execution_version)
    completed = completion(clients["s3"], bucket, execution, execution_version, current)
    if completed: return completed
    if current["status"] == "DESTROYED":
        # The dispatcher remains read-only. Only the independently scoped
        # cleanup producer can recover its marker from a valid committed proof.
        checkpoint_inventory(clients["s3"], bucket, execution, execution_version, current, metadata=True)
    # Read again immediately before the only mutation. The AWS cleanup worker
    # also checks current session before fencing or touching workload resources.
    current_session(clients["s3"], bucket, execution, execution_version)
    try:
        response = clients["lambda"].invoke(FunctionName=LAUNCHER, InvocationType="RequestResponse",
            Payload=encoded(dict(sessionId=session_id, executionVersion=execution_version, requestedEarly=True)))
        if response.get("StatusCode") != 200 or response.get("FunctionError"):
            raise ValueError("Cleanup launcher did not return a verified response")
        payload = response["Payload"].read(16 * 1024 + 1)
        if len(payload) > 16 * 1024: raise ValueError("Cleanup response exceeds policy")
        result = json.loads(payload)
        if result.get("sessionId") != session_id or result.get("status") not in ("started", "running", "already-destroyed"):
            raise ValueError("Unrecognized cleanup launcher result")
        if result["status"] == "already-destroyed":
            latest = current_session(clients["s3"], bucket, execution, execution_version)
            completed = completion(clients["s3"], bucket, execution, execution_version, latest)
            if not completed: raise ValueError("Launcher completion is missing inventory evidence")
            return completed
        build_id = result.get("buildId", "")
        if not re.fullmatch(BUILD, build_id): raise ValueError("Cleanup build handle is invalid")
    except Exception as error:
        raise DispatchUncertain("Cleanup dispatch was not positively observed; keep session/version and reconcile the AWS launcher/build. No automatic reinvocation.") from error
    end = monotonic() + 2400
    for _ in range(observations):
        if monotonic() >= end: break
        latest = current_session(clients["s3"], bucket, execution, execution_version)
        build = observed_build(clients["codebuild"], build_id, execution, execution_version, bucket)
        if build.get("buildStatus") == "SUCCEEDED" and build.get("buildComplete") is True:
            latest = current_session(clients["s3"], bucket, execution, execution_version)
            completed = completion(clients["s3"], bucket, execution, execution_version, latest)
            if not completed: raise ValueError("Cleanup build succeeded without protected inventory evidence")
            return dict(completed, buildId=build_id)
        if build.get("buildStatus") != "IN_PROGRESS" or build.get("buildComplete") is not False:
            raise ValueError("Cleanup build failed or returned an unknown state; preserve diagnostics and execution inputs")
        sleep(max(0, min(10, end - monotonic())))
    return dict(sessionId=session_id, executionVersion=execution_version, buildId=build_id,
                waiting="CLEANUP_RUNNING", inventoryVerified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    args = parser.parse_args()
    import boto3
    from botocore.config import Config
    # Never let the SDK automatically repeat an ambiguous mutation.
    config = Config(connect_timeout=10, read_timeout=120, retries={"total_max_attempts": 1})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("sts", "s3", "lambda", "codebuild")}
    github = GitHub(INFRA_REPO, os.environ["GITHUB_TOKEN"])
    try:
        result = request_cleanup(clients, github, bucket=args.bucket, session_id=args.session_id,
            execution_version=args.execution_version, run_id=os.environ["GITHUB_RUN_ID"], source_sha=os.environ["GITHUB_SHA"])
    except DispatchUncertain:
        result = dict(sessionId=args.session_id, executionVersion=args.execution_version,
                      waiting="DISPATCH_UNCERTAIN", inventoryVerified=False)
        write_summary(result)
        raise SystemExit("Cleanup dispatch is uncertain; preserve this execution and inspect AWS cleanup before retrying.")
    write_summary(result)


def write_summary(result):
    print(json.dumps(result, sort_keys=True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("### Independent cleanup observation\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__": main()
