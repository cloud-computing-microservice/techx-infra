"""NO_SOURCE CodeBuild loader for lifecycle manager tasks; no branch checkout."""
import os
from pathlib import Path
import re
import subprocess

from cleanup_loader import load


def command(infra, directory, bucket, version, environment):
    mode = environment.get("TECHX_MODE", "prepare")
    stage = environment.get("TECHX_PLAN_STAGE", "foundation")
    attempt = environment.get("TECHX_PLAN_ATTEMPT", "1")
    valid = mode in ("prepare", "plan", "apply") and stage in ("foundation", "edge") or (mode, stage, attempt) == ("bootstrap", "gitops", "1")
    if not valid or not re.fullmatch(r"[1-9][0-9]{0,5}", attempt):
        raise ValueError("Unsupported typed lifecycle operation")
    return ["python", str(infra / "scripts/run_lifecycle_manager.py"), mode, "--stage", stage, "--attempt", attempt,
            "--execution", str(directory / "execution.json"), "--execution-version", version, "--bucket", bucket]


def main():
    import boto3
    identity = boto3.client("sts", region_name="us-east-1").get_caller_identity()
    if identity.get("Account") != "058114477594" or not re.fullmatch(
            r"arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-manager/[^/]+", identity.get("Arn", "")):
        raise ValueError("Dedicated lifecycle manager role required")
    directory = Path("/tmp/techx-lifecycle-execution")
    bucket, session, version = (os.environ[key] for key in ("CONTROL_BUCKET", "TECHX_SESSION_ID", "TECHX_EXECUTION_VERSION"))
    infra = load(boto3.client("s3", region_name="us-east-1"), bucket, session, version, directory)
    subprocess.run(command(infra, directory, bucket, version, os.environ), check=True)


if __name__ == "__main__": main()
