"""Protected GitHub-hosted public acceptance; private/VPN checks remain pending."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from automation_budget import validate_estimate
from automation_session import S3SessionStore, claim, transition, utc, validate
from chart_selection import GitHub, INFRA_REPO, read_release, read_selection
from cleanup_schedule import verify_existing
from edge_activation import read_activation_receipt
from leased_command import execute
from plan_review_store import ACCOUNT, checksum, immutable_json, read_json
from public_acceptance_contract import public_context, read_public_receipt, result_key
from public_probe import ProbeFailure, PublicProbe
from select_bootstrap_chart import verified_selector
from workflow_handoff import guard_child

WORKFLOW = ".github/workflows/staging-acceptance.yml"


def hosted_runner(github, run_id, execution):
    verified_selector(github, run_id, execution, workflow=WORKFLOW)
    response = github.get(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = response.get("jobs", [])
    if type(response.get("total_count")) is not int or response["total_count"] != len(jobs) or len(jobs) > 10:
        raise ValueError("Hosted acceptance job evidence is incomplete")
    rows = [job for job in jobs if job.get("name") == "public"]
    if len(rows) != 1: raise ValueError("Expected one actual public acceptance job")
    job = rows[0]
    if (job.get("run_id") != int(run_id) or job.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or job.get("status") != "in_progress" or job.get("runner_group_name") != "GitHub Actions"
            or job.get("labels") != ["ubuntu-24.04"] or type(job.get("runner_id")) is not int or job["runner_id"] < 1
            or type(job.get("id")) is not int or job["id"] < 1):
        raise ValueError("Public acceptance must run on the actual GitHub-hosted Ubuntu job")
    return dict(jobId=job["id"], runnerId=job["runner_id"], runnerGroup=job["runner_group_name"], labels=job["labels"])


class PublicAcceptance:
    def __init__(self, clients, store, bucket, execution, version, run_id, dispatch_id, activation_sha256, *, clock=None):
        validate(execution)
        if not re.fullmatch(r"[1-9][0-9]*", str(run_id)) or not re.fullmatch(r"[a-f0-9]{64}", dispatch_id):
            raise ValueError("Only an admitted public acceptance workflow may run")
        if not re.fullmatch(r"[a-f0-9]{64}", activation_sha256): raise ValueError("An exact activation receipt is required")
        self.clients, self.store, self.bucket = clients, store, bucket
        self.execution, self.version = deepcopy(execution), version
        self.run_id, self.dispatch_id, self.activation_sha = str(run_id), dispatch_id, activation_sha256
        self.owner = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-public-acceptance/techx-public-{self.run_id}"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.workflow_run = f"https://github.com/{INFRA_REPO}/actions/runs/{self.run_id}"

    def validate_record(self, record, *, idle=False):
        validate(record)
        if (any(record.get(key) != self.execution.get(key) for key in ("sessionId", "account", "region", "environment",
                "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"))
                or record.get("executionVersion") != self.version or record["status"] != "PROVISIONING"
                or record.get("pendingPlan") is not None or self.clock() >= utc(record["cleanupStartAt"])):
            raise ValueError("Public acceptance session is closed or differs from immutable execution")
        context = public_context(record)
        if (context["exposure"]["receiptReference"]["sha256"] != self.activation_sha
                or record.get("workflowHandoff", {}).get("id") != self.dispatch_id
                or record["workflowHandoff"].get("phase") not in ("CALLING", "OBSERVED")
                or hasattr(self, "context_hash") and checksum(context) != self.context_hash):
            raise ValueError("Public acceptance predecessor/handoff was superseded")
        if idle:
            if record.get("lease") or record.get("stages", {}).get("publicAcceptance"):
                raise ValueError("Public acceptance is already consumed or the environment is busy")
        else:
            lease = record.get("lease", {})
            if lease.get("owner") != self.owner or lease.get("operation") != "deploy" or lease.get("epoch") != self.epoch:
                raise ValueError("The public probe no longer owns its deployment lease")
            claim(record, self.owner, "deploy", now=self.clock())
            if utc(lease["expiresAt"]) <= self.clock(): raise ValueError("Public probe lease expired")
            stage = record.get("stages", {}).get("publicAcceptance")
            if stage and (stage.get("state") != "RUNNING" or stage.get("dispatchId") != self.dispatch_id
                    or stage.get("workflowRun") != self.workflow_run or stage.get("contextSha256") != self.context_hash):
                raise ValueError("Public acceptance checkpoint was replaced or completed")
        return record

    def current(self, *, idle=False):
        identity = self.clients["sts"].get_caller_identity()
        if identity.get("Account") != ACCOUNT or identity.get("Arn") != self.owner:
            raise ValueError("Only this public acceptance run's scoped role may probe")
        record, etag = self.store.read()
        self.validate_record(record, idle=idle)
        validate_estimate(record, self.execution["costEstimate"])
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        return record, etag

    def guard(self): return self.current()[0]

    def admit(self):
        guard_child(self.clients["s3"], self.bucket, dispatch_id=self.dispatch_id, repository=INFRA_REPO,
            workflow="staging-acceptance.yml", source_sha=self.execution["sourceRevisions"]["techx-infra"], run_id=self.run_id,
            inputs=dict(session_id=self.execution["sessionId"], execution_version=self.version, activation_sha256=self.activation_sha),
            observations=24, clock=self.clock)

    def perform(self, github, *, probe_factory=PublicProbe):
        current, _ = self.store.read()
        self.epoch = current.get("lease", {}).get("epoch")
        self.context_hash = checksum(public_context(current))
        record, etag = self.current()
        self.admit()
        runner = hosted_runner(github, self.run_id, self.execution)
        if record.get("stages", {}).get("publicAcceptance"):
            raise ValueError("Public probe execution was already consumed; do not repeat it")
        activation = read_activation_receipt(self.clients["s3"], self.bucket, record, self.version)
        read_release(self.clients["s3"], self.bucket, record, self.execution, self.version)
        read_selection(self.clients["s3"], self.bucket, record, self.execution, self.version)
        current, etag = self.current()
        if current.get("stages", {}).get("publicAcceptance"):
            raise ValueError("Public probe execution was already consumed")
        updated = deepcopy(current)
        updated["stages"]["publicAcceptance"] = dict(state="RUNNING", attempt=1, dispatchId=self.dispatch_id,
            workflowRun=self.workflow_run, contextSha256=self.context_hash)
        self.store.write(updated, etag)
        try:
            result = probe_factory(self.guard, record["sessionId"], self.dispatch_id).perform(activation["resources"]["target"])
            self.guard()
            if hosted_runner(github, self.run_id, self.execution) != runner:
                raise ValueError("Hosted public probe identity changed")
            receipt = dict(schemaVersion=1, kind="public-acceptance", sessionId=record["sessionId"], executionVersion=self.version,
                contextSha256=self.context_hash, dispatchId=self.dispatch_id, workflowRun=self.workflow_run, runner=runner,
                result=result, completedAt=self.clock().isoformat())
            self.guard()
            ref = immutable_json(self.clients["s3"], self.bucket, result_key(record["sessionId"], self.dispatch_id), receipt)
            ref["sha256"] = checksum(receipt)
            current, etag = self.current()
            updated = deepcopy(current)
            updated["stages"]["publicAcceptance"].update(state="PASSED", receiptReference=ref)
            read_public_receipt(self.clients["s3"], self.bucket, updated, self.version)
            self.validate_record(current)
            self.store.write(updated, etag)
            return dict(state="PUBLIC_PASSED", private="pending", vpn="pending", receiptReference=ref)
        except BaseException as error:
            # Only bounded classifications enter diagnostics, never server bodies,
            # response headers, tokens or order/customer payloads.
            try:
                current, etag = self.current()
                failure = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=self.version,
                    dispatchId=self.dispatch_id, workflowRun=self.workflow_run, contextSha256=self.context_hash,
                    classification=error.check if isinstance(error, ProbeFailure) else type(error).__name__)
                immutable_json(self.clients["s3"], self.bucket, result_key(record["sessionId"], self.dispatch_id).replace("result.json", "failure.json"), failure)
                current, etag = self.current()
                failed = transition(current, self.owner, "FAILED_NEEDS_CLEANUP", now=self.clock())
                failed["stages"]["publicAcceptance"].update(state="FAILED")
                self.store.write(failed, etag)
            except Exception:
                pass  # Cleanup takeover or unavailable evidence storage must win.
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--activation-sha256", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if (os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or os.environ.get("GITHUB_REPOSITORY") != INFRA_REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/main" or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
            or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", args.session_id)):
        raise ValueError("Public acceptance requires the protected first-attempt hosted main workflow")
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1, "mode": "standard"})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler")}
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    worker = PublicAcceptance(clients, S3SessionStore(args.bucket), args.bucket, execution, args.execution_version,
        os.environ["GITHUB_RUN_ID"], os.environ.get("TECHX_DISPATCH_ID", ""), args.activation_sha256)
    github = GitHub(INFRA_REPO, os.environ["GITHUB_TOKEN"])
    hosted_runner(github, worker.run_id, execution)
    if args.worker:
        result = worker.perform(github)
        print(json.dumps(result, sort_keys=True))
    else:
        worker.current(idle=True)
        worker.admit()
        command = [sys.executable, str(Path(__file__)), "--bucket", args.bucket, "--session-id", args.session_id,
            "--execution-version", args.execution_version, "--activation-sha256", args.activation_sha256, "--worker"]
        execute(worker.store, worker.owner, "deploy", args.session_id, command)


if __name__ == "__main__": main()
