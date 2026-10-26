"""Preview and privately authorize one cold promotion; never merge on a runner."""
import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import tempfile

from approve_plan import verified_reviewer
from automation_session import S3SessionStore, utc
from bootstrap_publish import archive_loader
from chart_promotion import ColdPromotionChecks
from chart_selection import CHART_REPO, INFRA_REPO, GitHub
from plan_approval import version
from plan_review_store import ACCOUNT, checksum, encoded, immutable_json, read_json
from select_bootstrap_chart import verified_selector
from workflow_handoff import guard_child, title

WORKFLOW = ".github/workflows/staging-chart-promote.yml"
ENVIRONMENT = "staging-chart-promote"


def read_promotion_approval(s3, bucket, execution, execution_version, reference, now):
    """Read immutable grant bytes, never an actor-provided approval boolean."""
    prefix = f"promotion-approvals/staging/{execution['sessionId']}/"
    if (not isinstance(reference, dict) or set(reference) != {"key", "versionId", "sha256"}
            or not isinstance(reference.get("key"), str) or not reference["key"].startswith(prefix)
            or not re.fullmatch(r"[a-f0-9]{64}\.json", reference["key"][len(prefix):])
            or not isinstance(reference.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", reference["sha256"])):
        raise ValueError("An exact session-scoped private promotion approval reference is required")
    version(reference["versionId"])
    value, _ = read_json(s3, bucket, reference["key"], reference["versionId"])
    request = value.get("request")
    if (set(value) != {"schemaVersion", "kind", "request", "requestSha256", "reviewer", "approvedAt", "expiresAt"}
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or value.get("kind") != "cold-promotion-approval" or not isinstance(request, dict)
            or checksum(value) != reference["sha256"] or checksum(request) != value.get("requestSha256")
            or reference["key"] != prefix + checksum(request) + ".json"):
        raise ValueError("Private promotion approval bytes/checksum differ from the exact reference")
    approved, expiry = utc(value["approvedAt"]), utc(value["expiresAt"])
    if (not utc(execution["createdAt"]) <= approved <= now < expiry
            or expiry > approved + timedelta(minutes=15)
            or expiry > utc(execution["cleanupStartAt"]) - timedelta(minutes=3)):
        raise ValueError("Promotion approval is expired or extends its approved cleanup window")
    expected = dict(schemaVersion=1, kind="cold-promotion", sessionId=execution["sessionId"],
        executionVersion=execution_version, sourceRevisions=execution["sourceRevisions"],
        approvedBudgetUsd=execution["approvedBudgetUsd"], deadline=execution["deadline"], cleanupStartAt=execution["cleanupStartAt"])
    if any(request.get(key) != item for key, item in expected.items()):
        raise ValueError("Promotion approval differs from the admitted execution context")
    return value


def promotion_index_key(session_id, run_id):
    if (not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session_id)
            or not re.fullmatch(r"[1-9][0-9]*", str(run_id))):
        raise ValueError("A fixed session and actual promotion review run are required")
    return f"promotion-approvals/staging/{session_id}/runs/{run_id}.json"


def authorize_promotion(checks, source, chart, infra, reference, *, owner, epoch, app_id, app_slug):
    """Revalidate an exact grant on an owned executor; this never performs merge."""
    if owner is None or epoch is None: raise ValueError("Grant consumption requires an owned private deployment lease")
    checks.guard(owner=owner, epoch=epoch)
    value = read_promotion_approval(checks.clients["s3"], checks.bucket, checks.execution, checks.version, reference, checks.clock())
    request = value["request"]
    approved, expiry = utc(value["approvedAt"]), utc(value["expiresAt"])
    run_url = request.get("workflowRun", "")
    if not isinstance(run_url, str) or not re.fullmatch(rf"https://github.com/{INFRA_REPO}/actions/runs/[1-9][0-9]*", run_url):
        raise ValueError("Promotion approval must bind an actual infra workflow run")
    run_id = run_url.rsplit("/", 1)[1]
    def approved_run():
        run = infra.get(f"actions/runs/{run_id}")
        if (run.get("id") != int(run_id) or run.get("status") != "completed" or run.get("conclusion") != "success"
                or run.get("event") != "workflow_dispatch" or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
                or run.get("head_branch") != "main" or run.get("head_sha") != checks.execution["sourceRevisions"]["techx-infra"]
                or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
                or any(run.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))
                or not utc(checks.execution["createdAt"]) <= utc(run["created_at"]) <= approved
                # Actions run timestamps have second precision; the private
                # grant timestamp retains microseconds within that last second.
                or not approved.replace(microsecond=0) <= utc(run["updated_at"]) <= checks.clock()
                or verified_reviewer(infra, run_id, environment=ENVIRONMENT) != value["reviewer"]):
            raise ValueError("Private promotion grant lacks its successful trusted run and real human approval")
    approved_run()
    fresh = checks.evaluate(source, chart, app_id=app_id, app_slug=app_slug, owner=owner, epoch=epoch)
    fresh["workflowRun"] = run_url
    if fresh != request: raise ValueError("Current promotion candidate differs from its reviewed private grant")
    approved_run()
    checks.guard(owner=owner, epoch=epoch)
    if checks.clock() >= expiry: raise ValueError("Promotion grant expired during verification")
    return dict(reference=reference, approval=value)


