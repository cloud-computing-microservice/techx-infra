"""Trusted AMI loader invoked only by the pinned one-parameter SSM document.

The AMI packages this module, automation_bundle.py, cleanup_launcher.py and
cleanup_loader.py from the reviewed bootstrap source. Workload code is imported
only after exact-version download, archive checksum and source manifest checks.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from cleanup_loader import load

ACCOUNT = "058114477594"


def read_json(client, bucket, key, version=None):
    args = dict(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
    if version:
        args["VersionId"] = version
    response = client.get_object(**args)
    payload = response["Body"].read(256 * 1024 + 1)
    if len(payload) > 256 * 1024 or (version and response.get("VersionId") != version):
        raise ValueError("Task loader received an oversized or wrong-version object")
    return json.loads(payload)


def admitted_task(client, bucket, task_id, instance_id, now):
    if not re.fullmatch(r"[a-f0-9]{32}", task_id):
        raise ValueError("Invalid task identity")
    record = read_json(client, bucket, "automation/staging/session.json")
    if (record.get("account") != ACCOUNT or record.get("region") != "us-east-1" or record.get("environment") != "staging"
            or record.get("status") not in ("PLANNING", "PROVISIONING", "TESTING", "FAILED_NEEDS_CLEANUP")):
        raise ValueError("There is no active deployment session for this task")
    pointer = record.get("deploymentTask", {})
    if (record["status"] == "FAILED_NEEDS_CLEANUP") != ((pointer.get("mode"), pointer.get("stage")) == ("rollback", "prepare")):
        raise ValueError("Failed sessions admit only the exact reserved rollback preparation")
    if (record["status"] == "TESTING") != ((pointer.get("mode"), pointer.get("stage")) in (("release", "active"), ("verify", "active"))):
        raise ValueError("TESTING admits only bound active rollout/private verification tasks")
    ref = pointer.get("reference", {})
    if (pointer.get("taskId") != task_id or pointer.get("instanceId") != instance_id or pointer.get("state") != "DISPATCHING"
            or ref.get("key") != f"control/staging/tasks/{task_id}.json" or not ref.get("versionId") or ref["versionId"] == "null"):
        raise ValueError("Task is stale, consumed or not assigned to this instance")
    task = read_json(client, bucket, ref["key"], ref["versionId"])
    digest = hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if (digest != ref.get("sha256") or task.get("schemaVersion") != 1 or task.get("taskId") != task_id
            or task.get("sessionId") != record.get("sessionId") or task.get("executionVersion") != record.get("executionVersion")
            or task.get("instanceId") != instance_id
            or (task.get("mode"), task.get("stage")) not in {("plan", "foundation"), ("apply", "foundation"), ("plan", "edge"), ("apply", "edge"), ("bootstrap", "gitops"), ("promote", "chart"), ("activate", "exposure"), ("verify", "private"), ("verify", "active"), ("release", "active"), ("rollback", "prepare")}
            or type(task.get("attempt")) is not int or task["attempt"] < 1):
        raise ValueError("Task intent differs from the admitted pointer")
    def utc(value):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None: raise ValueError("Task timestamps must include a timezone")
        return value.astimezone(timezone.utc)
    if not utc(task["createdAt"]) <= now < utc(task["expiresAt"]) <= utc(record["cleanupStartAt"]):
        raise ValueError("Task is expired or the cleanup window has started")
    if record.get("lease"):
        raise ValueError("Manager has not handed off the deployment lease")
    return task


def run_task(clients, bucket, kms, task_id, base, *, run=subprocess.run, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    identity = clients["sts"].get_caller_identity()
    match = re.fullmatch(rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-executor/(i-[a-f0-9]{{8,17}})", identity.get("Arn", ""))
    if identity.get("Account") != ACCOUNT or not match:
        raise ValueError("Only the dedicated EC2 instance role may load a task")
    task = admitted_task(clients["s3"], bucket, task_id, match[1], clock())
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    # Every delivery gets a new extraction path. Exactly one may pass the later
    # S3 task CAS; duplicate deliveries cannot run the stage or mark it failed.
    directory = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=base)) / "execution"
    infra = load(clients["s3"], bucket, task["sessionId"], task["executionVersion"], directory)
    directory.chmod(0o700)
    sys.path.insert(0, str(infra / "scripts"))
    # These imports are from the hash-verified immutable bundle, not branch head.
    from automation_session import S3SessionStore
    from deployment_manager import inspect_instance
    from deployment_tasks import claim_task, finish_task
    from deployment_runtime import TerraformRuntime
    execution = json.loads((directory / "execution.json").read_bytes())
    private = read_json(clients["s3"], bucket, f"private/staging/{task['sessionId']}/inputs.json")
    runtime = TerraformRuntime(directory / "source", execution, private, bucket, kms, clients, directory / "loader-runtime")
    inspect_instance(clients["ec2"], execution, private)
    runtime.executor()
    store = S3SessionStore(bucket)
    task = claim_task(store, clients["s3"], bucket, task_id, match[1], clock())
    log = directory / "task.private.log"
    succeeded = False
    try:
        with log.open("xb") as output:
            log.chmod(0o600)
            worker = {"bootstrap": "run_gitops_bootstrap.py", "promote": "run_chart_promotion.py", "activate": "run_edge_activation.py", "verify": "run_private_acceptance.py", "release": "run_active_rollout.py"}.get(task["mode"], "run_lifecycle_stage.py")
            if (task["mode"], task["stage"]) == ("verify", "active"): worker = "run_active_private_acceptance.py"
            if (task["mode"], task["stage"]) == ("rollback", "prepare"): worker = "run_rollback_prepare.py"
            run([sys.executable, str(infra / "scripts" / worker), task["mode"], "--stage", task["stage"],
                 "--attempt", str(task["attempt"]), "--execution", str(directory / "execution.json"),
                 "--execution-version", task["executionVersion"], "--bucket", bucket, "--kms-key", kms,
                 "--task-id", task_id], check=True, stdout=output, stderr=subprocess.STDOUT)
        finish_task(store, task, True, clock())
        succeeded = True
    finally:
        if not succeeded:
            try: finish_task(store, task, False, clock())
            except Exception: pass  # Preserve the original error and any cleanup takeover.
        if log.exists():
            clients["s3"].put_object(Bucket=bucket, ExpectedBucketOwner=ACCOUNT,
                Key=f"private/staging/{task['sessionId']}/tasks/{task_id}/task.private.log",
                Body=log.read_bytes(), ServerSideEncryption="aws:kms", SSEKMSKeyId=kms)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--kms-key", required=True)
    args = parser.parse_args()
    import boto3
    clients = {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "ec2")}
    try:
        run_task(clients, args.bucket, args.kms_key, args.task_id, Path("/var/lib/techx/tasks"))
    except Exception as error:
        print(f"Deployment task failed: {type(error).__name__}. Inspect private task evidence and cleanup.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__": main()
