"""Leased cold publisher entry point for a protected disposable GitHub runner."""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile
import subprocess
import os

from automation_session import S3SessionStore, claim
from bootstrap_publish import ACCOUNT, OWNER, ROLE, ColdPublisher
from bootstrap_provenance import PLATFORM_REPO, publisher_proof
from chart_selection import GitHub
from leased_command import execute
from plan_review_store import read_json
from plan_approval import version


def verified_checkout(root, expected, run=subprocess.run):
    """The workflow checkout is fixed too; the archive separately pins builds."""
    root = root.resolve()
    top = run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True).stdout.strip()
    actual = run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if Path(top).resolve() != root or actual != expected:
        raise ValueError("Automation checkout differs from admitted infra source")
    run(["git", "-C", str(root), "diff", "--exit-code", "HEAD", "--"], check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--propose-chart", action="store_true")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", args.session_id): raise ValueError("Invalid cold session")
    version(args.execution_version)
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "scheduler", "ssm", "codebuild", "lambda")}
    identity = clients["sts"].get_caller_identity()
    if (not re.fullmatch(OWNER, args.owner) or identity.get("Account") != ACCOUNT or not re.fullmatch(ROLE, identity.get("Arn", ""))
            or not identity["Arn"].endswith(f"/techx-bootstrap-{args.owner.split(':')[1]}")):
        raise ValueError("This entry point requires the protected bootstrap publisher role")
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", args.execution_version)
    if execution.get("sessionId") != args.session_id:
        raise ValueError("The admitted execution belongs to another session")
    verified_checkout(Path(__file__).parents[1], execution["sourceRevisions"]["techx-infra"])
    github = GitHub(PLATFORM_REPO, os.environ["GITHUB_TOKEN"])
    publisher_proof(github, execution, args.owner)
    store = S3SessionStore(args.bucket)
    current, _ = store.read()
    if current.get("sessionId") != args.session_id or current.get("executionVersion") != args.execution_version:
        raise ValueError("A stale publisher cannot operate on the current session")
    if args.worker:
        if args.directory is None: raise ValueError("The leased child needs its fresh extraction directory")
        record, _ = store.read()
        publisher = ColdPublisher(clients, store, args.bucket, execution, args.execution_version, args.owner,
            record.get("lease", {}).get("epoch"), github=github)
        if args.propose_chart:
            from bootstrap_proposal import ChartProposal, ChartWriter
            source = publisher.source(args.directory / "archive").parent
            result = ChartProposal(publisher, ChartWriter(os.environ["GH_TOKEN"])).perform(source)
        else:
            result = publisher.perform(args.directory)
        print(json.dumps(result, sort_keys=True))
    else:
        # Reject stale/closed/not-yet-applied sessions before entering the
        # failure handler; they must not launch a paid cleanup for an old run.
        claim(current, args.owner, "bootstrap-publish")
        ColdPublisher(clients, store, args.bucket, execution, args.execution_version, args.owner, 1,
                      github=github).admission(current)
        if args.propose_chart:
            from chart_selection import read_release
            read_release(clients["s3"], args.bucket, current, execution, args.execution_version)
        try:
            with tempfile.TemporaryDirectory(prefix="techx-cold-publish-") as directory:
                command = [sys.executable, str(Path(__file__).resolve()), "--session-id", args.session_id,
                    "--execution-version", args.execution_version, "--bucket", args.bucket, "--owner", args.owner,
                    "--worker", "--directory", str(Path(directory) / "run")]
                if args.propose_chart: command.append("--propose-chart")
                execute(store, args.owner, "bootstrap-publish", args.session_id, command)
        except BaseException:
            clients["lambda"].invoke(FunctionName=f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
                InvocationType="Event", Payload=json.dumps(dict(sessionId=args.session_id, executionVersion=args.execution_version, requestedEarly=True)).encode())
            raise


if __name__ == "__main__": main()
