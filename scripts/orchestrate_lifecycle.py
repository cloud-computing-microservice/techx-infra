"""Protected wakeup/observation of one already admitted staging execution."""
import json
import os
from pathlib import Path
import re

from automation_session import S3SessionStore, utc, validate
from chart_callback import bound_chart_run
from chart_selection import CHART_REPO, INFRA_REPO, PLATFORM_REPO, GitHub
from lifecycle_coordinator import Coordinator
from lifecycle_dispatch import CONTEXT, Dispatcher
from lifecycle_orchestration import Orchestrator
from plan_approval import version
from plan_review_store import ACCOUNT, encoded, read_json
from workflow_handoff import WorkflowAPI, listed_json, load_intent, response_identity

WORKFLOW = ".github/workflows/staging-orchestrate.yml"
OPEN_WORKFLOW = ".github/workflows/staging-lifecycle.yml"


def resolve(clients, store, bucket, github, *, run_id, source_sha, event_name, event, chart=None):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real orchestrator run is required")
    own = github.get(f"actions/runs/{run_id}")
    if (own.get("id") != int(run_id) or own.get("status") != "in_progress"
            or own.get("event") != event_name or event_name not in ("workflow_dispatch", "workflow_run")
            or type(own.get("run_attempt")) is not int or own["run_attempt"] != 1
            or own.get("head_branch") != "main" or own.get("head_sha") != source_sha
            or own.get("path") not in (WORKFLOW, WORKFLOW + "@main")
            or any(own.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Orchestration must run once from trusted infra main")
    chart_run_id = None
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs", {})
        if not isinstance(inputs, dict) or set(inputs) - {"session_id", "execution_version", "chart_run_id"}:
            raise ValueError("Only an existing immutable session or an actual chart run may be woken")
        inputs = {key: value for key, value in inputs.items() if value != ""}
        if set(inputs) == {"chart_run_id"}:
            number = inputs["chart_run_id"]
            if not isinstance(number, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", number):
                raise ValueError("Chart callback requires one actual numeric run ID")
            chart_run_id = int(number)
        elif set(inputs) == {"session_id", "execution_version"}:
            session, object_version = inputs["session_id"], inputs["execution_version"]
        else: raise ValueError("Session references and chart run hints cannot be mixed or incomplete")
    if event_name == "workflow_run" or chart_run_id is not None:
        pointer, _ = read_json(clients["s3"], bucket, "control/staging/current-execution.json")
        if pointer.get("status") == "OFF": return dict(waiting="NO_ACTIVE_SESSION")
        session, object_version = pointer["sessionId"], pointer["executionVersion"]
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session): raise ValueError("Invalid existing session")
    version(object_version)
    current, _ = store.read()
    validate(current)
    if current["sessionId"] != session or current.get("executionVersion") != object_version:
        raise ValueError("Wakeup targets a different or stale staging execution")
    execution, _ = read_json(clients["s3"], bucket, f"executions/{session}.json", object_version)
    validate(execution)
    if (execution["sessionId"] != session or current.get("executionVersion") != object_version
            or any(current.get(key) != execution.get(key) for key in CONTEXT)):
        raise ValueError("Wakeup targets a different or stale staging execution")
    if current["status"] in ("OFF", "DESTROYED", "DESTROYING"):
        return dict(waiting="NO_ACTIVE_SESSION")
    if not re.fullmatch(rf"https://github.com/{INFRA_REPO}/actions/runs/[1-9][0-9]*", execution.get("sourceAdmission", {}).get("workflowRun", "")):
        raise ValueError("Orchestration requires an execution admitted by the protected opening workflow")
    if source_sha != execution["sourceRevisions"]["techx-infra"]:
        raise ValueError("Orchestrator source differs from the immutable admitted infra revision")
    root_id = int(execution["sourceAdmission"]["workflowRun"].rsplit("/", 1)[1])
    root = github.get(f"actions/runs/{root_id}")
    if (root.get("id") != root_id or root.get("event") != "workflow_dispatch" or root.get("head_sha") != source_sha
            or root.get("head_branch") != "main" or type(root.get("run_attempt")) is not int or root["run_attempt"] != 1
            or root.get("path") not in (OPEN_WORKFLOW, OPEN_WORKFLOW + "@main")
            or any(root.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Opening workflow identity differs from the protected admission")
    if root.get("status") != "completed": return dict(waiting="OPENING_WORKFLOW_RUNNING")
    bound_failure = root.get("conclusion") != "success" or current["status"] == "FAILED_NEEDS_CLEANUP"
    if chart_run_id is not None:
        if chart is None: raise ValueError("Chart callback requires independent read-only GitHub evidence")
        if not bound_chart_run(clients["s3"], bucket, current, execution, object_version, chart, chart_run_id):
            return dict(waiting="UNRELATED_CALLBACK")
        # A stale failed run must not kill a newer accepted release. The engine
        # observes the latest actual CI/review; the sender supplies no verdict.
    if event_name == "workflow_run":
        number = event.get("workflow_run", {}).get("id")
        if type(number) is not int or number < 1: raise ValueError("A real completed workflow callback is required")
        source = github.get(f"actions/runs/{number}")
        if (source.get("id") != number or source.get("status") != "completed"
                or source.get("event") != "workflow_dispatch" or type(source.get("run_attempt")) is not int or source["run_attempt"] != 1
                or source.get("head_sha") != source_sha or source.get("head_branch") != "main"
                or any(source.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
            return dict(waiting="UNRELATED_CALLBACK")
        source_url = f"https://github.com/{INFRA_REPO}/actions/runs/{number}"
        bound = (source.get("path") in (OPEN_WORKFLOW, OPEN_WORKFLOW + "@main")
                 and execution.get("sourceAdmission", {}).get("workflowRun") == source_url)
        state = current.get("workflowHandoff")
        if not bound and state:
            intent = load_intent(clients["s3"], bucket, current, state.get("intentReference"))
            target = intent["target"]
            key = state["intentReference"]["key"].replace("/intent.json", "/receipt.json")
            receipt = listed_json(clients["s3"], bucket, key)
            if receipt:
                value, _ = receipt
                if (value.get("dispatchId") != state["id"] or value.get("intentReference") != state["intentReference"]):
                    raise ValueError("Callback handoff receipt differs from its protected intent")
                response = response_identity(value["response"], target["repository"])
                bound = (target["repository"] == INFRA_REPO and response["workflow_run_id"] == number
                         and source.get("path") in (f".github/workflows/{target['workflow']}", f".github/workflows/{target['workflow']}@main"))
        if not bound: return dict(waiting="UNRELATED_CALLBACK")
        bound_failure = bound_failure or source.get("conclusion") != "success"
    return dict(execution=execution, executionVersion=object_version, boundFailure=bound_failure)


def request_cleanup(clients, store, execution, object_version):
    current, _ = store.read()
    validate(current)
    if (current.get("executionVersion") != object_version
            or any(current.get(key) != execution.get(key) for key in CONTEXT)
            or current["status"] in ("OFF", "DESTROYED", "DESTROYING")):
        return False
    response = clients["lambda"].invoke(FunctionName=f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
        InvocationType="Event", Payload=encoded(dict(sessionId=execution["sessionId"], executionVersion=object_version, requestedEarly=True)))
    if response.get("StatusCode") != 202: raise ValueError("Independent cleanup request was not acknowledged")
    return True


def main():
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler", "ssm", "codebuild", "lambda")}
    run_id, source_sha = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_SHA"]
    identity = clients["sts"].get_caller_identity()
    if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
            f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-{run_id}"):
        raise ValueError("Only this protected orchestrator's exact dispatcher identity is accepted")
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    if event_path.stat().st_size > 1024 * 1024: raise ValueError("Callback event exceeds policy")
    bucket, token = os.environ["CONTROL_BUCKET"], os.environ["GITHUB_TOKEN"]
    store = S3SessionStore(bucket)
    infra = WorkflowAPI(INFRA_REPO, token)
    chart = GitHub(CHART_REPO, token)
    context = resolve(clients, store, bucket, infra, run_id=run_id, source_sha=source_sha,
                      event_name=os.environ["GITHUB_EVENT_NAME"], event=json.loads(event_path.read_bytes()), chart=chart)
    if context.get("waiting"):
        summary(context)
        return
    execution, object_version = context["execution"], context["executionVersion"]
    dispatcher = Dispatcher(clients, store, bucket, execution, object_version)
    try:
        if context["boundFailure"] or dispatcher.clock() >= utc(execution["cleanupStartAt"]):
            requested = request_cleanup(clients, store, execution, object_version)
            summary(dict(waiting="CLEANUP_REQUESTED", requestAcknowledged=requested))
            return
        apis = {INFRA_REPO: infra}
        if os.environ.get("LIFECYCLE_APP_TOKEN"):
            apis[PLATFORM_REPO] = WorkflowAPI(PLATFORM_REPO, os.environ["LIFECYCLE_APP_TOKEN"])
        result = Orchestrator(dispatcher, Coordinator(clients, store, bucket), apis, chart).run()
        summary(result)
    except BaseException:
        request_cleanup(clients, store, execution, object_version)
        raise


def summary(result):
    print(json.dumps(result, sort_keys=True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("### Admitted lifecycle orchestration\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__": main()
