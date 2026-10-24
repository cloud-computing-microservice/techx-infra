"""Leased cold publisher entry point for a protected disposable GitHub runner."""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

from automation_session import S3SessionStore, claim
from bootstrap_publish import ACCOUNT, OWNER, ROLE, ColdPublisher
from leased_command import execute
from plan_review_store import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "scheduler", "ssm", "lambda")}
    identity = clients["sts"].get_caller_identity()
    if (not re.fullmatch(OWNER, args.owner) or identity.get("Account") != ACCOUNT or not re.fullmatch(ROLE, identity.get("Arn", ""))
            or not identity["Arn"].endswith(f"/techx-bootstrap-{args.owner.split(':')[1]}")):
        raise ValueError("This entry point requires the protected bootstrap publisher role")
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    if execution.get("sessionId") != args.session_id:
        raise ValueError("The admitted execution belongs to another session")
    store = S3SessionStore(args.bucket)
    current, _ = store.read()
    if current.get("sessionId") != args.session_id or current.get("executionVersion") != args.execution_version:
        raise ValueError("A stale publisher cannot operate on the current session")
    if args.worker:
        if args.directory is None: raise ValueError("The leased child needs its fresh extraction directory")
        record, _ = store.read()
        publisher = ColdPublisher(clients, store, args.bucket, execution, args.execution_version, args.owner,
            record.get("lease", {}).get("epoch"))
        print(json.dumps(publisher.perform(args.directory), sort_keys=True))
    else:
        # Reject stale/closed/not-yet-applied sessions before entering the
        # failure handler; they must not launch a paid cleanup for an old run.
        claim(current, args.owner, "bootstrap-publish")
        try:
            with tempfile.TemporaryDirectory(prefix="techx-cold-publish-") as directory:
                command = [sys.executable, str(Path(__file__).resolve()), "--session-id", args.session_id,
                    "--execution-version", args.execution_version, "--bucket", args.bucket, "--owner", args.owner,
                    "--worker", "--directory", str(Path(directory) / "run")]
                execute(store, args.owner, "bootstrap-publish", args.session_id, command)
        except BaseException:
            clients["lambda"].invoke(FunctionName=f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
                InvocationType="Event", Payload=json.dumps(dict(sessionId=args.session_id, executionVersion=args.execution_version, requestedEarly=True)).encode())
            raise


if __name__ == "__main__": main()
