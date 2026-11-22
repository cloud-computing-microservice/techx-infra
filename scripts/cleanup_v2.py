"""Cleanup evidence contracts, used by the future trusted AWS collector/finalizer.

No JSON payload authenticates a service observation. Registry is fetched from
protected CAS storage; observations are collected by AWS clients in the fixed
account/region with complete session discovery. These validators neither query
AWS nor write completion. The finalizer must independently collect fresh facts,
fence all writers, and conditionally seal the result under its exclusive role.
"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json

from execution_v2 import (ACCOUNT, PREFIX, REGION, decode_object, execution_reference, fields, integer,
                          reference, text, timestamp, validate_execution, validate_session)
from recovery_v2 import checksum

INVENTORY_FIELDS = set(json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()))
MAX_AGE = timedelta(minutes=5)
_FACT_SEAL = object()


class ValidatedFacts:
    """In-process guard: decoded JSON cannot pretend a collector already ran.

    This is not cryptographic authentication or a sandbox against arbitrary
    Python code. It prevents accidentally feeding worker/job JSON directly into
    finalization; the trusted runtime must call the collectors/validators first.
    """
    __slots__ = ("__kind", "__payload")

    def __init__(self, kind, payload, *, _seal=None):
        if _seal is not _FACT_SEAL: raise ValueError("Facts must be produced by the evidence validators")
        self.__kind, self.__payload = kind, deepcopy(payload)

    @property
    def kind(self):
        return self.__kind

    def as_dict(self):
        return deepcopy(self.__payload)


def fresh(value, *, now, after):
    observed = timestamp(value)
    if not max(after, now - MAX_AGE) <= observed <= now:
        raise ValueError("Cleanup evidence is stale, predates the fence/provisional receipt, or is in the future")
    return observed


def workload_zero(inventory_body, inventory_ref, state_body, state_ref, execution, execution_ref, epoch, *, now, after):
    validate_execution(execution)
    execution_reference(execution_ref, execution["sessionId"])
    integer(epoch)
    key = inventory_ref.get("key") if isinstance(inventory_ref, dict) else None
    prefix = f"{PREFIX}cleanup/{execution['sessionId']}/inventories/"
    text(key, prefix + r"[a-f0-9]{32}\.json")
    snapshot = decode_object(inventory_body, inventory_ref, key=key, max_bytes=1024 * 1024)
    fields(snapshot, {"schemaVersion", "executionReference", "leaseEpoch", "snapshotId", "observedAt", "stateReference", "inventory"})
    integer(snapshot["leaseEpoch"])
    if (type(snapshot["schemaVersion"]) is not int or snapshot["schemaVersion"] != 2
            or snapshot["executionReference"] != execution_ref or snapshot["leaseEpoch"] != epoch
            or key != f"{prefix}{text(snapshot['snapshotId'], r'[a-f0-9]{32}')}.json"
            or checksum(snapshot["stateReference"]) != checksum(state_ref)):
        raise ValueError("Inventory snapshot differs from execution, epoch or state version")
    fresh(snapshot["observedAt"], now=now, after=after)
    inventory = snapshot["inventory"]
    fields(inventory, INVENTORY_FIELDS | {"accountId", "region", "environment"})
    if (inventory["accountId"], inventory["region"], inventory["environment"]) != (ACCOUNT, REGION, "staging"):
        raise ValueError("Workload inventory scope mismatch")
    if any(not isinstance(inventory[name], list) or inventory[name] for name in INVENTORY_FIELDS):
        raise ValueError("Every workload inventory group must be positively present and empty")
    state = decode_object(state_body, state_ref, key="staging/terraform.tfstate", max_bytes=8 * 1024 * 1024)
    if type(state.get("version")) is not int or state["version"] != 4 or not isinstance(state.get("resources"), list):
        raise ValueError("Initialized Terraform state format required")
    integer(state.get("serial"), minimum=0)
    text(state.get("lineage"), r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
    for resource in state["resources"]:
        if (not isinstance(resource, dict) or resource.get("mode") not in ("managed", "data")
                or not isinstance(resource.get("instances"), list)
                or resource["mode"] == "managed" and resource["instances"]):
            raise ValueError("State is malformed or still contains managed instances")
    return ValidatedFacts("workload", dict(executionReference=deepcopy(execution_ref), leaseEpoch=epoch, observedAt=snapshot["observedAt"],
                inventoryReference=deepcopy(inventory_ref), stateReference=deepcopy(state_ref),
                stateLineage=state["lineage"], stateSerial=state["serial"]), _seal=_FACT_SEAL)


def api_fact(value, resource_id, operation, *, now, after, end, seen_requests):
    fields(value, {"observedAt", "requestedId", "operation", "response"})
    if value["requestedId"] != resource_id or value["operation"] != operation:
        raise ValueError("API result is not bound to the singleton resource request")
    observed = fresh(value["observedAt"], now=now, after=after)
    if observed > end or not isinstance(value["response"], dict):
        raise ValueError("API observation differs from its collection interval")
    response = value["response"]
    metadata = response.get("ResponseMetadata", {})
    text(metadata.get("RequestId"), r"[A-Za-z0-9-]{8,128}")
    if metadata["RequestId"] in seen_requests:
        raise ValueError("One API response cannot stand in for multiple resource lookups")
    seen_requests.add(metadata["RequestId"])
    if type(metadata.get("HTTPStatusCode")) is not int:
        raise ValueError("Positive AWS response metadata required")
    return response


def absent(response, code):
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 400 and response.get("Error", {}).get("Code") == code


def instance_result(response, instance_id, launch, execution, execution_ref, *, active):
    if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200 or response.get("Error") or response.get("NextToken"):
        raise ValueError("Unknown EC2 state cannot prove termination")
    reservations = response.get("Reservations")
    if not isinstance(reservations, list) or any(not isinstance(r, dict) or not isinstance(r.get("Instances"), list) for r in reservations):
        raise ValueError("Incomplete DescribeInstances response")
    instances = [i for r in reservations for i in r["Instances"]]
    if len(instances) != 1 or not isinstance(instances[0], dict): raise ValueError("Exact instance service result required")
    instance = instances[0]
    tags = instance.get("Tags")
    if (not isinstance(tags, list) or any(not isinstance(t, dict) or set(t) != {"Key", "Value"} for t in tags)
            or len({t["Key"] for t in tags}) != len(tags)):
        raise ValueError("Positive worker ownership tags required")
    tag_map = {t["Key"]: t["Value"] for t in tags}
    expected_tags = dict(Project="TechX", Environment="automation", SessionId=execution["sessionId"],
                         ExecutionSha256=execution_ref["sha256"], Role=f"{launch['purpose']}-executor")
    if (instance.get("InstanceId") != instance_id or instance.get("ClientToken") != launch["launchToken"]
            or any(tag_map.get(k) != v for k, v in expected_tags.items())
            or instance.get("State", {}).get("Name") != ("running" if active else "terminated")):
        raise ValueError("Worker identity or positive terminal state mismatch")
    if not active and (instance.get("PublicIpAddress") or instance.get("NetworkInterfaces") != [] or instance.get("BlockDeviceMappings") != []):
        raise ValueError("Terminated worker still exposes attached network, volume or public address")


def compute_gone(registry, registry_ref, observations, observations_ref, execution, execution_ref, epoch, *, active_cleanup_id, final, now, after):
    """Verify a complete protected roster, positive terminal/absence APIs and assets.

    The trusted collector must obtain discoveredInstanceIds from complete fresh
    session-tagged EC2 pagination without state filters. This function cannot
    establish that provenance from arbitrary JSON or accept a boolean 'empty'.
    """
    validate_execution(execution)
    execution_reference(execution_ref, execution["sessionId"])
    integer(epoch)
    if type(final) is not bool:
        raise ValueError("Collector mode must be a fixed internal boolean")
    fields(registry, {"schemaVersion", "executionReference", "leaseEpoch", "launches"})
    reference(registry_ref, key=f"{PREFIX}workers/{execution['sessionId']}/registry.json")
    if registry_ref["sha256"] != checksum(registry):
        raise ValueError("Registry differs from its exact canonical private version")
    fields(observations, {"schemaVersion", "executionReference", "leaseEpoch", "observedAt", "discoveredInstanceIds",
                          "instances", "volumes", "networkInterfaces", "terminalProofs"})
    for value in (registry, observations):
        if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2
                or value["executionReference"] != execution_ref or type(value["leaseEpoch"]) is not int or value["leaseEpoch"] != epoch):
            raise ValueError("Compute evidence differs from execution or fence")
    kind = "compute-observations" if final else "provisional-compute-observations"
    reference(observations_ref, key=f"{PREFIX}cleanup/{execution['sessionId']}/{kind}/{epoch}.json")
    if observations_ref["sha256"] != checksum(observations):
        raise ValueError("Compute observations differ from their exact canonical private version")
    end = fresh(observations["observedAt"], now=now, after=after)
    launches = registry["launches"]
    if not isinstance(launches, list) or not 1 <= len(launches) <= 5:
        raise ValueError("Bounded complete worker roster required")
    known, slots, tokens, volumes, interfaces = {}, set(), set(), set(), set()
    for launch in launches:
        fields(launch, {"purpose", "attempt", "phase", "launchToken", "instanceId", "volumeIds", "networkInterfaceIds", "terminalReference"})
        if launch["purpose"] not in ("deployment", "cleanup", "probe"):
            raise ValueError("Unknown worker purpose")
        integer(launch["attempt"], maximum=3 if launch["purpose"] == "cleanup" else 1)
        text(launch["launchToken"], r"[a-f0-9]{64}")
        slot = (launch["purpose"], launch["attempt"])
        if slot in slots or launch["launchToken"] in tokens:
            raise ValueError("Duplicate worker generation or launch token")
        slots.add(slot); tokens.add(launch["launchToken"])
        if launch["phase"] == "PREPARED":
            if launch["instanceId"] is not None or launch["volumeIds"] != [] or launch["networkInterfaceIds"] != [] or launch["terminalReference"] is not None:
                raise ValueError("Uncalled launch cannot invent compute")
            continue
        if launch["phase"] != "OBSERVED":
            raise ValueError("Uncertain CALLING launch prevents cleanup completion")
        instance_id = text(launch["instanceId"], r"i-[a-f0-9]{17}")
        if instance_id in known:
            raise ValueError("Duplicate instance in worker roster")
        known[instance_id] = launch
        if launch["terminalReference"] is not None:
            reference(launch["terminalReference"], key=f"{PREFIX}cleanup/{execution['sessionId']}/terminals/{instance_id}.json")
        for name, pattern, seen in (("volumeIds", r"vol-[a-f0-9]{17}", volumes),
                                    ("networkInterfaceIds", r"eni-[a-f0-9]{17}", interfaces)):
            values = launch[name]
            if not isinstance(values, list) or not 1 <= len(values) <= 8:
                raise ValueError("Observed worker requires its complete original asset registration")
            for value in values:
                text(value, pattern)
                if value in seen: raise ValueError("Duplicate registered worker asset")
                seen.add(value)
    if active_cleanup_id not in known or known[active_cleanup_id]["purpose"] != "cleanup":
        raise ValueError("The cleanup owner must be a positively registered cleanup worker")
    discovered = observations["discoveredInstanceIds"]
    if (not isinstance(discovered, list) or any(not isinstance(i, str) for i in discovered)
            or len(set(discovered)) != len(discovered) or set(discovered) - set(known)):
        raise ValueError("Session discovery contains unknown or duplicate compute")
    for name in ("instances", "volumes", "networkInterfaces", "terminalProofs"):
        if not isinstance(observations[name], dict): raise ValueError("Explicit per-resource API observations required")
    if set(observations["instances"]) != set(known):
        raise ValueError("Missing or unexpected instance observation")
    seen_requests, historical = set(), set()
    for instance_id, launch in known.items():
        response = api_fact(observations["instances"][instance_id], instance_id, "DescribeInstances",
                            now=now, after=after, end=end, seen_requests=seen_requests)
        active = not final and instance_id == active_cleanup_id
        if absent(response, "InvalidInstanceID.NotFound"):
            if active: raise ValueError("Provisional cleanup worker is absent")
            # NotFound alone is unsafe during EC2 eventual consistency. Require
            # a protected exact-version positive terminated observation first.
            proof = observations["terminalProofs"].get(instance_id)
            fields(proof, {"reference", "body"})
            if launch["terminalReference"] is None or checksum(proof["reference"]) != checksum(launch["terminalReference"]):
                raise ValueError("Missing registered positive terminal proof for the absent instance")
            if not isinstance(proof["body"], str): raise ValueError("Exact private terminal proof bytes required")
            recorded = decode_object(proof["body"].encode(), proof["reference"],
                                     key=f"{PREFIX}cleanup/{execution['sessionId']}/terminals/{instance_id}.json")
            fields(recorded, {"schemaVersion", "executionReference", "instanceId", "observation"})
            if (type(recorded["schemaVersion"]) is not int or recorded["schemaVersion"] != 2
                    or recorded["executionReference"] != execution_ref or recorded["instanceId"] != instance_id):
                raise ValueError("Terminal proof belongs to another worker/execution")
            prior_at = timestamp(recorded["observation"]["observedAt"])
            if not timestamp(execution["startedAt"]) <= prior_at <= timestamp(observations["instances"][instance_id]["observedAt"]):
                raise ValueError("Terminal proof timestamp is not historical to the fresh absence")
            prior = api_fact(recorded["observation"], instance_id, "DescribeInstances", now=prior_at,
                             after=timestamp(execution["startedAt"]), end=prior_at, seen_requests=seen_requests)
            instance_result(prior, instance_id, launch, execution, execution_ref, active=False)
            historical.add(instance_id)
            continue
        instance_result(response, instance_id, launch, execution, execution_ref, active=active)
    if set(observations["terminalProofs"]) != historical:
        raise ValueError("Unbound or unnecessary historical terminal proof")
    required_volumes = {v for i, row in known.items() if final or i != active_cleanup_id for v in row["volumeIds"]}
    required_enis = {v for i, row in known.items() if final or i != active_cleanup_id for v in row["networkInterfaceIds"]}
    for name, expected, code, operation in (("volumes", required_volumes, "InvalidVolume.NotFound", "DescribeVolumes"),
                                           ("networkInterfaces", required_enis, "InvalidNetworkInterfaceID.NotFound", "DescribeNetworkInterfaces")):
        if set(observations[name]) != expected: raise ValueError("Missing or unexpected registered asset observation")
        for resource_id, fact in observations[name].items():
            if not absent(api_fact(fact, resource_id, operation, now=now, after=after, end=end, seen_requests=seen_requests), code):
                raise ValueError("Worker asset still exists or its absence is unproven")
    return ValidatedFacts("compute", dict(executionReference=deepcopy(execution_ref), leaseEpoch=epoch, observedAt=observations["observedAt"],
                phase="ALL_SESSION_COMPUTE_GONE" if final else "INFRA_STOPPED_CLEANUP_OWNER_LIVE",
                cleanupInstanceId=active_cleanup_id, instanceIds=sorted(known),
                registryReference=deepcopy(registry_ref), observationsReference=deepcopy(observations_ref)), _seal=_FACT_SEAL)


def read_provisional(body, ref, execution, execution_ref, epoch):
    validate_execution(execution)
    execution_reference(execution_ref, execution["sessionId"])
    integer(epoch)
    value = decode_object(body, ref, key=f"{PREFIX}cleanup/{execution['sessionId']}/provisional/{epoch}.json")
    fields(value, {"schemaVersion", "executionReference", "leaseEpoch", "cleanupInstanceId", "snapshotId",
                   "inventoryReference", "stateReference", "computeReference", "createdAt"})
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["executionReference"] != execution_ref
            or type(value["leaseEpoch"]) is not int or value["leaseEpoch"] != epoch):
        raise ValueError("Provisional receipt belongs to another execution/fence")
    text(value["cleanupInstanceId"], r"i-[a-f0-9]{17}")
    text(value["snapshotId"], r"[a-f0-9]{32}")
    reference(value["inventoryReference"], key=f"{PREFIX}cleanup/{execution['sessionId']}/inventories/{value['snapshotId']}.json")
    reference(value["stateReference"], key="staging/terraform.tfstate")
    reference(value["computeReference"], key=f"{PREFIX}cleanup/{execution['sessionId']}/provisional-compute/{epoch}.json")
    if timestamp(value["createdAt"]) < timestamp(execution["startedAt"]): raise ValueError("Receipt predates execution")
    return value


def validate_final(body, ref, provisional_body, provisional_ref, execution, execution_ref, session,
                   workload_facts, compute_facts, workload_ref, compute_ref, *, caller, now):
    """Seal only independently validated fresh facts; never worker self-proof.

    Facts must come from workload_zero/compute_gone in this trusted invocation,
    not user/job inputs. Runtime fencing, immutable evidence publication and CAS
    completion writes remain finalizer duties, enforced by separate IAM policy.
    """
    validate_session(session, execution, execution_ref)
    if session["status"] != "DESTROYING": raise ValueError("Finalization requires the live destroying session")
    epoch = session["leaseEpoch"]
    provisional = read_provisional(provisional_body, provisional_ref, execution, execution_ref, epoch)
    after = timestamp(provisional["createdAt"])
    principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-finalizer/techx-automation-v2-finalizer"
    if not isinstance(caller, dict) or caller.get("Account") != ACCOUNT or caller.get("Arn") != principal:
        raise ValueError("Only the actual dedicated finalizer principal may seal completion")
    if (type(workload_facts) is not ValidatedFacts or workload_facts.kind != "workload"
            or type(compute_facts) is not ValidatedFacts or compute_facts.kind != "compute"):
        raise ValueError("Untrusted JSON cannot substitute for freshly validated collector facts")
    workload_facts, compute_facts = workload_facts.as_dict(), compute_facts.as_dict()
    for facts in (workload_facts, compute_facts):
        if (not isinstance(facts, dict) or facts.get("executionReference") != execution_ref
                or type(facts.get("leaseEpoch")) is not int or facts["leaseEpoch"] != epoch):
            raise ValueError("Validated facts differ from the live execution/fence")
        fresh(facts.get("observedAt"), now=now, after=after)
    if (compute_facts.get("phase") != "ALL_SESSION_COMPUTE_GONE"
            or compute_facts.get("cleanupInstanceId") != provisional["cleanupInstanceId"]):
        raise ValueError("Cleanup owner is not independently proven gone")
    value = decode_object(body, ref, key=f"{PREFIX}cleanup/{execution['sessionId']}/final/{epoch}.json")
    fields(value, {"schemaVersion", "executionReference", "leaseEpoch", "provisionalReference", "inventoryReference", "stateReference",
                   "workloadReference", "computeReference", "completedAt", "finalizerPrincipalArn"})
    reference(value["workloadReference"], key=f"{PREFIX}cleanup/{execution['sessionId']}/workload/{epoch}.json")
    reference(value["computeReference"], key=f"{PREFIX}cleanup/{execution['sessionId']}/compute/{epoch}.json")
    reference(workload_ref, key=value["workloadReference"]["key"])
    reference(compute_ref, key=value["computeReference"]["key"])
    if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["executionReference"] != execution_ref
            or type(value["leaseEpoch"]) is not int or value["leaseEpoch"] != epoch
            or checksum(value["provisionalReference"]) != checksum(provisional_ref)
            or checksum(value["inventoryReference"]) != checksum(workload_facts["inventoryReference"])
            or checksum(value["stateReference"]) != checksum(workload_facts["stateReference"])
            or checksum(value["workloadReference"]) != checksum(workload_ref)
            or checksum(value["computeReference"]) != checksum(compute_ref)
            or value["workloadReference"]["sha256"] != checksum(workload_facts)
            or value["computeReference"]["sha256"] != checksum(compute_facts)
            or value["finalizerPrincipalArn"] != principal):
        raise ValueError("Final receipt does not bind the actual validated evidence and authority")
    completed = timestamp(value["completedAt"])
    if not max(after, timestamp(workload_facts["observedAt"]), timestamp(compute_facts["observedAt"])) <= completed <= now:
        raise ValueError("Completion predates final evidence or is in the future")
    return value
