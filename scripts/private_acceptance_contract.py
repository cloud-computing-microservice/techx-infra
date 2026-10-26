"""Cold private acceptance receipt and the atomic READY release payload."""
from copy import deepcopy
import ipaddress
import re

from automation_session import utc

from plan_approval import version
from plan_review_store import checksum, read_json
from public_acceptance_contract import public_context, validate_public_checkpoint

CHECKS = {"argocd-authenticated", "runtime-digests", "dynamodb-irsa", "replica-self-heal", "order-restart-persistence",
          "vpn-private-dns-https", "vpn-disconnected"}


def evidence_record(record):
    # Evidence is produced in PROVISIONING and remains readable after READY.
    # This projection is read-only; mutation entry points keep their original
    # status gates and cannot use a READY session to create another deployment.
    if record.get("status") not in ("PROVISIONING", "READY"):
        raise ValueError("Private acceptance evidence belongs to a closed session")
    return dict(record, status="PROVISIONING")


def private_context(record):
    projected = evidence_record(record)
    validate_public_checkpoint(projected)
    return dict(public_context(projected), publicAcceptance=deepcopy(record["stages"]["publicAcceptance"]))


def validate_private_start(record, attempt):
    if record.get("status") != "PROVISIONING" or attempt != 1 or record.get("stages", {}).get("private"):
        raise ValueError("Only one unconsumed cold private acceptance task is allowed")
    return checksum(private_context(record))


def validate_private_checkpoint(record, task=None):
    stage = record.get("stages", {}).get("private", {})
    ref = stage.get("receiptReference", {})
    if (stage.get("state") != "PASSED" or stage.get("attempt") != 1
            or stage.get("contextSha256") != checksum(private_context(record))
            or not re.fullmatch(r"[a-f0-9]{32}", stage.get("taskId", ""))
            or not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != f"private/staging/{record['sessionId']}/acceptance/{stage['taskId']}.json"
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))
            or task is not None and (task["taskId"] != stage["taskId"] or task.get("expectedAcceptanceSha256") != stage["contextSha256"])):
        raise ValueError("Private acceptance has no exact terminal receipt checkpoint")
    version(ref.get("versionId"))
    return stage


def read_private_receipt(client, bucket, record, execution_version):
    stage = validate_private_checkpoint(record)
    ref = stage["receiptReference"]
    value, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    expected = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=execution_version,
        taskId=stage["taskId"], contextSha256=stage["contextSha256"], checks=sorted(CHECKS),
        publicReference=record["stages"]["publicAcceptance"]["receiptReference"])
    if (not isinstance(value, dict) or checksum(value) != ref["sha256"]
            or any(value.get(k) != v for k, v in expected.items()) or value.get("release") != stage.get("release")):
        raise ValueError("Private acceptance receipt differs from its exact task/public predecessor")
    vpn, release = value.get("vpn", {}), value.get("release", {})
    if (vpn.get("disconnected") is not True or vpn.get("commonName") != "techx-demo-operator"
            or not re.fullmatch(r"cvpn-connection-[a-f0-9]{8,17}", vpn.get("connectionId", ""))
            or ipaddress.ip_address(vpn.get("clientIp", "")) not in ipaddress.ip_network("172.24.0.0/22")
            or not isinstance(release, dict) or set(release) != {"platformSha", "chartSha", "images"}
            or not re.fullmatch(r"[a-f0-9]{40}", release.get("platformSha", ""))
            or release.get("platformSha") != record["sourceRevisions"]["techx-platform"]
            or release.get("chartSha") != record["stages"]["gitops"]["chartSha"]
            or set(release.get("images", {})) != {"frontend", "catalog-api", "order-api"}):
        raise ValueError("Private VPN/release acceptance is incomplete")
    if not utc(record["createdAt"]) <= utc(value.get("completedAt", "")) < utc(record["cleanupStartAt"]):
        raise ValueError("Private acceptance was not completed in the admitted window")
    for name, repository in {"frontend": "frontend", "catalog-api": "catalog", "order-api": "order"}.items():
        if not re.fullmatch(r"058114477594\.dkr\.ecr\.us-east-1\.amazonaws\.com/techx/" + repository + r"@sha256:[a-f0-9]{64}", release["images"][name]):
            raise ValueError("Accepted runtime artifact is outside the exact repository/digest contract")
    return value


def ready_release(client, bucket, record, execution_version):
    from public_acceptance_contract import read_public_receipt
    read_public_receipt(client, bucket, evidence_record(record), execution_version)
    receipt = read_private_receipt(client, bucket, record, execution_version)
    from edge_activation import read_activation_receipt
    activation = read_activation_receipt(client, bucket, evidence_record(record), execution_version)
    if receipt.get("stateSha256") != activation["stateSha256"] or receipt["vpn"].get("endpointId") != activation["resources"]["endpointId"]:
        raise ValueError("Private acceptance used a different committed edge/VPN endpoint")
    return dict(receipt["release"], acceptanceReference=deepcopy(record["stages"]["private"]["receiptReference"]))
