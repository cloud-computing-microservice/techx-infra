"""Hosted public verification of one exact private rollout in TESTING."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from active_public_contract import active_public_context, active_result_key, read_active_public_receipt
from active_release_contract import read_rollout_receipt
from automation_session import S3SessionStore
from chart_selection import GitHub, INFRA_REPO
from edge_activation import read_activation_receipt
from leased_command import execute
from plan_review_store import read_json
from private_acceptance_contract import evidence_record
from public_acceptance import PublicAcceptance, hosted_runner


class ActivePublicAcceptance(PublicAcceptance):
    session_status = "TESTING"
    lease_operation = "acceptance"
    receipt_kind = "active-public-acceptance"

    def __init__(self, *args, release_id, **kwargs):
        if not re.fullmatch(r"[a-f0-9]{64}", release_id): raise ValueError("Exact active release identity required")
        super().__init__(*args, **kwargs)
        self.release_id = release_id

    def context(self, record):
        context = active_public_context(record)
        if context["releaseContext"]["releaseId"] != self.release_id: raise ValueError("Public child belongs to another active candidate")
        return context

    def predecessor_sha(self, context): return context["rollout"]["receiptReference"]["sha256"]
    def stage(self, record): return record.get("releaseCandidate", {}).get("publicAcceptance")
    def set_stage(self, record, stage): record["releaseCandidate"]["publicAcceptance"] = stage
    def receipt_fields(self, record): return dict(releaseId=self.release_id, rolloutReference=record["releaseCandidate"]["rollout"]["receiptReference"])
    def result_key(self, record): return active_result_key(record["sessionId"], self.release_id, self.dispatch_id)
    def read_receipt(self, record): return read_active_public_receipt(self.clients["s3"], self.bucket, record, self.version)
    def handoff_inputs(self):
        return dict(session_id=self.execution["sessionId"], execution_version=self.version, release_id=self.release_id, rollout_sha256=self.activation_sha)

    def network_target(self, record):
        read_rollout_receipt(self.clients["s3"], self.bucket, record, self.version)
        # The rollout changes the Application, not the committed edge. Read the
        # original network receipt as historical binding, never as runtime proof.
        return read_activation_receipt(self.clients["s3"], self.bucket, evidence_record(record), self.version)["resources"]["target"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--rollout-sha256", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if (os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or os.environ.get("GITHUB_REPOSITORY") != INFRA_REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", args.session_id)):
        raise ValueError("Active public checks require the protected first-attempt hosted main job")
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1, "mode": "standard"})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler")}
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    worker = ActivePublicAcceptance(clients, S3SessionStore(args.bucket), args.bucket, execution, args.execution_version,
        os.environ["GITHUB_RUN_ID"], os.environ.get("TECHX_DISPATCH_ID", ""), args.rollout_sha256, release_id=args.release_id)
    github = GitHub(INFRA_REPO, os.environ["GITHUB_TOKEN"])
    hosted_runner(github, worker.run_id, execution)
    if args.worker:
        print(json.dumps(worker.perform(github), sort_keys=True))
    else:
        worker.current(idle=True)
        worker.admit()
        command = [sys.executable, str(Path(__file__)), "--bucket", args.bucket, "--session-id", args.session_id,
            "--execution-version", args.execution_version, "--release-id", args.release_id, "--rollout-sha256", args.rollout_sha256, "--worker"]
        execute(worker.store, worker.owner, "acceptance", args.session_id, command)


if __name__ == "__main__": main()
