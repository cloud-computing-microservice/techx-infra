"""Protected hosted public probes for the exact restored rollback revision."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from automation_session import S3SessionStore, utc
from chart_selection import GitHub, INFRA_REPO
from cleanup_completion import CONTEXT
from edge_activation import read_activation_receipt
from leased_command import execute
from plan_review_store import read_json
from public_acceptance import PublicAcceptance, hosted_runner
from rollback_public_contract import network_record, public_context, read_public_receipt, result_key
from rollback_rollout_contract import read_rollback_rollout
from rollback_task_contract import slot


class RollbackPublicAcceptance(PublicAcceptance):
    session_status = "FAILED_NEEDS_CLEANUP"
    lease_operation = "rollback"
    receipt_kind = "rollback-public-acceptance"

    def __init__(self, *args, rollback_id, **kwargs):
        if not re.fullmatch(r"[a-f0-9]{64}", rollback_id): raise ValueError("Exact restored rollback identity required")
        super().__init__(*args, **kwargs)
        self.rollback_id = rollback_id

    def context(self, record):
        context = public_context(record)
        if context["rollbackContext"]["rollbackId"] != self.rollback_id: raise ValueError("Public child belongs to another rollback")
        return context

    def predecessor_sha(self, context): return context["rollout"]["receiptReference"]["sha256"]
    def stage(self, record): return slot(record).get("publicAcceptance")
    def set_stage(self, record, stage): record["rollbackAttempt"]["publicAcceptance"] = stage
    def receipt_fields(self, record): return dict(rollbackId=self.rollback_id, rolloutReference=slot(record)["rolloutReference"])
    def result_key(self, record): return result_key(record, self.dispatch_id)
    def read_receipt(self, record): return read_public_receipt(self.clients["s3"], self.bucket, record, self.version)
    def failed_record(self, record): return dict(record)
    def handoff_inputs(self):
        return dict(session_id=self.execution["sessionId"], execution_version=self.version, rollback_id=self.rollback_id, rollout_sha256=self.activation_sha)

    def validate_record(self, record, *, idle=False):
        super().validate_record(record, idle=idle)
        if (self.execution.get("action") != "deploy-window-rollback" or any(record.get(name) != self.execution.get(name) for name in CONTEXT)
                or self.clock() >= utc(slot(record)["intent"]["expiresAt"])):
            raise ValueError("Rollback public checks changed their immutable execution or expired")
        return record

    def network_target(self, record):
        read_rollback_rollout(self.clients["s3"], self.bucket, record, self.version)
        return read_activation_receipt(self.clients["s3"], self.bucket, network_record(record), self.version)["resources"]["target"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--rollback-id", required=True)
    parser.add_argument("--rollout-sha256", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if (os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or os.environ.get("GITHUB_REPOSITORY") != INFRA_REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", args.session_id)):
        raise ValueError("Rollback public checks require the protected first-attempt hosted main job")
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1, "mode": "standard"})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler")}
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    worker = RollbackPublicAcceptance(clients, S3SessionStore(args.bucket), args.bucket, execution, args.execution_version,
        os.environ["GITHUB_RUN_ID"], os.environ.get("TECHX_DISPATCH_ID", ""), args.rollout_sha256, rollback_id=args.rollback_id)
    github = GitHub(INFRA_REPO, os.environ["GITHUB_TOKEN"])
    hosted_runner(github, worker.run_id, execution)
    if args.worker: print(json.dumps(worker.perform(github), sort_keys=True))
    else:
        worker.current(idle=True); worker.admit()
        command = [sys.executable, str(Path(__file__)), "--bucket", args.bucket, "--session-id", args.session_id,
            "--execution-version", args.execution_version, "--rollback-id", args.rollback_id, "--rollout-sha256", args.rollout_sha256, "--worker"]
        execute(worker.store, worker.owner, "rollback", args.session_id, command)


if __name__ == "__main__": main()
