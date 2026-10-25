"""Trusted cold-publisher run and latest complete platform CI provenance."""
import re

PLATFORM_REPO = "cloud-computing-microservice/techx-platform"
WORKFLOW = ".github/workflows/bootstrap-staging.yml"
VERIFY = ".github/workflows/verify.yml"
PROOF_FIELDS = {"repository", "platformSha", "infraSha", "workflowRunId", "ciRunId"}


def validate_proof(proof, execution):
    if (not isinstance(proof, dict) or set(proof) != PROOF_FIELDS or proof.get("repository") != PLATFORM_REPO
            or proof.get("platformSha") != execution["sourceRevisions"]["techx-platform"]
            or proof.get("infraSha") != execution["sourceRevisions"]["techx-infra"]
            or any(type(proof.get(key)) is not int or proof[key] < 1 for key in ("workflowRunId", "ciRunId"))):
        raise ValueError("Cold release requires exact trusted source/CI provenance")
    return proof


def publisher_proof(github, execution, owner):
    if not re.fullmatch(re.escape(PLATFORM_REPO) + r":[1-9][0-9]*:1", owner):
        raise ValueError("Cold publish must be the first attempt of a real platform workflow")
    run_id = int(owner.split(":")[1])
    run = github.get(f"actions/runs/{run_id}")
    sha = execution["sourceRevisions"]["techx-platform"]
    if (run.get("id") != run_id or run.get("status") != "in_progress" or run.get("event") != "workflow_dispatch"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != sha or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
            or any(run.get(key, {}).get("full_name") != PLATFORM_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Cold publisher must run trusted main at the admitted platform SHA")
    response = github.get(f"actions/workflows/verify.yml/runs?head_sha={sha}&per_page=100")
    runs = response.get("workflow_runs", [])
    if type(response.get("total_count")) is not int or response["total_count"] != len(runs) or not runs:
        raise ValueError("Complete platform CI history for the exact source is required")
    valid = [row for row in runs if row.get("head_sha") == sha and row.get("head_branch") == "main"
        and row.get("event") in ("push", "workflow_dispatch") and row.get("path") in (VERIFY, VERIFY + "@main")
        and all(row.get(key, {}).get("full_name") == PLATFORM_REPO for key in ("repository", "head_repository"))]
    if not valid or any(type(row.get("id")) is not int or row["id"] < 1 for row in valid):
        raise ValueError("Platform CI must be the trusted main workflow on the admitted source")
    ci = max(valid, key=lambda row: row["id"])
    if ci.get("status") != "completed" or ci.get("conclusion") != "success":
        raise ValueError("Latest platform main CI must pass; older green runs cannot authorize publishing")
    return validate_proof(dict(repository=PLATFORM_REPO, platformSha=sha,
        infraSha=execution["sourceRevisions"]["techx-infra"], workflowRunId=run_id, ciRunId=ci["id"]), execution)
