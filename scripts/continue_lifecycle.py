"""Protected GitHub continuation of an existing admitted staging session.

This entry point cannot open a paid session, choose a stage, or accept a boolean
as plan approval. A bounded observation timeout retains the same build handle.
"""
import argparse
import json
import math
import os
import re
import time
import tempfile
from pathlib import Path

from automation_session import S3SessionStore
from chart_selection import GitHub, INFRA_REPO
from lifecycle_coordinator import Coordinator
from lifecycle_dispatch import Dispatcher, observed_build
from plan_approval import version
from plan_review_store import ACCOUNT, read_json

WORKFLOW = ".github/workflows/staging-continue.yml"
OPEN_WORKFLOW = ".github/workflows/staging-lifecycle.yml"


def verified_caller(github, run_id, execution):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real lifecycle workflow run is required")
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("status") != "in_progress" or run.get("event") != "workflow_dispatch"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or run.get("path") not in (WORKFLOW, WORKFLOW + "@main", OPEN_WORKFLOW, OPEN_WORKFLOW + "@main")
            or any(run.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Continuation must run once from trusted main at the exact admitted infra SHA")
    if run.get("path") in (OPEN_WORKFLOW, OPEN_WORKFLOW + "@main") and execution.get("sourceAdmission", {}).get("workflowRun") != f"https://github.com/{INFRA_REPO}/actions/runs/{run_id}":
        raise ValueError("Opening workflow may continue only the execution it admitted")


def observation_end(now, deadline=None):
    """Nested continuation may shorten, never renew, its caller's time budget."""
    if deadline is None: return now + 2400
    if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline > now + 2400:
        raise ValueError("Observation deadline must be finite and cannot extend forty minutes")
    return deadline


def drive(dispatcher, coordinator, *, resume_approved=False, observations=240, sleep=time.sleep, monotonic=time.monotonic,
          failure_disposition=None, observation_deadline=None):
    if type(observations) is not int or not 1 <= observations <= 240:
        raise ValueError("Continuation observation must be bounded to 40 minutes")
    started = monotonic()
    end = observation_end(started, observation_deadline)
    if end <= started:
        return dict(waiting="OBSERVATION_LIMIT", sessionId=dispatcher.execution["sessionId"], executionVersion=dispatcher.version)
    for _ in range(observations):
        if monotonic() >= end: break
        if failure_disposition is not None and dispatcher.store.read()[0].get("status") == "FAILED_NEEDS_CLEANUP":
            return failure_disposition()
        try:
            record, _ = dispatcher.current(idle=False, allow_ready=True)
        except ValueError:
            if failure_disposition is not None and dispatcher.store.read()[0].get("status") == "FAILED_NEEDS_CLEANUP":
                return failure_disposition()
            raise
        build = observed_build(dispatcher.clients["codebuild"], record)
        if build.get("buildStatus") == "IN_PROGRESS" and build.get("buildComplete") is False:
            sleep(max(0, min(10, end - monotonic())))
            continue
        if build.get("buildStatus") != "SUCCEEDED" or build.get("buildComplete") is not True:
            if failure_disposition is not None and dispatcher.store.read()[0].get("status") == "FAILED_NEEDS_CLEANUP":
                return failure_disposition()
            dispatcher.cleanup()
            raise ValueError("Manager failed or returned an unknown terminal result; independent cleanup requested")
        if record["status"] == "READY":
            result = dispatcher.advance()
        elif resume_approved:
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
    if observation_deadline is not None and monotonic() >= end:
        return dict(waiting="OBSERVATION_LIMIT", sessionId=dispatcher.execution["sessionId"], executionVersion=dispatcher.version)
    if failure_disposition is not None and dispatcher.store.read()[0].get("status") == "FAILED_NEEDS_CLEANUP":
        return failure_disposition()
    record, _ = dispatcher.current(idle=False, allow_ready=True)
    build = observed_build(dispatcher.clients["codebuild"], record)
    return dict(sessionId=record["sessionId"], executionVersion=dispatcher.version, waiting="OBSERVATION_LIMIT",
                buildId=build["id"], buildStatus=build.get("buildStatus"))


def continue_execution(dispatcher, coordinator, infra, chart, run_id, rollback_source, *, resume_approved=False,
                       observations=240, sleep=time.sleep, monotonic=time.monotonic):
    """Resume the admitted policy; a plan-resume flag is never rollback consent."""
    if type(observations) is not int or not 1 <= observations <= 240:
        raise ValueError("Continuation observation must be bounded to forty minutes")
    from automation_session import utc, validate
    from cleanup_completion import CONTEXT
    record, _ = dispatcher.store.read()
    validate(record)
    if (record.get("executionVersion") != dispatcher.version
            or any(record.get(k) != dispatcher.execution.get(k) for k in CONTEXT)
            or record.get("status") in ("OFF", "DESTROYING", "DESTROYED")
            or dispatcher.clock() >= utc(record["cleanupStartAt"])):
        raise ValueError("Continuation belongs to another or closed immutable session")
    if dispatcher.execution.get("action") != "deploy-window-rollback":
        dispatcher.current(idle=False, allow_ready=True)
        return drive(dispatcher, coordinator, resume_approved=resume_approved, observations=observations, sleep=sleep, monotonic=monotonic)
    if record["status"] == "FAILED_NEEDS_CLEANUP" and resume_approved:
        raise ValueError("A plan-resume flag cannot approve rollback of a failed execution")
    from lifecycle_orchestration import Orchestrator
    end = observation_end(monotonic())
    engine = Orchestrator(dispatcher, coordinator, {INFRA_REPO: infra}, chart, run_id=run_id, rollback_source=rollback_source)
    def rollback():
        return engine.run(observations=observations, sleep=sleep, monotonic=monotonic, observation_deadline=end)
    if record["status"] == "FAILED_NEEDS_CLEANUP": return rollback()
    dispatcher.current(idle=False, allow_ready=True)
    return drive(dispatcher, coordinator, resume_approved=resume_approved, observations=observations, sleep=sleep,
        monotonic=monotonic, failure_disposition=rollback, observation_deadline=end)


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
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler", "ssm", "codebuild", "lambda")}
    run_id = os.environ["GITHUB_RUN_ID"]
    identity = clients["sts"].get_caller_identity()
    if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
            f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-{run_id}"):
        raise ValueError("Only this protected lifecycle run's dispatcher role may continue")
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    if execution.get("sessionId") != args.session_id: raise ValueError("Immutable execution belongs to another session")
    from chart_selection import CHART_REPO
    from workflow_handoff import WorkflowAPI
    infra = WorkflowAPI(INFRA_REPO, os.environ["GITHUB_TOKEN"])
    chart = GitHub(CHART_REPO, os.environ.get("CHART_READ_TOKEN") or os.environ["GITHUB_TOKEN"])
    verified_caller(infra, run_id, execution)
    store = S3SessionStore(args.bucket)
    dispatcher = Dispatcher(clients, store, args.bucket, execution, args.execution_version)
    with tempfile.TemporaryDirectory(prefix="techx-rollback-continuation-") as directory:
        root = Path(directory) / "execution"
        def rollback_source():
            from bootstrap_publish import archive_loader
            archive_loader(clients["s3"], args.bucket, args.session_id, args.execution_version, root)
            if json.loads((root / "execution.json").read_bytes()) != execution:
                raise ValueError("Continuation archive differs from immutable execution")
            return root / "source"
        result = continue_execution(dispatcher, Coordinator(clients, store, args.bucket), infra, chart, run_id,
            rollback_source, resume_approved=args.resume_approved)
    if result.get("waiting") == "READY" and execution["action"] == "deploy-test-destroy":
        from orchestrate_lifecycle import request_cleanup
        result = dict(waiting="CLEANUP_REQUESTED", requestAcknowledged=request_cleanup(clients, store, execution, args.execution_version))
    print(json.dumps(result, sort_keys=True))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as output:
            output.write("### Lifecycle continuation\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__": main()
