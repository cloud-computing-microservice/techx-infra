"""One bounded foundation/edge task on the persistent deployment EC2 executor.

The trusted immutable-bundle loader invokes this entry point. Planning exits
after publishing the review; a separate approved invocation resumes apply. It
does not wait for a reviewer, bootstrap GitOps, or publish READY by itself.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from automation_budget import validate_estimate
from automation_session import S3SessionStore, utc
from cleanup_schedule import verify_existing
from deployment_runtime import ACCOUNT, TerraformRuntime
from deployment_tasks import read_task
from leased_command import execute
from lifecycle_stage import StageController, validate_planning
from plan_approval import MAX_PLAN_BYTES, digest, version
from plan_review_store import approval_key, checksum, load_review, read_json


class LifecycleStageWorker:
    def __init__(self, runtime, store, execution_version, *, clock=None, task_id=None):
        version(execution_version)
        self.runtime, self.store, self.execution_version = runtime, store, execution_version
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.task_id = task_id
        self.controller = StageController(runtime.clients["s3"], store, runtime.bucket, self.clock)

    def guard(self, owner, epoch):
        execution = self.runtime.execution
        record, _ = self.controller.owned(execution["sessionId"], owner, epoch)
        for key in ("account", "region", "environment", "sourceRevisions", "approvedBudgetUsd", "deadline", "cleanupStartAt", "maximumHours", "action"):
            if record.get(key) != execution.get(key):
                raise ValueError("Current session differs from immutable execution")
        if record.get("executionVersion") != self.execution_version:
            raise ValueError("Current execution version changed")
        if self.task_id is not None:
            task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
            if (record["deploymentTask"].get("state") != "RUNNING"
                    or (task["mode"], task["stage"], task["attempt"]) != self.action
                    or task["instanceId"] != self.runtime.private["deploymentExecutor"]["instanceId"]):
                raise ValueError("Stage worker is not the current claimed remote task")
        elif record.get("deploymentTask"):
            raise ValueError("A managed remote task cannot bypass its task identity")
        verify_existing(self.runtime.clients["scheduler"], record, self.execution_version, self.clock())
        return record

    def estimate(self):
        execution = self.runtime.execution
        result = self.runtime.run(["pwsh", "-NoProfile", "-File", str(self.runtime.infra / "scripts/cost-estimate.ps1"),
            "-Hours", str(execution["maximumHours"]), "-ApprovedBudgetUsd", str(execution["approvedBudgetUsd"])],
            env=self.runtime.env, check=True, capture_output=True)
        estimate = json.loads(result.stdout)
        validate_estimate(execution, estimate)
        return estimate

    def existing_review(self, stage, attempt):
        session_id = self.runtime.execution["sessionId"]
        key = f"private/staging/{session_id}/plans/{stage}/{attempt}/request.json"
        try:
            envelope, object_version = read_json(self.runtime.clients["s3"], self.runtime.bucket, key)
        except self.runtime.clients["s3"].exceptions.NoSuchKey:
            return None, None
        ref = dict(key=key, versionId=object_version, sha256=checksum(envelope), sessionId=session_id, stage=stage, attempt=attempt)
        return ref, load_review(self.runtime.clients["s3"], self.runtime.bucket, ref, self.clock())

    def perform(self, mode, stage, attempt, owner, epoch, *, approval_minutes=45):
        if mode not in ("plan", "apply") or stage not in ("foundation", "edge") or type(attempt) is not int or attempt < 1:
            raise ValueError("A supported mode/stage and positive attempt are required")
        if type(approval_minutes) is not int or not 5 <= approval_minutes <= 60:
            raise ValueError("Approval expiry must be between 5 and 60 minutes")
        self.action = mode, stage, attempt
        record = self.guard(owner, epoch)
        checkpoint = record.get("stages", {}).get(stage, {})
        retry_published = mode == "plan" and checkpoint.get("state") == "AWAITING_APPROVAL" and checkpoint.get("attempt") == attempt
        if mode == "plan" and not retry_published:
            validate_planning(record, stage, attempt)
        if mode == "apply" and (checkpoint.get("state") != "AWAITING_APPROVAL" or checkpoint.get("attempt") != attempt):
            raise ValueError("Apply requires the current unconsumed stage attempt")
        if stage == "edge":
            from deployment_tasks import validate_gitops_checkpoint
            gitops = validate_gitops_checkpoint(record)
            if self.runtime.private["terraformVars"].get("internal_alb_arn") != gitops["internalAlbArn"]:
                raise ValueError("Edge private inputs differ from the observed GitOps ALB")
        self.runtime.initialize()
        self.guard(owner, epoch)
        if mode == "plan" and stage == "foundation":
            self.runtime.prepare_bootstrap(lambda: self.guard(owner, epoch))
            self.guard(owner, epoch)
        self.runtime.configure(stage, planning=mode == "plan")
        if mode == "plan":
            self.guard(owner, epoch)
            self.runtime.persist_variables()
            estimate = self.estimate()
            ref, envelope = self.existing_review(stage, attempt)
            if envelope is not None:
                # Resume a lost publish/CAS response using the original binary,
                # creation time and expiry. Never generate different plan bytes
                # at an already occupied immutable attempt key.
                request = envelope["request"]
                if request["context"] != self.runtime.observe():
                    raise ValueError("Stored plan context changed; a new reviewed attempt is required")
                if envelope["costEstimate"] != {key: estimate[key] for key in envelope["costEstimate"]}:
                    raise ValueError("Cost estimate changed since the original review")
                if retry_published:
                    current = self.guard(owner, epoch)
                    if current.get("pendingPlan") != ref or checkpoint.get("reviewReference") != ref:
                        raise ValueError("Published attempt no longer matches the pending review")
                    return ref
                artifact = request["plan"]
                response = self.runtime.clients["s3"].get_object(Bucket=self.runtime.bucket, Key=artifact["key"],
                    VersionId=artifact["versionId"], ExpectedBucketOwner=ACCOUNT)
                payload = response["Body"].read(MAX_PLAN_BYTES + 1)
                if response.get("VersionId") != artifact["versionId"] or digest(payload) != artifact["sha256"]:
                    raise ValueError("Stored binary differs from the review being recovered")
                context, changes, expires = request["context"], envelope["resourceChanges"], request["expiresAt"]
            else:
                if retry_published:
                    raise ValueError("Pending review is missing from private storage")
                payload, context, changes = self.runtime.plan()
                expires = min(utc(record["cleanupStartAt"]), self.clock() + timedelta(minutes=approval_minutes)).isoformat()
            self.guard(owner, epoch)
            return self.controller.propose(record["sessionId"], owner, epoch, stage, attempt,
                payload, context, changes, estimate, expires)
        # Fetch approval only from its IAM-protected prefix. Workflow inputs
        # cannot supply permission JSON or arbitrary object keys/versions.
        ref = record["pendingPlan"]
        envelope = load_review(self.runtime.clients["s3"], self.runtime.bucket, ref, self.clock())
        key = approval_key(envelope["request"], ref["sha256"])
        _, object_version = read_json(self.runtime.clients["s3"], self.runtime.bucket, key)
        approval_ref = dict(key=key, versionId=object_version, reviewSha256=ref["sha256"],
                            sessionId=record["sessionId"], stage=stage, attempt=attempt)
        def observe():
            self.guard(owner, epoch)
            return self.runtime.observe()
        return self.controller.apply(record["sessionId"], owner, epoch, approval_ref, observe, self.runtime.apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--stage", choices=("foundation", "edge"), required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--kms-key", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    import boto3
    from botocore.config import Config
    # ImportCertificate has no idempotency token: the SDK must not retry a lost
    # response internally before our durable intent can reconcile it.
    clients = {name: boto3.client(name, region_name="us-east-1",
        **({"config": Config(retries={"total_max_attempts": 1, "mode": "standard"})} if name == "acm" else {})) for name in
        ("s3", "sts", "ec2", "scheduler", "acm", "secretsmanager", "resourcegroupstaggingapi")}
    local = json.loads(args.execution.read_bytes())
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{local['sessionId']}.json", args.execution_version)
    if local != execution:
        raise ValueError("Local execution differs from the exact admitted S3 version")
    private, _ = read_json(clients["s3"], args.bucket, f"private/staging/{execution['sessionId']}/inputs.json")
    runtime = TerraformRuntime(args.execution.parent / "source", execution, private, args.bucket, args.kms_key, clients,
                               args.execution.parent / f"runtime-{args.stage}-{args.attempt}-{args.mode}")
    runtime.executor()  # Validate STS/EC2/egress before claiming a lease.
    # The instance ID comes from the validated private snapshot, never from a
    # caller-selected owner or IP. CIDR above is intentionally not the owner.
    owner = f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{private['deploymentExecutor']['instanceId']}"
    store = S3SessionStore(args.bucket)
    if args.worker:
        record, _ = store.read()
        lease = record.get("lease", {})
        result = LifecycleStageWorker(runtime, store, args.execution_version, task_id=args.task_id).perform(
            args.mode, args.stage, args.attempt, owner, lease.get("epoch"))
        print(json.dumps(result, sort_keys=True))
    else:
        command = [sys.executable, str(Path(__file__)), args.mode, "--stage", args.stage, "--attempt", str(args.attempt),
            "--execution", str(args.execution), "--execution-version", args.execution_version,
            "--bucket", args.bucket, "--kms-key", args.kms_key, "--task-id", args.task_id, "--worker"]
        execute(store, owner, "deploy", execution["sessionId"], command)


if __name__ == "__main__": main()
