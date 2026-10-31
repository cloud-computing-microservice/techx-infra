"""Typed private rollout context; an accepted baseline is never a candidate."""
from copy import deepcopy
import re

from automation_session import utc
from plan_approval import version
from plan_review_store import checksum, read_json
from ready_release_contract import candidate_key, read_manifest, read_proposal, read_reference


def reference(ref, key):
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"} or ref.get("key") != key
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Exact versioned active release reference required")
    version(ref.get("versionId"))
    return ref


def release_context(record):
    pointer = record.get("releaseCandidate", {})
    if (record.get("status") not in ("READY", "TESTING") or record.get("pendingPlan") is not None
            or pointer.get("state") not in ("PROPOSED", "ROLLED_OUT")
            or not re.fullmatch(r"[a-f0-9]{40}", pointer.get("sourceSha", "")) or not record.get("acceptedRelease")):
        raise ValueError("Private rollout requires the accepted session and reviewed active candidate")
    context = dict(releaseId=pointer.get("id"), sourceSha=pointer["sourceSha"], acceptedBaseline=deepcopy(record["acceptedRelease"]))
    for name, filename in (("intentReference", "intent"), ("manifestReference", "manifest"), ("proposalReference", "proposal")):
        context[name] = deepcopy(reference(pointer.get(name), candidate_key(record["sessionId"], pointer.get("id", ""), filename)))
    approval = pointer.get("promotionApprovalReference", {})
    prefix = f"promotion-approvals/staging/{record['sessionId']}/"
    if (not isinstance(approval.get("key"), str) or not approval["key"].startswith(prefix)
            or not re.fullmatch(r"[a-f0-9]{64}\.json", approval["key"][len(prefix):])):
        raise ValueError("Private rollout lacks its own reviewed promotion grant")
    context["approvalReference"] = deepcopy(reference(approval, approval["key"]))
    return context


def validate_release_start(record, attempt):
    context = release_context(record)
    if (record["status"] != "TESTING" or attempt != 1 or record["releaseCandidate"]["state"] != "PROPOSED"
            or record["releaseCandidate"].get("rollout") or record["releaseCandidate"].get("partialRollout")):
        raise ValueError("Only one unconsumed private rollout may run for this candidate")
    return context


def admitted_release(client, bucket, record, execution, execution_version, now):
    from review_chart_promotion import read_promotion_approval
    context = release_context(record)
    grant = read_promotion_approval(client, bucket, execution, execution_version, context["approvalReference"], now, kind="ready-promotion")
    expected = dict(releaseId=context["releaseId"], intentReference=context["intentReference"], releaseReference=context["manifestReference"],
        proposalReference=context["proposalReference"], acceptedBaseline=context["acceptedBaseline"])
    if any(grant["request"].get(key) != item for key, item in expected.items()):
        raise ValueError("Private task admission grant belongs to another candidate/baseline")
    return context


def validate_rollout_checkpoint(record, task=None):
    context = release_context(record)
    checkpoint = record["releaseCandidate"].get("rollout", {})
    if (record["status"] != "TESTING" or record["releaseCandidate"]["state"] != "ROLLED_OUT"
            or checkpoint.get("state") != "ROLLED_OUT" or checkpoint.get("contextSha256") != checksum(context)
            or not re.fullmatch(r"[a-f0-9]{32}", checkpoint.get("taskId", ""))
            or not re.fullmatch(r"[a-f0-9]{40}", checkpoint.get("chartSha", ""))
            or task is not None and (task.get("taskId") != checkpoint["taskId"] or task.get("expectedReleaseContext") != context)):
        raise ValueError("Active rollout lacks its exact task/context/selected revision checkpoint")
    reference(checkpoint.get("receiptReference"), f"private/staging/{record['sessionId']}/active/{context['releaseId']}/rollout.json")
    return checkpoint