class PromotionReview(ColdPromotionChecks):
    def __init__(self, *args, run_id, read_only=True, dispatch_id="", **kwargs):
        super().__init__(*args, **kwargs)
        if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real promotion review workflow is required")
        self.run_id, self.read_only = str(run_id), read_only
        if dispatch_id: title(dispatch_id)
        self.dispatch_id = dispatch_id

    def guard(self, *, owner=None, epoch=None):
        role, name = ("chart-reader", "chart-read") if self.read_only else ("promotion-approver", "promotion-approve")
        identity = self.clients["sts"].get_caller_identity()
        if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
                f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-{role}/techx-{name}-{self.run_id}"):
            raise ValueError("Promotion review requires its exact read-only or approval-only workflow role")
        if owner is not None or epoch is not None: raise ValueError("The review workflow must not hold a deployment lease")
        record = super().guard()
        guard_child(self.clients["s3"], self.bucket, dispatch_id=self.dispatch_id, repository=INFRA_REPO,
            workflow=WORKFLOW.rsplit("/", 1)[1], source_sha=self.execution["sourceRevisions"]["techx-infra"],
            run_id=self.run_id, inputs=dict(session_id=self.execution["sessionId"], execution_version=self.version), clock=self.clock)
        return record

    def preview(self, source, chart, infra, *, app_id, app_slug):
        verified_selector(infra, self.run_id, self.execution, workflow=WORKFLOW)
        request = self.evaluate(source, chart, app_id=app_id, app_slug=app_slug)
        request["workflowRun"] = f"https://github.com/{INFRA_REPO}/actions/runs/{self.run_id}"
        return request

    def approve(self, source, chart, infra, request, request_sha256, *, app_id, app_slug):
        if self.read_only: raise ValueError("Preview identity cannot issue promotion approval")
        if (not isinstance(request, dict) or not isinstance(request_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", request_sha256) or checksum(request) != request_sha256):
            raise ValueError("An exact promotion preview/checksum is required")
        if self.preview(source, chart, infra, app_id=app_id, app_slug=app_slug) != request:
            raise ValueError("Promotion candidate changed after preview; a new review is required")
        reviewer = verified_reviewer(infra, self.run_id, environment=ENVIRONMENT)
        if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", reviewer):
            raise ValueError("Human promotion reviewer is incomplete")
        now = self.clock()
        expiry = min(now + timedelta(minutes=15), utc(request["cleanupStartAt"]) - timedelta(minutes=3))
        if expiry <= now: raise ValueError("Promotion review has insufficient time before the cleanup margin")
        value = dict(schemaVersion=1, kind="cold-promotion-approval", request=request, requestSha256=request_sha256,
                     reviewer=reviewer, approvedAt=now.isoformat(), expiresAt=expiry.isoformat())
        # Review can be withdrawn, source/main can move and cleanup can start
        # while API reads are in flight. None of those changes may issue a grant.
        if (self.preview(source, chart, infra, app_id=app_id, app_slug=app_slug) != request
                or verified_reviewer(infra, self.run_id, environment=ENVIRONMENT) != reviewer):
            raise ValueError("Promotion scope or human approval changed before publication")
        self.guard()
        if self.clock() >= expiry: raise ValueError("Promotion approval expired before publication")
        key = f"promotion-approvals/staging/{self.execution['sessionId']}/{request_sha256}.json"
        ref = immutable_json(self.clients["s3"], self.bucket, key, value)
        ref["sha256"] = checksum(value)
        if self.dispatch_id:
            self.guard()
            index = dict(schemaVersion=1, kind="cold-promotion-index", sessionId=self.execution["sessionId"],
                executionVersion=self.version, sourceRevisions=self.execution["sourceRevisions"],
                dispatchId=self.dispatch_id, workflowRun=request["workflowRun"], approvalReference=ref)
            immutable_json(self.clients["s3"], self.bucket, promotion_index_key(self.execution["sessionId"], self.run_id), index)
        return ref


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "approve"))
    args = parser.parse_args()
    if (os.environ.get("GITHUB_REPOSITORY") != INFRA_REPO or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise ValueError("Promotion review must run once from trusted infra main")
    session, object_version = os.environ["SESSION_ID"], os.environ["EXECUTION_VERSION"]
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session): raise ValueError("Invalid promotion session")
    app_text, app_slug = os.environ["PROMOTION_APP_ID"], os.environ["PROMOTION_APP_SLUG"]
    if not re.fullmatch(r"[1-9][0-9]*", app_text): raise ValueError("Actual promotion App metadata must be configured")
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler")}
    bucket = os.environ["CONTROL_BUCKET"]
    execution, _ = read_json(clients["s3"], bucket, f"executions/{session}.json", object_version)
    if execution.get("sessionId") != session: raise ValueError("Promotion execution belongs to another session")
    review = PromotionReview(clients, S3SessionStore(bucket), bucket, execution, object_version,
                             run_id=os.environ["GITHUB_RUN_ID"], read_only=args.mode == "preview",
                             dispatch_id=os.environ.get("TECHX_DISPATCH_ID", ""))
    review.guard()
    chart, infra = GitHub(CHART_REPO, os.environ["CHART_READ_TOKEN"]), GitHub(INFRA_REPO, os.environ["GITHUB_TOKEN"])
    with tempfile.TemporaryDirectory(prefix="techx-promotion-review-") as directory:
        root = Path(directory) / "execution"
        archive_loader(clients["s3"], bucket, session, object_version, root)
        if json.loads((root / "execution.json").read_bytes()) != execution:
            raise ValueError("Promotion archive differs from immutable execution")
        if args.mode == "preview":
            result = review.preview(root / "source", chart, infra, app_id=int(app_text), app_slug=app_slug)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write("request=" + encoded(result).decode() + "\nsha256=" + checksum(result) + "\n")
        else:
            request = os.environ["PROMOTION_REQUEST_JSON"]
            if len(request.encode()) > 64 * 1024: raise ValueError("Promotion preview exceeds policy")
            result = review.approve(root / "source", chart, infra, json.loads(request), os.environ["PROMOTION_REQUEST_SHA256"],
                                    app_id=int(app_text), app_slug=app_slug)
        print(json.dumps(result, sort_keys=True))
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
                output.write("### Cold chart promotion " + args.mode + "\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__": main()
