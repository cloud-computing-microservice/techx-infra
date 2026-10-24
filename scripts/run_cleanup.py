"""Independent cleanup entry point for a verified immutable execution bundle."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

from automation_session import S3SessionStore, claim, transition
from automation_inputs import validate_variables
from cleanup_execution import (assert_inventory, delete_session_secrets, fence_session, remove_owned_shop,
                               restore_cleanup_access, stop_executor, terminate_deployment_executors, terminate_probes)
from cloudflare_dns import CloudflareDNS, HOST, validate_name_target
from leased_command import execute


def persist_diagnostics(clients, bucket, session_id, owner, directory):
    build_key = hashlib.sha256(owner.encode()).hexdigest()[:20]
    infra = Path(__file__).parents[1]
    candidates = [directory / "teardown.private.log"] + list((infra / ".plans").glob("*.private.json")) + list((infra / ".plans").glob("*.tfplan"))
    for path in candidates:
        if path.is_file():
            clients["s3"].put_object(Bucket=bucket, Key=f"cleanup/{session_id}/diagnostics/{build_key}/{path.name}",
                                     Body=path.read_bytes(), ExpectedBucketOwner="058114477594")


def aws_clients():
    import boto3
    return {name: boto3.client(name, region_name="us-east-1") for name in ("s3", "sts", "eks", "codebuild", "ec2", "secretsmanager")}


def read_private_snapshot(clients, record, bucket):
    response = clients["s3"].get_object(Bucket=bucket, Key=f"private/staging/{record['sessionId']}/inputs.json", ExpectedBucketOwner="058114477594")
    body = response["Body"].read(256 * 1024 + 1)
    if len(body) > 256 * 1024: raise ValueError("Private cleanup snapshot exceeds size policy")
    private = json.loads(body)
    if private.get("sessionId") != record["sessionId"] or private.get("schemaVersion") != 1:
        raise ValueError("Private cleanup snapshot belongs to another session")
    return private


def prepare_takeover(clients, record, owner, bucket):
    private = read_private_snapshot(clients, record, bucket)
    lease = record.get("lease", {})
    previous = lease.get("owner")
    expected_instance = None
    stopped_managers = set()
    if previous and previous != owner and lease.get("operation") != "release":
        match = re.fullmatch(r"arn:aws:ec2:us-east-1:058114477594:instance/(i-[a-f0-9]{8,17})", previous)
        if match:
            expected_instance = match[1]
        else:
            stop_executor(clients["codebuild"], previous, owner)
            stopped_managers.add(previous)
    # A manager may save launch intent or an instance ID while we stop it.
    # S3's fresh read, after confirmed terminal status, is authoritative.
    original_launch = private.get("deploymentLaunch")
    private = read_private_snapshot(clients, record, bucket)
    launch = private.get("deploymentLaunch")
    if original_launch is not None and (launch is None or
            any(original_launch.get(key) != launch.get(key) for key in ("clientToken", "managerArn"))):
        raise ValueError("Executor launch identity changed during cleanup takeover")
    if launch and launch.get("managerArn"):
        manager = launch["managerArn"]
        if not re.fullmatch(r"arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:[a-f0-9-]+", manager):
            raise ValueError("Launch intent must identify the fixed lifecycle manager")
        if manager not in stopped_managers:
            stop_executor(clients["codebuild"], manager, owner)
        private = read_private_snapshot(clients, record, bucket)
        fresh = private.get("deploymentLaunch")
        if fresh is None or any(fresh.get(key) != launch.get(key) for key in ("clientToken", "managerArn")):
            raise ValueError("Executor launch identity changed after manager termination")
        launch = fresh
    recorded = private.get("deploymentExecutor", {}).get("instanceId")
    if recorded:
        if expected_instance is not None and expected_instance != recorded:
            raise ValueError("Lease executor differs from its private launch snapshot")
        expected_instance = recorded
    # Do this even when no lease exists: an idle approval-waiting executor still
    # costs money and a detached remote command may outlive its manager.
    token, proof = None, None
    proof_key = f"cleanup/{record['sessionId']}/executor-proof.json"
    if launch is not None:
        token = launch.get("clientToken", "")
        if not re.fullmatch(r"[a-f0-9]{64}", token): raise ValueError("Invalid executor launch intent")
        if launch.get("protocolVersion") == 1 and launch.get("phase") == "PREPARED":
            if not launch.get("managerArn") or private.get("deploymentExecutor") or expected_instance is not None:
                raise ValueError("Pre-API intent conflicts with an executor reference")
            observed = terminate_deployment_executors(clients["ec2"], record["sessionId"])
            if observed["instanceIds"]:
                raise ValueError("Pre-API intent conflicts with an observed executor")
            proof = dict(schemaVersion=1, executionVersion=record["executionVersion"], clientToken=token,
                managerArn=launch["managerArn"], launchNotInvoked=True, instanceIds=[], terminated=True,
                checkedAt=datetime.now(timezone.utc).isoformat())
            clients["s3"].put_object(Bucket=bucket, Key=proof_key, ExpectedBucketOwner="058114477594",
                                    Body=json.dumps(proof).encode(), ContentType="application/json")
            return fence_session(record, owner, executor_stopped=True)
        try:
            result = clients["s3"].get_object(Bucket=bucket, Key=proof_key, ExpectedBucketOwner="058114477594")
            proof_body = result["Body"].read(16 * 1024 + 1)
            if len(proof_body) > 16 * 1024: raise ValueError("Oversized executor termination proof")
            proof = json.loads(proof_body)
        except clients["s3"].exceptions.NoSuchKey:
            pass
        if proof is not None:
            if (proof.get("schemaVersion") != 1 or proof.get("executionVersion") != record.get("executionVersion")
                    or proof.get("clientToken") != token or proof.get("terminated") is not True
                    or not isinstance(proof.get("instanceIds"), list) or not proof["instanceIds"]
                    or any(not re.fullmatch(r"i-[a-f0-9]{8,17}", item) for item in proof["instanceIds"])
                    or (private.get("deploymentExecutor", {}).get("instanceId") is not None
                        and private["deploymentExecutor"]["instanceId"] not in proof["instanceIds"])
                    or (expected_instance is not None and expected_instance not in proof["instanceIds"])):
                raise ValueError("Executor termination proof does not match the admitted launch")
    if token is None:
        terminate_deployment_executors(clients["ec2"], record["sessionId"], expected_instance)
    else:
        observed = terminate_deployment_executors(clients["ec2"], record["sessionId"],
            None if proof is not None else expected_instance, launch_token=None if proof is not None else token)
        if proof is None:
            proof = dict(schemaVersion=1, executionVersion=record["executionVersion"], clientToken=token,
                         instanceIds=observed["instanceIds"], terminated=True, checkedAt=datetime.now(timezone.utc).isoformat())
            clients["s3"].put_object(Bucket=bucket, Key=proof_key, ExpectedBucketOwner="058114477594",
                                    Body=json.dumps(proof).encode(), ContentType="application/json")
    return fence_session(record, owner, executor_stopped=True)


def work(args, clients, record, directory):
    session_id = record["sessionId"]
    response = clients["s3"].get_object(Bucket=args.bucket, Key=f"private/staging/{session_id}/inputs.json",
                                        ExpectedBucketOwner="058114477594")
    private = json.loads(response["Body"].read())
    if (private.get("sessionId") != session_id or private.get("schemaVersion") != 1
            or type(private.get("dnsMutationAttempted")) is not bool or not isinstance(private.get("validationDnsPlans"), list)):
        raise ValueError("Private input/DNS intent snapshot is invalid")
    variables = validate_variables(dict(private["terraformVars"]))
    for plan in private["validationDnsPlans"]:
        if validate_name_target(plan["name"], plan["target"])[0] == HOST:
            raise ValueError("Validation intents cannot contain the storefront record")
    dns_client = None
    def get_dns():
        nonlocal dns_client
        if dns_client is None:
            secret = clients["secretsmanager"].get_secret_value(SecretId=os.environ["CLOUDFLARE_SECRET_ARN"])
            credentials = json.loads(secret["SecretString"])
            dns_client = CloudflareDNS(credentials["zoneId"], credentials["apiToken"])
        return dns_client
    if private["dnsMutationAttempted"]: remove_owned_shop(get_dns(), session_id)
    # Recursive caches and every authoritative nameserver must converge before edge deletion.
    for attempt in range(31):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("public-dns.py"))], capture_output=True, text=True)
        if result.returncode == 0: break
        if attempt == 30: raise ValueError("DNS removal was not confirmed; edge teardown refused")
        time.sleep(10)
    varfile = directory / "cleanup.private.tfvars.json"
    varfile.write_text(json.dumps(variables), encoding="utf-8")
    infra = Path(__file__).parents[1]
    if private.get("certificateState"):
        (infra / "certificate-state-staging.private.json").write_text(json.dumps(private["certificateState"]), encoding="utf-8")
    terminate_probes(clients["ec2"], session_id)
    delete_session_secrets(clients["secretsmanager"], session_id)
    with urlopen("https://checkip.amazonaws.com", timeout=15) as response: caller = response.read().decode().strip()
    restore_cleanup_access(clients["eks"], caller)
    with (directory / "teardown.private.log").open("w", encoding="utf-8") as log:
        subprocess.run(["pwsh", "-NoProfile", "-File", str(infra / "scripts/destroy-domain-vpn-environment.ps1"),
                        "-Environment", "staging", "-ExpectedAccountId", "058114477594", "-ConfirmDestroy",
                        "-PrivateVarFile", str(varfile)], check=True, cwd=infra, stdout=log, stderr=subprocess.STDOUT)
    for plan in private["validationDnsPlans"]:
        name, target = validate_name_target(plan["name"], plan["target"])
        dns_client = get_dns()
        records = dns_client.records(name)
        if not records: continue
        if len(records) != 1 or records[0].get("comment") != f"techx:{session_id}" or records[0]["content"].rstrip(".").lower() != target:
            raise ValueError("Validation DNS ownership changed; deletion refused")
        dns_client.delete(dict(id=records[0]["id"], name=name, content=target, sessionId=session_id))
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(infra / "scripts/domain-vpn-inventory.ps1")],
                            check=True, capture_output=True, text=True)
    inventory = json.loads(result.stdout)
    assert_inventory(inventory)
    (directory / "inventory.json").write_text(json.dumps(inventory, sort_keys=True), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", type=Path, required=True)
    parser.add_argument("--execution-version", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    clients = aws_clients()
    if clients["sts"].get_caller_identity()["Account"] != "058114477594": raise ValueError("Unexpected cleanup AWS account")
    owner = os.environ.get("CODEBUILD_BUILD_ARN", "")
    if not re.fullmatch(r"arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-cleanup:[a-f0-9-]+", owner):
        raise ValueError("This cleanup entry point requires the dedicated CodeBuild executor")
    record = json.loads(args.execution.read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(__file__).parents[1] / "bootstrap/automation"))
    from cleanup_launcher import validate_execution
    validate_execution(record, record["sessionId"], args.execution_version)
    session_id = record["sessionId"]
    store = S3SessionStore(args.bucket)
    current, etag = store.read()
    if current["sessionId"] != session_id: raise ValueError("Stale cleanup cannot touch a newer staging session")
    directory = args.execution.parent
    if args.worker:
        if current.get("lease", {}).get("owner") != owner or current.get("lease", {}).get("operation") != "cleanup":
            raise ValueError("Cleanup worker does not own the lifecycle lease")
        try:
            work(args, clients, record, directory)
        except BaseException:
            try: persist_diagnostics(clients, args.bucket, session_id, owner, directory)
            except Exception as error: print(f"Private cleanup diagnostic upload failed: {type(error).__name__}", file=sys.stderr)
            raise
        persist_diagnostics(clients, args.bucket, session_id, owner, directory)
        return
    lease = current.get("lease", {})
    fenced = prepare_takeover(clients, current, owner, args.bucket)
    store.write(fenced, etag)
    if lease.get("operation") == "release": time.sleep(35)  # Allow the publisher's bounded heartbeat to stop its child.
    command = [sys.executable, str(Path(__file__)), "--execution", str(args.execution),
               "--execution-version", args.execution_version, "--bucket", args.bucket, "--worker"]
    execute(store, owner, "cleanup", session_id, command)
    payload = (directory / "inventory.json").read_bytes()
    inventory = json.loads(payload)
    assert_inventory(inventory)
    digest = hashlib.sha256(payload).hexdigest()
    evidence_key = f"cleanup/{session_id}/inventory-{digest}.json"
    clients["s3"].put_object(Bucket=args.bucket, Key=evidence_key, Body=payload, ExpectedBucketOwner="058114477594")
    current, etag = store.read()
    owned = claim(current, owner, "cleanup")
    owned = transition(owned, owner, "DESTROYING")
    owned["cleanupEvidence"] = dict(inventoryEmpty=True, artifactSha256=digest, key=evidence_key)
    completed = transition(owned, owner, "DESTROYED")
    store.write(completed, etag)
    marker = dict(executionVersion=args.execution_version, inventoryEmpty=True, artifactSha256=digest,
                  checkedAt=datetime.now(timezone.utc).isoformat(), inventoryKey=evidence_key)
    clients["s3"].put_object(Bucket=args.bucket, Key=f"cleanup/{session_id}/complete.json",
                             Body=json.dumps(marker).encode(), ExpectedBucketOwner="058114477594")
    print("Staging workload teardown and scoped inventory verification completed.")


if __name__ == "__main__": main()
