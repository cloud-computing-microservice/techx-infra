"""GitHub preview/start entry point; preview has no AWS clients or credentials."""
import argparse
import json
import os
from datetime import timedelta, timezone
from pathlib import Path

from automation_session import S3SessionStore, utc
from chart_selection import GitHub
from lifecycle_admission import REPOS, StartWorkflow
from plan_review_store import checksum
from promotion_authority import from_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "start"))
    args = parser.parse_args()
    if (os.environ.get("GITHUB_REPOSITORY") != REPOS["techx-infra"] or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"):
        raise ValueError("Dispatch a new lifecycle run from trusted infra main")
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    if event_path.stat().st_size > 1024 * 1024: raise ValueError("Workflow event exceeds size policy")
    inputs = json.loads(event_path.read_bytes())["inputs"]
    kwargs = dict(bucket=os.environ["CONTROL_BUCKET"], kms_key=os.environ["CONTROL_KMS_KEY_ARN"])
    if args.mode == "start":
        import boto3
        from botocore.config import Config
        kwargs["clients"] = {name: boto3.client(name, region_name="us-east-1", config=Config(retries={"total_max_attempts": 1}))
            for name in ("s3", "sts", "scheduler", "codebuild", "lambda", "cloudfront", "logs", "kms")}
        kwargs["store"] = S3SessionStore(kwargs["bucket"])
    githubs = {name: GitHub(repo, os.environ["GITHUB_TOKEN"]) for name, repo in REPOS.items()}
    workflow = StartWorkflow(githubs, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_SHA"], Path(__file__).parents[2],
        promotion_check=lambda: from_environment(Path(os.environ.get("RUNNER_TEMP", "."))), **kwargs)
    if args.mode == "preview":
        request = workflow.preview(inputs)
        digest = checksum(request)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("request=" + json.dumps(request, separators=(",", ":"), sort_keys=True) + "\nrequest_sha256=" + digest + "\n")
        result = dict(request, requestSha256=digest)
        vietnam = timezone(timedelta(hours=7))
        result["deadlineVietnamTime"] = utc(request["record"]["deadline"]).astimezone(vietnam).isoformat()
        result["cleanupStartVietnamTime"] = utc(request["record"]["cleanupStartAt"]).astimezone(vietnam).isoformat()
        heading = "Opening preview: review budget, sources, backend and fixed deadline"
    else:
        value = os.environ["START_REQUEST_JSON"]
        if len(value.encode()) > 1024 * 1024: raise ValueError("Opening request exceeds size policy")
        result = workflow.start(inputs, json.loads(value), os.environ["START_REQUEST_SHA256"])
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            for key, name in (("sessionId", "session_id"), ("executionVersion", "execution_version"), ("buildId", "build_id")):
                output.write(name + "=" + result[key] + "\n")
        heading = "Session admitted; independent AWS cleanup armed before manager dispatch"
    # Request/summary deliberately omit the private budget email. No artifact
    # upload step publishes execution inputs, plans, secrets or Terraform state.
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write("## " + heading + "\n\n```json\n" + json.dumps(result, indent=2, sort_keys=True) + "\n```\n")
    if args.mode == "start": print(json.dumps(result, sort_keys=True))


if __name__ == "__main__": main()
