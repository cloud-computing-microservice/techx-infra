"""Protected GitHub continuation of an existing admitted staging session.

This entry point cannot open a paid session, choose a stage, or accept a boolean
as plan approval. A bounded observation timeout retains the same build handle.
"""
import argparse
import json
import os
import re
import time

from automation_session import S3SessionStore
from chart_selection import GitHub, INFRA_REPO
from lifecycle_coordinator import Coordinator
from lifecycle_dispatch import Dispatcher, observed_build
from plan_approval import version
from plan_review_store import ACCOUNT, read_json

WORKFLOW = ".github/workflows/staging-continue.yml"


def verified_caller(github, run_id, execution):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real lifecycle workflow run is required")
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("status") != "in_progress" or run.get("event") != "workflow_dispatch"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
            or any(run.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Continuation must run once from trusted main at the exact admitted infra SHA")


def drive(dispatcher, coordinator, *, resume_approved=False, observations=240, sleep=time.sleep, monotonic=time.monotonic):
    if type(observations) is not int or not 1 <= observations <= 240:
        raise ValueError("Continuation observation must be bounded to 40 minutes")
    end = monotonic() + 2400
    for _ in range(observations):
        if monotonic() >= end: break
        record, _ = dispatcher.current(idle=False)
        build = observed_build(dispatcher.clients["codebuild"], record)
        if build.get("buildStatus") == "IN_PROGRESS" and build.get("buildComplete") is False:
            sleep(max(0, min(10, end - monotonic())))
            continue
        if build.get("buildStatus") != "SUCCEEDED" or build.get("buildComplete") is not True:
            dispatcher.cleanup()
            raise ValueError("Manager failed or returned an unknown terminal result; independent cleanup requested")
        if resume_approved:
            result = coordinator.resume_approved(record["sessionId"])
            resume_approved = False  # This run never consumes a later review.
        else:
            result = dispatcher.advance()
        if result.get("waiting"):
            if result["waiting"] == "PLAN_APPROVAL":
                current, _ = dispatcher.current()
                result["reviewReference"] = current["pendingPlan"]
            return result
    # Read the exact current pointer again. Never restart because observation
    # expired; an operator or future caller can continue this same AWS build.
    record, _ = dispatcher.current(idle=False)
    build = observed_build(dispatcher.clients["codebuild"], record)
    return dict(sessionId=record["sessionId"], executionVersion=dispatcher.version, waiting="OBSERVATION_LIMIT",
                buildId=build["id"], buildStatus=build.get("buildStatus"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--resume-approved", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", args.session_id): raise ValueError("Invalid lifecycle session")
    version(args.execution_version)
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "scheduler", "ssm", "codebuild", "lambda")}
    run_id = os.environ["GITHUB_RUN_ID"]
    identity = clients["sts"].get_caller_identity()
    if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
            f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-{run_id}"):
        raise ValueError("Only this protected lifecycle run's dispatcher role may continue")
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    if execution.get("sessionId") != args.session_id: raise ValueError("Immutable execution belongs to another session")
    verified_caller(GitHub(INFRA_REPO, os.environ["GITHUB_TOKEN"]), run_id, execution)
    store = S3SessionStore(args.bucket)
    dispatcher = Dispatcher(clients, store, args.bucket, execution, args.execution_version)
    # Stale identity/session/deadline fails before entering any cleanup handler.
    dispatcher.current(idle=False)
    result = drive(dispatcher, Coordinator(clients, store, args.bucket), resume_approved=args.resume_approved)
    print(json.dumps(result, sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("### Lifecycle continuation\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__": main()
