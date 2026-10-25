"""Human-reviewed, zero-diff reuse of the exact admitted cold-start chart baseline."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile

from approve_plan import verified_reviewer
from automation_session import S3SessionStore
from bootstrap_publish import archive_loader
from chart_selection import (CHART_REPO, INFRA_REPO, GitHub, REUSE_ENVIRONMENT, REUSE_WORKFLOW,
                             latest_chart_ci, read_proposal, read_release, release_tree, reuse_proposal)
from plan_review_store import ACCOUNT, checksum, encoded, read_json
from select_bootstrap_chart import ChartSelector, verified_selector
from workflow_handoff import guard_child


class ChartReuse(ChartSelector):
    def __init__(self, *args, read_only=False, **kwargs):
        self.read_only = read_only
        super().__init__(*args, **kwargs)

    def expected_identity(self):
        role = "chart-reader" if self.read_only else "chart-selector"
        name = "chart-read" if self.read_only else "chart-reuse"
        return f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-{role}/techx-{name}-{self.run_id}"

    def preview(self, source, chart_github, infra_github):
        record, _ = self.guard()
        verified_selector(infra_github, self.run_id, self.execution, workflow=REUSE_WORKFLOW)
        release = read_release(self.clients["s3"], self.bucket, record, self.execution, self.version)
        proposal = read_proposal(self.clients["s3"], self.bucket, record, self.execution, self.version)
        if not reuse_proposal(proposal): raise ValueError("A changed release must use its reviewed PR, not baseline reuse")
        baseline = self.execution["sourceRevisions"]["techx-chart"]
        ci = latest_chart_ci(chart_github, baseline)
        tree = release_tree(chart_github, Path(source), self.execution, release, baseline)
        if tree["changedFiles"] or tree["treeSha"] != proposal["treeSha"]:
            raise ValueError("Reuse requires zero tree changes at the exact proposal baseline")
        current, _ = self.guard()
        if (current.get("bootstrapRelease") != record["bootstrapRelease"]
                or current.get("bootstrapProposal") != record["bootstrapProposal"]
                or latest_chart_ci(chart_github, baseline) != ci):
            raise ValueError("Reuse release, proposal or CI changed during preview")
        return dict(schemaVersion=1, kind="reuse", sessionId=record["sessionId"], executionVersion=self.version,
            sourceRevisions=self.execution["sourceRevisions"], baselineSha=baseline, treeSha=tree["treeSha"],
            releaseReference=record["bootstrapRelease"], proposalReference=record["bootstrapProposal"],
            manifest=deepcopy(release["manifest"]), ciRun=ci, approvedBudgetUsd=record["approvedBudgetUsd"],
            deadline=record["deadline"], cleanupStartAt=record["cleanupStartAt"],
            workflowRun=f"https://github.com/{INFRA_REPO}/actions/runs/{self.run_id}")

    def approve(self, source, chart_github, infra_github, request, expected_sha256):
        if self.read_only: raise ValueError("The preview identity cannot publish a chart selection")
        if (not isinstance(request, dict) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
                or checksum(request) != expected_sha256):
            raise ValueError("An exact reviewed reuse preview is required")
        fresh = self.preview(source, chart_github, infra_github)
        if fresh != request: raise ValueError("Reuse preview changed after human review; start a new review")
        reviewer = verified_reviewer(infra_github, self.run_id, environment=REUSE_ENVIRONMENT)
        if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", reviewer):
            raise ValueError("Human reuse reviewer is incomplete")
        value = dict(schemaVersion=2, kind="reuse", sessionId=request["sessionId"], executionVersion=self.version,
            sourceRevisions=request["sourceRevisions"], releaseReference=request["releaseReference"],
            proposalReference=request["proposalReference"], baselineSha=request["baselineSha"],
            chartSha=request["baselineSha"], treeSha=request["treeSha"],
            provenance=dict(reviewRun=request["workflowRun"], reviewer=reviewer,
                            previewSha256=expected_sha256, ciRun=request["ciRun"]))
        def live():
            verified_selector(infra_github, self.run_id, self.execution, workflow=REUSE_WORKFLOW)
            if verified_reviewer(infra_github, self.run_id, environment=REUSE_ENVIRONMENT) != reviewer:
                raise ValueError("Human reuse review changed before publication")
            if latest_chart_ci(chart_github, request["baselineSha"]) != request["ciRun"]:
                raise ValueError("Reuse chart main or CI changed before publication")
        return self.publish_value(value, live)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "approve"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", args.session_id): raise ValueError("Invalid reuse session")
    import boto3
    from botocore.config import Config
    config = Config(retries={"total_max_attempts": 1})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler")}
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    if execution.get("sessionId") != args.session_id: raise ValueError("Reuse execution belongs to another session")
    selector = ChartReuse(clients, S3SessionStore(args.bucket), args.bucket, execution, args.execution_version,
                          os.environ["GITHUB_RUN_ID"], read_only=args.mode == "preview")
    selector.guard()
    guard_child(clients["s3"], args.bucket, dispatch_id=os.environ.get("TECHX_DISPATCH_ID", ""),
        repository=INFRA_REPO, workflow="staging-chart-reuse.yml", source_sha=os.environ["GITHUB_SHA"], run_id=os.environ["GITHUB_RUN_ID"],
        inputs=dict(session_id=args.session_id, execution_version=args.execution_version))
    token = os.environ["GITHUB_TOKEN"]
    chart, infra = GitHub(CHART_REPO, token), GitHub(INFRA_REPO, token)
    with tempfile.TemporaryDirectory(prefix="techx-chart-reuse-") as directory:
        root = Path(directory) / "execution"
        archive_loader(clients["s3"], args.bucket, args.session_id, args.execution_version, root)
        if json.loads((root / "execution.json").read_bytes()) != execution:
            raise ValueError("Reuse archive differs from immutable execution")
        if args.mode == "preview":
            result = selector.preview(root / "source", chart, infra)
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write("request=" + encoded(result).decode() + "\nsha256=" + checksum(result) + "\n")
        else:
            request = os.environ["REUSE_REQUEST_JSON"]
            if len(request.encode()) > 256 * 1024: raise ValueError("Reuse request exceeds size policy")
            result = selector.approve(root / "source", chart, infra, json.loads(request), os.environ["REUSE_REQUEST_SHA256"])
        print(json.dumps(result, sort_keys=True))
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
                output.write("### Cold chart reuse " + args.mode + "\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")


if __name__ == "__main__": main()
