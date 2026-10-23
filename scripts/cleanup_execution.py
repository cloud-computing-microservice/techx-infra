"""Bounded recovery and inventory checks used by the independent cleanup executor."""
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import time
from urllib.request import urlopen

from automation_session import validate
from cloudflare_dns import CloudflareDNS, HOST

ACCOUNT = "058114477594"
CLEANUP_ROLE = f"arn:aws:iam::{ACCOUNT}:role/techx-automation-cleanup-executor"


def assert_inventory(inventory):
    if (inventory.get("accountId") != ACCOUNT or inventory.get("environment") != "staging" or inventory.get("region") != "us-east-1"):
        raise ValueError("Inventory scope mismatch")
    fields = json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text())
    for name in fields:
        if not isinstance(inventory.get(name), list) or inventory[name]:
            raise ValueError(f"Inventory is missing/invalid or nonempty: {name}")


def fence_session(record, owner, executor_stopped=False):
    """Only release-only publishers can be logically fenced without host control.

    They cannot create infrastructure, namespaces or ECR repositories. Changing
    status/epoch closes promotion and their next heartbeat terminates the command.
    Infrastructure/acceptance/cleanup executors require AWS terminal-state proof.
    """
    validate(record)
    lease = record.get("lease", {})
    if lease.get("owner") and lease["owner"] != owner and lease.get("operation") != "release" and not executor_stopped:
        raise ValueError("Infrastructure executor must be confirmed stopped before cleanup takeover")
    result = dict(record, status="FAILED_NEEDS_CLEANUP", leaseEpoch=record.get("leaseEpoch", lease.get("epoch", 0)) + 1)
    result.pop("lease", None)
    result["cleanupFence"] = dict(previousOwner=lease.get("owner"), at=datetime.now(timezone.utc).isoformat())
    return result


def stop_executor(client, owner, current_owner, sleep=time.sleep):
    if owner == current_owner: return True
    pattern = r"arn:aws:codebuild:us-east-1:058114477594:build/(techx-automation-(?:lifecycle|cleanup)):[a-f0-9-]+"
    if not re.fullmatch(pattern, owner): raise ValueError("Unknown infrastructure executor identity")
    build_id = owner.split("build/", 1)[1]
    for attempt in range(19):
        response = client.batch_get_builds(ids=[build_id])
        builds = response.get("builds", [])
        if response.get("buildsNotFound") or len(builds) != 1 or builds[0].get("arn") != owner:
            raise ValueError("Previous executor status is unknown")
        status = builds[0].get("buildStatus")
        if status in ("SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"): return True
        if status != "IN_PROGRESS": raise ValueError("Unrecognized executor status")
        if attempt == 0: client.stop_build(id=build_id)
        sleep(10)
    raise TimeoutError("Previous infrastructure executor did not stop")


def remove_owned_shop(client, session_id):
    records = client.records(HOST)
    if not records: return
    if len(records) != 1 or records[0].get("comment") != f"techx:{session_id}":
        raise ValueError("Storefront DNS is not owned by this cleanup session")
    record = records[0]
    client.delete(dict(id=record["id"], name=HOST, content=record["content"], sessionId=session_id))


def terminate_probes(ec2, session_id):
    filters = [dict(Name="tag:Project", Values=["techx"]), dict(Name="tag:Environment", Values=["staging"]),
               dict(Name="tag:SessionId", Values=[session_id]), dict(Name="tag:Role", Values=["private-probe"]),
               dict(Name="instance-state-name", Values=["pending", "running", "stopped", "stopping"])]
    response = ec2.describe_instances(Filters=filters)
    ids = []
    for reservation in response.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            tags = {item["Key"]: item["Value"] for item in instance.get("Tags", [])}
            if any(tags.get(key) != value for key, value in dict(Project="techx", Environment="staging", SessionId=session_id, Role="private-probe").items()):
                raise ValueError("Probe ownership mismatch")
            ids.append(instance["InstanceId"])
    if response.get("NextToken"): raise ValueError("Unexpected paginated probe inventory; no instances terminated")
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids, WaiterConfig=dict(Delay=10, MaxAttempts=60))


def delete_session_secrets(client, session_id):
    prefix = f"techx/staging/{session_id}/"
    response = client.list_secrets(Filters=[dict(Key="name", Values=[prefix])], IncludePlannedDeletion=True)
    if response.get("NextToken"): raise ValueError("Unexpected secret inventory pagination")
    for secret in response.get("SecretList", []):
        tags = {item["Key"]: item["Value"] for item in secret.get("Tags", [])}
        if (not secret.get("Name", "").startswith(prefix) or tags.get("SessionId") != session_id
                or tags.get("Project") != "techx" or tags.get("Environment") != "staging"
                or not secret.get("ARN", "").startswith(f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{prefix}")):
            raise ValueError("Refusing to delete a secret outside this session")
        client.delete_secret(SecretId=secret["ARN"], ForceDeleteWithoutRecovery=True)


def restore_cleanup_access(eks, caller_ip, sleep=time.sleep):
    address = ipaddress.ip_address(caller_ip)
    if address.version != 4 or not address.is_global: raise ValueError("Cleanup requires a validated public IPv4")
    try: cluster = eks.describe_cluster(name="techx-staging")["cluster"]
    except eks.exceptions.ResourceNotFoundException: return
    if cluster.get("arn") != f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/techx-staging": raise ValueError("Unexpected cluster ARN")
    if cluster.get("status") != "ACTIVE": return  # Partial-foundation teardown handles non-ACTIVE/no-Helm state.
    config = cluster["resourcesVpcConfig"]
    if config.get("endpointPrivateAccess") is not True: raise ValueError("Unexpected EKS private endpoint configuration")
    cidr = f"{address}/32"
    if config.get("publicAccessCidrs") != [cidr] or config.get("endpointPublicAccess") is not True:
        result = eks.update_cluster_config(name="techx-staging", resourcesVpcConfig=dict(
            endpointPrivateAccess=True, endpointPublicAccess=True, publicAccessCidrs=[cidr]))
        for _ in range(120):
            update = eks.describe_update(name="techx-staging", updateId=result["update"]["id"])["update"]
            if update.get("status") == "Successful": break
            if update.get("status") in ("Failed", "Cancelled"): raise ValueError("Cleanup endpoint recovery failed")
            sleep(5)
        else: raise TimeoutError("Cleanup endpoint recovery timed out")
    try: eks.describe_access_entry(clusterName="techx-staging", principalArn=CLEANUP_ROLE)
    except eks.exceptions.ResourceNotFoundException:
        eks.create_access_entry(clusterName="techx-staging", principalArn=CLEANUP_ROLE, type="STANDARD",
                                tags=dict(Project="techx", Environment="staging"))
    for attempt in range(20):
        try:
            eks.associate_access_policy(clusterName="techx-staging", principalArn=CLEANUP_ROLE,
                                        policyArn="arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
                                        accessScope=dict(type="cluster"))
            break
        except eks.exceptions.ResourceNotFoundException:
            if attempt == 19: raise
            sleep(3)
