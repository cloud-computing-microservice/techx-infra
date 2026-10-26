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
    if lease.get("owner") and lease["owner"] != owner and lease.get("operation") not in ("release", "bootstrap-publish") and not executor_stopped:
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


def stop_dispatched_manager(clients, bucket, record, owner):
    """Recover a lost StartBuild response by positive service evidence only."""
    from lifecycle_dispatch import BUILD, PROJECT, environment, observed_build
    ref = record.get("lifecycleDispatch")
    if ref is None: return None  # Historical sessions use their launch/lease proof.
    if (not isinstance(ref, dict) or not re.fullmatch(r"[a-f0-9]{64}", ref.get("id", ""))
            or ref.get("phase") not in ("PREPARED", "CALLING", "OBSERVED")):
        raise ValueError("Invalid lifecycle dispatch recovery intent")
    expected = environment(record, ref["operation"])
    if ref["phase"] == "PREPARED": return None  # API call requires a later CAS; parent fencing uses this record's ETag.
    recovered = dict(ref)
    if ref["phase"] == "CALLING":
        proof_key = f"cleanup/{record['sessionId']}/manager-{ref['id']}.json"
        try:
            result = clients["s3"].get_object(Bucket=bucket, Key=proof_key, ExpectedBucketOwner=ACCOUNT)
            body = result["Body"].read(16385)
            if len(body) > 16384: raise ValueError("Oversized manager recovery receipt")
            proof = json.loads(body)
        except clients["s3"].exceptions.NoSuchKey:
            proof = None
        if proof is not None:
            if (proof.get("dispatchId") != ref["id"] or proof.get("executionVersion") != record.get("executionVersion")
                    or proof.get("terminated") is not True or not re.fullmatch(BUILD, proof.get("buildId", ""))):
                raise ValueError("Manager recovery receipt differs from the current dispatch")
            recovered.update(phase="OBSERVED", buildId=proof["buildId"])
        else:
            token, seen, matches = None, set(), []
            for _ in range(5):
                # AWS rejects sortOrder once a project has over 100 builds;
                # its default is already descending and supports pagination.
                args = dict(projectName=PROJECT)
                if token: args["nextToken"] = token
                listing = clients["codebuild"].list_builds_for_project(**args)
                ids = listing.get("ids")
                if (not isinstance(ids, list) or len(ids) > 100 or any(not re.fullmatch(BUILD, item) for item in ids)
                        or len(set(ids)) != len(ids) or seen.intersection(ids)):
                    raise ValueError("Ambiguous lifecycle manager recovery inventory")
                seen.update(ids)
                if ids:
                    response = clients["codebuild"].batch_get_builds(ids=ids)
                    builds = response.get("builds", [])
                    if (response.get("buildsNotFound") or len(builds) != len(ids)
                            or {item.get("id") for item in builds} != set(ids)):
                        raise ValueError("Manager recovery observations are incomplete")
                    for build in builds:
                        rows = [row for row in build.get("environment", {}).get("environmentVariables", []) if row.get("name", "").startswith("TECHX_")]
                        if len(rows) == len(expected) and {row["name"]: row.get("value") for row in rows} == expected:
                            matches.append(build["id"])
                token = listing.get("nextToken")
                if not token: break
            if token or len(matches) != 1:
                raise ValueError("StartBuild outcome remains unproven; empty/ambiguous inventory cannot complete cleanup")
            recovered.update(phase="OBSERVED", buildId=matches[0])
    build = observed_build(clients["codebuild"], dict(record, lifecycleDispatch=recovered))
    stop_executor(clients["codebuild"], build["arn"], owner)
    if ref["phase"] == "CALLING":
        clients["s3"].put_object(Bucket=bucket, Key=proof_key, ExpectedBucketOwner=ACCOUNT,
            Body=json.dumps(dict(dispatchId=ref["id"], executionVersion=record["executionVersion"], buildId=build["id"], terminated=True)).encode())
    return build["arn"]


