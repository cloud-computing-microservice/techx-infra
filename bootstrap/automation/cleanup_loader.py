"""Trusted CodeBuild loader. This file is embedded in the reviewed project buildspec."""
import json
import os
from pathlib import Path
import re
import subprocess

from automation_bundle import MAX_BUNDLE_BYTES, extract
from cleanup_launcher import validate_execution


def load(client, bucket, session_id, version, directory):
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", session_id) or not version or version == "null":
        raise ValueError("An exact execution identity is required")
    response = client.get_object(Bucket=bucket, Key=f"executions/{session_id}.json", VersionId=version,
                                 ExpectedBucketOwner="058114477594")
    execution_bytes = response["Body"].read(256 * 1024 + 1)
    if response.get("VersionId") != version or len(execution_bytes) > 256 * 1024:
        raise ValueError("Execution read returned an unexpected version or oversized object")
    record = json.loads(execution_bytes)
    validate_execution(record, session_id, version)
    artifact = record["cleanupBundle"]
    if artifact["key"] != f"bundles/{artifact['sha256']}.tar.gz":
        raise ValueError("Bundle object name must match its content hash")
    source = client.get_object(Bucket=bucket, Key=artifact["key"], VersionId=artifact["versionId"],
                               ExpectedBucketOwner="058114477594")
    payload = source["Body"].read(MAX_BUNDLE_BYTES + 1)
    if source.get("VersionId") != artifact["versionId"] or len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError("Cleanup bundle version/size differs from the immutable execution")
    directory.mkdir(parents=True, exist_ok=False)
    bundle = directory / "source.tar.gz"
    bundle.write_bytes(payload)
    manifest = extract(bundle, directory / "source", artifact["sha256"])
    if manifest != record.get("sourceRevisions"):
        raise ValueError("Bundled revisions do not match the approved execution")
    (directory / "execution.json").write_text(json.dumps(record), encoding="utf-8")
    return directory / "source/techx-infra"


def main():
    import boto3
    if boto3.client("sts").get_caller_identity()["Account"] != "058114477594":
        raise ValueError("Cleanup executor is in the wrong AWS account")
    root = Path("/tmp/techx-cleanup-execution")
    bucket, session_id, version = (os.environ[key] for key in ("CONTROL_BUCKET", "TECHX_SESSION_ID", "TECHX_EXECUTION_VERSION"))
    infra = load(boto3.client("s3", region_name="us-east-1"), bucket, session_id, version, root)
    subprocess.run(["python", str(infra / "scripts/install_automation_tools.py")], check=True)
    subprocess.run(["python", str(infra / "scripts/run_cleanup.py"), "--execution", str(root / "execution.json"),
                    "--execution-version", version, "--bucket", bucket], check=True)


if __name__ == "__main__": main()