def read_rollout_receipt(client, bucket, record, execution_version):
    checkpoint = validate_rollout_checkpoint(record)
    ref = checkpoint["receiptReference"]
    value, _ = read_json(client, bucket, ref["key"], ref["versionId"])
    expected = dict(schemaVersion=1, kind="active-rollout", sessionId=record["sessionId"], executionVersion=execution_version,
        releaseContext=release_context(record), taskId=checkpoint["taskId"], chartSha=checkpoint["chartSha"])
    if (not isinstance(value, dict) or checksum(value) != ref["sha256"] or any(value.get(key) != item for key, item in expected.items())
            or not utc(record["createdAt"]) <= utc(value.get("completedAt", "")) < utc(record["cleanupStartAt"])):
        raise ValueError("Active rollout receipt differs from its exact admitted task/context")
    if set(value) != set(expected) | {"mergeReceiptReference", "chartCi", "application", "images", "completedAt"}:
        raise ValueError("Active rollout receipt schema changed")
    manifest = read_manifest(client, bucket, record, execution_version)["manifest"]
    names = dict(frontend="frontend", catalog="catalog-api", order="order-api")
    images = {names[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in manifest["images"]}
    if value.get("images") != images or not re.fullmatch(r"https://github.com/cloud-computing-microservice/techx-chart/actions/runs/[1-9][0-9]*", value.get("chartCi", "")):
        raise ValueError("Rollout runtime images/CI differ from the exact published artifacts")
    context = value["releaseContext"]
    merge = read_reference(client, bucket, value["mergeReceiptReference"], f"promotions/staging/{record['sessionId']}/{context['approvalReference']['sha256']}/receipt.json")
    proposal = read_proposal(client, bucket, record, execution_version)
    if (merge.get("approvalReference") != context["approvalReference"] or merge.get("taskId") != checkpoint["taskId"]
            or merge.get("chartSha") != checkpoint["chartSha"] or merge.get("headSha") != proposal["headSha"] or merge.get("treeSha") != proposal["treeSha"]):
        raise ValueError("Rollout has a different private merge receipt/task/head/tree")
    application = value.get("application", {})
    if not isinstance(application, dict) or set(application) != {"uid", "intentReference", "specSha256", "stamp"}:
        raise ValueError("Rollout lacks its exact Application observation")
    app_intent = read_reference(client, bucket, application["intentReference"], f"private/staging/{record['sessionId']}/active/{context['releaseId']}/application/intent.json")
    app_expected = dict(schemaVersion=1, kind="active-application-intent", sessionId=record["sessionId"], executionVersion=execution_version,
        taskId=checkpoint["taskId"], releaseContext=context, mergeReceiptReference=value["mergeReceiptReference"], chartSha=checkpoint["chartSha"], uid=application["uid"])
    if (any(app_intent.get(key) != item for key, item in app_expected.items()) or application["stamp"] != checksum(app_intent)
            or not isinstance(application["uid"], str) or not 1 <= len(application["uid"]) <= 128
            or app_intent.get("previousSpec", {}).get("source", {}).get("targetRevision") != context["acceptedBaseline"]["chartSha"]):
        raise ValueError("Application observation belongs to another intent or accepted baseline")
    selected_spec = deepcopy(app_intent["previousSpec"])
    selected_spec["source"]["targetRevision"] = checkpoint["chartSha"]
    if checksum(selected_spec) != application["specSha256"]:
        raise ValueError("Application observed spec differs from the pinned selected revision")
    return value


class ActiveReleaseController:
    def __init__(self, store, clock): self.store, self.clock = store, clock

    def owned(self, session_id, owner, epoch):
        from automation_session import claim
        record, etag = self.store.read()
        lease = record.get("lease", {})
        if (record.get("sessionId") != session_id or record.get("status") != "TESTING"
                or lease.get("owner") != owner or lease.get("operation") != "release" or lease.get("epoch") != epoch
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Owned private release lease required")
        claim(record, owner, "release", now=self.clock())
        release_context(record)
        return record, etag
