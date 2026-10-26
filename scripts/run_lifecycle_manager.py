"""Lifecycle CodeBuild entry point from its verified immutable source bundle."""
import argparse
import json
import os
from pathlib import Path

from automation_session import S3SessionStore
from deployment_executor import ExecutorFactory
from deployment_manager import DeploymentManager
from plan_review_store import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "plan", "apply", "bootstrap", "promote", "activate", "verify"))
    parser.add_argument("--stage", choices=("foundation", "edge", "gitops", "chart", "exposure", "private"), default="foundation")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "ec2", "ssm", "scheduler", "lambda")}
    local = json.loads(args.execution.read_bytes())
    record, _ = read_json(clients["s3"], args.bucket, f"executions/{local['sessionId']}.json", args.execution_version)
    if record != local: raise ValueError("Local execution differs from the exact admitted S3 version")
    manager = DeploymentManager(clients, S3SessionStore(args.bucket), record, args.execution_version, args.bucket,
                                 os.environ.get("CODEBUILD_BUILD_ARN", ""))
    try:
        if args.mode == "prepare":
            private = ExecutorFactory(manager).prepare()
            print(json.dumps(dict(status="EXECUTOR_PREPARED", instanceId=private["deploymentExecutor"]["instanceId"])))
        else:
            private, _ = read_json(clients["s3"], args.bucket, f"private/staging/{record['sessionId']}/inputs.json")
            pointer = manager.dispatch(private, args.mode, args.stage, args.attempt)
            completed = manager.wait(pointer)
            print(json.dumps(dict(status=completed["state"], taskId=completed["taskId"], stage=completed["stage"], attempt=completed["attempt"])))
    except BaseException:
        manager.request_cleanup()
        raise


if __name__ == "__main__": main()
