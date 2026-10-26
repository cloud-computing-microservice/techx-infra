"""Cloud-free child callback: wake an existing orchestrator, never choose work."""
import json
import os
import re

from chart_selection import GitHub, INFRA_REPO, PLATFORM_REPO, sha
from workflow_handoff import WorkflowAPI


def wake_existing(source, destination, *, run_id, source_sha, session_id, execution_version, repository=PLATFORM_REPO):
    sha(source_sha)
    workflows = {PLATFORM_REPO: {"bootstrap-staging.yml"}, INFRA_REPO: {"staging-plan-review.yml", "staging-chart-select.yml", "staging-chart-reuse.yml", "staging-chart-promote.yml", "staging-acceptance.yml"}}
    if repository not in workflows: raise ValueError("Unsupported lifecycle callback repository")
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real lifecycle callback run is required")
    run = source.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"
            or run.get("head_sha") != source_sha or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("path") not in {path for name in workflows[repository] for path in (f".github/workflows/{name}", f".github/workflows/{name}@main")}
            or run.get("status") not in ("in_progress", "completed")
            or any(run.get(key, {}).get("full_name") != repository for key in ("repository", "head_repository"))):
        raise ValueError("Only an actual first-attempt main lifecycle child can send this wakeup")
    # The destination reads its own current session and protected handoff run.
    # No caller-provided conclusion, stage, plan or budget is forwarded.
    return destination.wake(session_id, execution_version)


def main():
    repository = os.environ.get("GITHUB_REPOSITORY")
    if (repository not in (PLATFORM_REPO, INFRA_REPO) or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise ValueError("Callback requires a trusted main lifecycle workflow")
    result = wake_existing(GitHub(repository, os.environ["GITHUB_TOKEN"]),
        WorkflowAPI(INFRA_REPO, os.environ["LIFECYCLE_APP_TOKEN"]), run_id=os.environ["GITHUB_RUN_ID"],
        source_sha=os.environ["GITHUB_SHA"], session_id=os.environ["SESSION_ID"], execution_version=os.environ["EXECUTION_VERSION"], repository=repository)
    print(json.dumps(result, sort_keys=True))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("### Existing lifecycle wakeup\n\n" + result["html_url"] + "\n")


if __name__ == "__main__": main()
