"""Preview or approve an exact private plan after a real GitHub environment review.

No deployment mutation or StartBuild permission is used here. The lifecycle
coordinator must later consume the returned immutable approval reference.
"""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import re
from urllib.request import Request, urlopen

from automation_session import utc
from automation_budget import validate_estimate
from cleanup_schedule import verify_existing
from plan_approval import context_for_session, issue_approval
from plan_review_store import ACCOUNT, approval_key, immutable_json, load_review, read_json

REPO = "cloud-computing-microservice/techx-infra"
WORKFLOW = ".github/workflows/staging-plan-review.yml"
ENVIRONMENT = "staging-apply"


class GitHub:
    def __init__(self, token):
        if not token:
            raise ValueError("A short-lived read-only GitHub token is required")
        self.token = token

    def get(self, endpoint):
        # Endpoints are constructed from fixed repository and validated run IDs.
        request = Request(f"https://api.github.com/repos/{REPO}/{endpoint}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "techx-plan-approval"})
        with urlopen(request, timeout=30) as response:
            if response.headers.get("Link"):
                raise ValueError("Unexpected paginated GitHub approval data")
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("GitHub approval response exceeded limit")
        return json.loads(payload)


def verified_run(github, run_id, infra_sha, request, now):
    if not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("Invalid GitHub run ID")
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("repository", {}).get("full_name") != REPO
            or run.get("head_repository", {}).get("full_name") != REPO
            or run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main"
            or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
            or run.get("head_sha") != infra_sha or infra_sha != request["context"]["sourceRevisions"]["techx-infra"]
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("status") != "in_progress"
            or not utc(request["createdAt"]) <= utc(run["created_at"]) <= now):
        raise ValueError("Approval must run once from the exact reviewed infra commit on trusted main")
    return f"https://github.com/{REPO}/actions/runs/{run_id}"


def verified_reviewer(github, run_id, environment=ENVIRONMENT):
    history = github.get(f"actions/runs/{run_id}/approvals")
    if not isinstance(history, list):
        raise ValueError("Invalid GitHub environment review history")
    matching = [row for row in history if any(
        env.get("name") == environment and type(env.get("id")) is int and env["id"] > 0
        for env in row.get("environments", []))]
    # A new dispatch is required after rejection or ambiguous/multiple reviews.
    # Reruns are rejected above: history cannot silently approve changed inputs.
    if len(matching) != 1 or matching[0].get("state") != "approved":
        raise ValueError(f"Exactly one real {environment} environment approval is required")
    user = matching[0].get("user", {})
    if user.get("type") != "User" or type(user.get("id")) is not int or user["id"] < 1:
        raise ValueError("A human environment reviewer is required")
    return user["login"]


def pending_review(clients, bucket, ref, now):
    record, _ = read_json(clients["s3"], bucket, "automation/staging/session.json")
    if record.get("sessionId") != ref["sessionId"] or record.get("pendingPlan") != ref:
        raise ValueError("Plan was superseded or session changed")
    envelope = load_review(clients["s3"], bucket, ref, now)
    context_for_session(record, envelope["request"]["context"], now)
    validate_estimate(record, envelope["costEstimate"])
    # Read-only verification: this bridge must never create or fix schedules.
    verify_existing(clients["scheduler"], record, record.get("executionVersion"), now)
    return envelope


def process(clients, github, bucket, ref, run_id, infra_sha, *, approve, now=None):
    clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
    now = clock()
    identity = clients["sts"].get_caller_identity()
    role = "techx-automation-plan-approver" if approve else "techx-automation-plan-reader"
    if identity.get("Account") != ACCOUNT or not re.fullmatch(
            rf"arn:aws:sts::{ACCOUNT}:assumed-role/{role}/[^/]+", identity.get("Arn", "")):
        raise ValueError("Only the scoped OIDC plan review role may run this operation")
    envelope = pending_review(clients, bucket, ref, now)
    request = envelope["request"]
    review_run = verified_run(github, run_id, infra_sha, request, now)
    if not approve:
        return envelope
    reviewer = verified_reviewer(github, run_id)
    # Refuse a superseded request after the network calls, including teardown.
    now = clock()
    pending_review(clients, bucket, ref, now)
    approval = issue_approval(request, reviewer=reviewer, review_run=review_run, now=now)
    approval["reviewReference"] = ref.copy()
    def same_review(existing, proposed):
        if not isinstance(existing, dict) or set(existing) != set(proposed):
            return False
        # Retry after a lost PutObject response preserves the first audit time.
        for key in proposed:
            if key != "approvedAt" and existing[key] != proposed[key]:
                return False
        return utc(request["createdAt"]) <= utc(existing["approvedAt"]) <= now
    result = immutable_json(clients["s3"], bucket, approval_key(request, ref["sha256"]), approval, equivalent=same_review)
    return dict(result, reviewSha256=ref["sha256"], sessionId=ref["sessionId"], stage=ref["stage"], attempt=ref["attempt"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "approve"))
    args = parser.parse_args()
    if (os.environ.get("GITHUB_REPOSITORY") != REPO or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise ValueError("Dispatch a new plan review from trusted main; reruns are not approvals")
    # Keep user inputs in environment values, never interpolate them into shell.
    session, stage = os.environ["TECHX_SESSION_ID"], os.environ["TECHX_PLAN_STAGE"]
    attempt_text = os.environ["TECHX_PLAN_ATTEMPT"]
    if not re.fullmatch(r"[1-9][0-9]{0,5}", attempt_text):
        raise ValueError("Invalid plan attempt")
    ref = dict(sessionId=session, stage=stage, attempt=int(attempt_text),
        key=f"private/staging/{session}/plans/{stage}/{attempt_text}/request.json",
        versionId=os.environ["TECHX_REQUEST_VERSION"], sha256=os.environ["TECHX_REQUEST_SHA256"])
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "scheduler")}
    result = process(clients, GitHub(os.environ["GH_TOKEN"]), os.environ["TECHX_STATE_BUCKET"], ref,
                     os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_SHA"], approve=args.mode == "approve")
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    if args.mode == "preview":
        request = result["request"]
        safe = dict(sessionId=session, stage=stage, attempt=int(attempt_text),
                    reviewSha256=ref["sha256"], planSha256=request["plan"]["sha256"],
                    approvedBudgetUsd=request["context"]["approvedBudgetUsd"],
                    costEstimate=result["costEstimate"],
                    deadline=request["context"]["deadline"], expiresAt=request["expiresAt"],
                    sourceRevisions=request["context"]["sourceRevisions"], resourceChanges=result["resourceChanges"])
        with summary.open("a", encoding="utf-8") as output:
            output.write("## Saved plan awaiting review\n\n```json\n" + json.dumps(safe, indent=2) + "\n```\n")
    else:
        with summary.open("a", encoding="utf-8") as output:
            output.write("## Approval stored privately\n\n```json\n" + json.dumps(result, indent=2) + "\n```\n")
        print(json.dumps(result))


if __name__ == "__main__": main()
