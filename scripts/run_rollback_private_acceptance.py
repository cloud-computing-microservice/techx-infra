"""Fixed private rollback suite and terminal, atomic accepted READY handoff."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from accepted_rollback_history import finalize_rollback
from automation_session import S3SessionStore
from deployment_runtime import ACCOUNT, TerraformRuntime
from leased_command import execute
from plan_review_store import read_json
from rollback_private_acceptance import RollbackPrivateAcceptance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("verify",))
    parser.add_argument("--stage", choices=("rollback",), required=True)
    parser.add_argument("--attempt", type=int, choices=(1,), required=True)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--kms-key", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1, "mode": "standard"})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in
        ("s3", "sts", "ec2", "eks", "cloudfront", "scheduler", "secretsmanager", "ecr", "dynamodb", "elbv2", "resourcegroupstaggingapi")}
    local = json.loads(args.execution.read_bytes())
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{local['sessionId']}.json", args.execution_version)
    if execution != local: raise ValueError("Local execution differs from the admitted S3 version")
    private, _ = read_json(clients["s3"], args.bucket, f"private/staging/{execution['sessionId']}/inputs.json")
    runtime = TerraformRuntime(args.execution.parent / "source", execution, private, args.bucket, args.kms_key, clients,
        args.execution.parent / "runtime-rollback-private")
    runtime.executor()
    owner = f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{private['deploymentExecutor']['instanceId']}"
    store = S3SessionStore(args.bucket)
    if args.worker:
        record, _ = store.read()
        result = RollbackPrivateAcceptance(runtime, store, args.execution_version, args.task_id,
            clock=lambda: datetime.now(timezone.utc)).perform(owner, record.get("lease", {}).get("epoch"))
        print(json.dumps(result, sort_keys=True))
    else:
        command = [sys.executable, str(Path(__file__)), args.mode, "--stage", args.stage, "--attempt", str(args.attempt),
            "--execution", str(args.execution), "--execution-version", args.execution_version, "--bucket", args.bucket,
            "--kms-key", args.kms_key, "--task-id", args.task_id, "--worker"]
        execute(store, owner, "rollback", execution["sessionId"], command, success_status="READY",
            rollback_ready=lambda record: finalize_rollback(clients["s3"], args.bucket, record, args.execution_version))


if __name__ == "__main__": main()
