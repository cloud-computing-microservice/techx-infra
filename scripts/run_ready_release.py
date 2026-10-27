"""Protected hosted READY publisher; proposes a candidate without merging it."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from automation_session import S3SessionStore, claim
from bootstrap_proposal import ChartWriter
from chart_selection import GitHub, PLATFORM_REPO
from leased_command import execute
from plan_review_store import read_json
from ready_release_publish import ReadyProposal, ReadyPublisher
from run_bootstrap_publish import verified_checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--platform", required=True, type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1})
    clients = {name: boto3.client(name, region_name="us-east-1", config=config) for name in ("s3", "sts", "scheduler", "ecr")}
    store = S3SessionStore(args.bucket)
    current, _ = store.read()
    if current.get("sessionId") != args.session_id: raise ValueError("Current session changed before publishing")
    if current["status"] in ("OFF", "DESTROYED"):
        print(json.dumps(dict(state="NO_ACTIVE_SESSION")))
        return
    execution_version = current["executionVersion"]
    execution, _ = read_json(clients["s3"], args.bucket, f"executions/{args.session_id}.json", execution_version)
    verified_checkout(Path(__file__).parents[1], execution["sourceRevisions"]["techx-infra"])
    verified_checkout(args.platform, args.source_sha)
    github = GitHub(PLATFORM_REPO, os.environ["GITHUB_TOKEN"])
    publisher = ReadyPublisher(clients, store, args.bucket, execution, execution_version, args.source_sha, args.owner, github=github)
    publisher.admission(current)
    if args.worker:
        publisher.epoch = current.get("lease", {}).get("epoch")
        with tempfile.TemporaryDirectory(prefix="techx-active-release-") as directory:
            publisher.publish(args.platform, directory)
            result = ReadyProposal(publisher, ChartWriter(os.environ["GH_TOKEN"])).perform(args.platform.parent)
        print(json.dumps(dict(state="RELEASE_PROPOSED", releaseId=publisher.release_id, **result), sort_keys=True))
    else:
        claim(current, args.owner, "release")
        command = [sys.executable, str(Path(__file__).resolve()), "--bucket", args.bucket, "--session-id", args.session_id,
            "--source-sha", args.source_sha, "--owner", args.owner, "--platform", str(args.platform.resolve()), "--worker"]
        execute(store, args.owner, "release", args.session_id, command)


if __name__ == "__main__":
    try: main()
    except Exception as error:
        print(f"Active release failed: {type(error).__name__}. Candidate receipts and the accepted baseline are retained.", file=sys.stderr)
        raise SystemExit(1) from None