def remove_owned_shop(client, session_id, *, intent=None, observed=None, persist_observation=None):
    from cloudflare_dns import owned_shop_record, validate_shop_intent
    if intent is not None: validate_shop_intent(intent)
    records = client.records(HOST)
    if not records:
        if intent is not None and intent["phase"] == "CALLING" and observed is None:
            raise ValueError("Storefront DNS creation outcome remains unproven; absence cannot complete cleanup")
        return
    if len(records) != 1 or records[0].get("comment") != f"techx:{session_id}":
        raise ValueError("Storefront DNS is not owned by this cleanup session")
    record = records[0]
    ownership = dict(id=record["id"], name=HOST, content=record["content"], sessionId=session_id)
    if intent is not None:
        ownership = owned_shop_record(records, intent["target"], session_id)
        if (intent["phase"] == "OBSERVED" and ownership["id"] != intent["id"]
                or observed is not None and ownership != observed):
            raise ValueError("Storefront record was replaced after its creation proof")
        if persist_observation is None: raise ValueError("Durable DNS observation is required before deletion")
        persist_observation(ownership)
    client.delete(ownership)


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


def terminate_deployment_executors(ec2, session_id, expected_instance_id=None, *, launch_token=None):
    """Stop remote mutation even when the review-waiting VM has no active lease.

    Stopping its CodeBuild manager does not stop SSM/EC2 child processes. Every
    owned executor must be observed terminated before cleanup can fence deploy.
    """
    if not re.fullmatch(r"staging-[a-zA-Z0-9-]{1,64}", session_id):
        raise ValueError("Invalid deployment cleanup session")
    if expected_instance_id is not None and not re.fullmatch(r"i-[a-f0-9]{8,17}", expected_instance_id):
        raise ValueError("Invalid deployment executor identity")
    if launch_token is not None and not re.fullmatch(r"[a-f0-9]{64}", launch_token):
        raise ValueError("Invalid deployment launch intent token")
    filters = [dict(Name="tag:Project", Values=["techx"]), dict(Name="tag:Environment", Values=["staging"]),
               dict(Name="tag:SessionId", Values=[session_id]), dict(Name="tag:Role", Values=["deployment-executor"])]
    response = ec2.describe_instances(Filters=filters)
    if not isinstance(response.get("Reservations"), list) or response.get("NextToken"):
        raise ValueError("Paginated deployment executor inventory; cleanup takeover refused")
    ids, found, launch_observed = [], set(), False
    wanted = dict(Project="techx", Environment="staging", SessionId=session_id, Role="deployment-executor")
    for reservation in response.get("Reservations", []):
        if reservation.get("OwnerId") != ACCOUNT or not isinstance(reservation.get("Instances"), list):
            raise ValueError("Deployment executor account mismatch")
        for instance in reservation.get("Instances", []):
            tags = {item["Key"]: item["Value"] for item in instance.get("Tags", [])}
            instance_id = instance.get("InstanceId", "")
            state = instance.get("State", {}).get("Name")
            if (any(tags.get(key) != value for key, value in wanted.items())
                    or not re.fullmatch(r"i-[a-f0-9]{8,17}", instance_id) or instance_id in found
                    or state not in ("pending", "running", "stopping", "stopped", "shutting-down", "terminated")):
                raise ValueError("Deployment executor ownership/state is ambiguous")
            found.add(instance_id)
            if instance.get("ClientToken") == launch_token and launch_token is not None:
                launch_observed = True
            if state != "terminated": ids.append(instance_id)
    if expected_instance_id is not None and expected_instance_id not in found:
        raise ValueError("Lease executor is missing from scoped inventory; termination is unproven")
    if launch_token is not None and not launch_observed:
        raise ValueError("Executor launch outcome is unproven; an empty eventual-consistency read cannot complete teardown")
    if ids:
        ec2.terminate_instances(InstanceIds=ids)
        ec2.get_waiter("instance_terminated").wait(InstanceIds=ids, WaiterConfig=dict(Delay=10, MaxAttempts=60))
    return dict(instanceIds=sorted(found), launchObserved=launch_observed)


def delete_session_secrets(client, session_id, known_secrets=()):
    prefix = f"techx/staging/{session_id}/"
    response = client.list_secrets(Filters=[dict(Key="name", Values=[prefix])], IncludePlannedDeletion=True)
    if response.get("NextToken"): raise ValueError("Unexpected secret inventory pagination")
    # Exact recovered receipts bypass ListSecrets propagation delay, while the
    # list still catches a creation whose response was lost before persistence.
    combined = {secret["ARN"]: secret for secret in [*response.get("SecretList", []), *known_secrets]}
    for secret in combined.values():
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
