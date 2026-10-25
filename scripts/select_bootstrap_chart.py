"""Publish one protected chart-selection receipt; never deploy or merge here."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile

from automation_budget import validate_estimate
from automation_session import S3SessionStore, utc, validate
from bootstrap_publish import archive_loader
from chart_selection import CHART_REPO, INFRA_REPO, GitHub, latest_chart_ci, read_proposal, read_release, read_selection, reuse_proposal, selection
from cleanup_schedule import verify_existing
from plan_review_store import ACCOUNT, checksum, immutable_json, read_json
from workflow_handoff import guard_child

WORKFLOW = ".github/workflows/staging-chart-select.yml"


def verified_selector(github, run_id, execution, *, workflow=WORKFLOW):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real selector workflow run is required")
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("status") != "in_progress" or run.get("event") != "workflow_dispatch"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or run.get("path") not in (workflow, workflow + "@main")
            or any(run.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Selector must run once on the exact admitted infra SHA from trusted main")


class ChartSelector:
    def __init__(self, clients, store, bucket, execution, execution_version, run_id, *, clock=None):
        validate(execution)
        self.clients, self.store, self.bucket = clients, store, bucket
        self.execution, self.version, self.run_id = deepcopy(execution), execution_version, str(run_id)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def expected_identity(self):
        return f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-chart-selector/techx-chart-select-{self.run_id}"

    def guard(self):
        identity = self.clients["sts"].get_caller_identity()
        if identity.get("Account") != ACCOUNT or identity.get("Arn") != self.expected_identity():
            raise ValueError("Only the dedicated selector identity for this workflow run may publish")
        record, etag = self.store.read()
        validate(record)
        for key in ("sessionId", "account", "region", "environment", "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"):
            if record.get(key) != self.execution.get(key): raise ValueError("Selector session differs from immutable execution")
        if (record.get("executionVersion") != self.version or record["status"] != "PROVISIONING" or record.get("lease")
                or record.get("pendingPlan") is not None or self.clock() >= utc(record["cleanupStartAt"])
                or record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED" or record.get("stages", {}).get("edge")):
            raise ValueError("Selection requires the idle cold-start session before edge deployment")
        validate_estimate(record, self.execution["costEstimate"])
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        return record, etag

    def publish(self, source, chart_github, infra_github, chart_sha, pr_number):
        record, _ = self.guard()
        verified_selector(infra_github, self.run_id, self.execution)
        release = read_release(self.clients["s3"], self.bucket, record, self.execution, self.version)
        proposal = read_proposal(self.clients["s3"], self.bucket, record, self.execution, self.version)
        if reuse_proposal(proposal): raise ValueError("Zero-diff reuse requires a separate human-reviewed workflow")
        if proposal["prNumber"] != pr_number: raise ValueError("Selection PR differs from the cold proposal")
        value = selection(chart_github, Path(source), record, self.execution, self.version, release, chart_sha, pr_number)
        value["provenance"]["selectionRun"] = f"https://github.com/{INFRA_REPO}/actions/runs/{self.run_id}"
        if value["provenance"]["prHeadSha"] != proposal["headSha"] or value["treeSha"] != proposal["treeSha"]:
            raise ValueError("Merged chart differs from the immutable proposal head/tree")
        def live():
            verified_selector(infra_github, self.run_id, self.execution)
            if latest_chart_ci(chart_github, chart_sha) != value["provenance"]["ciRun"]:
                raise ValueError("Chart CI changed during selection publication")
        return self.publish_value(value, live)

    def publish_value(self, value, live):
        """Shared immutable handoff; retries preserve previously proven provenance."""
        def equivalent(previous):
            if {key: item for key, item in previous.items() if key != "provenance"} != {key: item for key, item in value.items() if key != "provenance"}:
                return False
            return (reuse_proposal(value) or all(previous.get("provenance", {}).get(name) == value["provenance"][name]
                                                  for name in ("prNumber", "prHeadSha")))
        record, _ = self.guard()
        live()
        if (record.get("bootstrapRelease") != value["releaseReference"]
                or record.get("bootstrapProposal") != value["proposalReference"]):
            raise ValueError("Cold release/proposal changed during chart verification")
        if record.get("bootstrapChart"):
            previous = read_selection(self.clients["s3"], self.bucket, record, self.execution, self.version)
            if not equivalent(previous):
                raise ValueError("A selected bootstrap chart cannot be replaced")
            return record["bootstrapChart"]
        key = f"chart-selections/staging/{record['sessionId']}/bootstrap.json"
        # A receipt may already exist after a lost session CAS. Preserve its
        # original provenance after freshly verifying the same release/PR.
        listing = self.clients["s3"].list_objects_v2(Bucket=self.bucket, Prefix=key, MaxKeys=1, ExpectedBucketOwner=ACCOUNT)
        rows = listing.get("Contents", [])
        if listing.get("IsTruncated") is not False or not isinstance(rows, list) or len(rows) > 1 or any(row.get("Key") != key for row in rows):
            raise ValueError("Ambiguous selection receipt lookup")
        if rows:
            previous, object_version = read_json(self.clients["s3"], self.bucket, key)
            if not equivalent(previous):
                raise ValueError("An existing selection receipt belongs to another release/PR")
            ref = dict(key=key, versionId=object_version, sha256=checksum(previous))
            read_selection(self.clients["s3"], self.bucket, dict(record, bootstrapChart=ref), self.execution, self.version)
        else:
            live()
            ref = immutable_json(self.clients["s3"], self.bucket, key, value)
            ref["sha256"] = checksum(value)
        record, etag = self.guard()
        live()
        if (record.get("bootstrapRelease") != value["releaseReference"] or record.get("bootstrapProposal") != value["proposalReference"]
                or record.get("bootstrapChart") not in (None, ref)):
            raise ValueError("Selection handoff lost its release/session identity")
        updated = deepcopy(record)
        updated["bootstrapChart"] = ref
        self.store.write(updated, etag)
        return ref


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--chart-sha", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    args = parser.parse_args()
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "scheduler")}
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    selector = ChartSelector(clients, S3SessionStore(args.bucket), args.bucket, execution, args.execution_version, os.environ["GITHUB_RUN_ID"])
    selector.guard()
    guard_child(clients["s3"], args.bucket, dispatch_id=os.environ.get("TECHX_DISPATCH_ID", ""),
        repository=INFRA_REPO, workflow="staging-chart-select.yml", source_sha=os.environ["GITHUB_SHA"], run_id=os.environ["GITHUB_RUN_ID"],
        inputs=dict(session_id=args.session_id, execution_version=args.execution_version, chart_sha=args.chart_sha, pr_number=str(args.pr_number)))
    token = os.environ["GITHUB_TOKEN"]
    with tempfile.TemporaryDirectory(prefix="techx-chart-select-") as directory:
        root = Path(directory) / "execution"
        archive_loader(clients["s3"], args.bucket, args.session_id, args.execution_version, root)
        if json.loads((root / "execution.json").read_bytes()) != execution:
            raise ValueError("Selection archive differs from admitted execution")
        result = selector.publish(root / "source", GitHub(CHART_REPO, token), GitHub(INFRA_REPO, token), args.chart_sha, args.pr_number)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__": main()
