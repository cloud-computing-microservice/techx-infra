"""Durable foundation/edge plan checkpoints and single-consumption apply.

The lifecycle worker supplies fresh observations while holding a deploy lease.
This controller does not bootstrap infrastructure, choose sources or initialize
the backend. Its runner must review the actual saved-plan JSON, then apply that
file with the approved backend/toolchain; see apply_saved_plan below.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile

from automation_budget import validate_estimate
from automation_session import claim, transition, utc
from plan_approval import context_for_session, create_request, digest, version
from plan_review_store import ACCOUNT, publish_review, read_authorized_plan, validate_changes


def validate_planning(record, stage, attempt):
    if stage not in ("foundation", "edge") or type(attempt) is not int or attempt < 1:
        raise ValueError("A supported stage and positive plan attempt are required")
    previous = record.get("stages", {}).get(stage)
    if previous and (previous.get("state") != "AWAITING_APPROVAL" or attempt != previous.get("attempt", 0) + 1):
        raise ValueError("Stage already consumed/uncertain or replan attempt is not sequential")
    if previous is None and attempt != 1:
        raise ValueError("A stage must begin at attempt 1")
    if stage == "edge" and record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED":
        raise ValueError("Edge planning requires a completed foundation")
    return previous


def apply_saved_plan(environment_root, plan_path, stage, run=subprocess.run):
    """Fixed commands only; no shell or arbitrary workflow command is accepted."""
    environment_root = Path(environment_root).resolve()
    if environment_root.name != "staging" or environment_root.parent.name != "environments":
        raise ValueError("Only the bundled staging Terraform root may be applied")
    if stage not in ("foundation", "edge"):
        raise ValueError("Unknown deployment stage")
    plan_json = run(["terraform", f"-chdir={environment_root}", "show", "-json", str(plan_path)],
                    check=True, capture_output=True)
    # JSON remains private in memory. The policy script outputs counts only.
    review_script = Path(__file__).with_name("review-plan.py")
    import sys
    run([sys.executable, str(review_script), stage], input=plan_json.stdout, check=True, capture_output=True)
    run(["terraform", f"-chdir={environment_root}", "apply", "-input=false", "-lock-timeout=60s", "-no-color", str(plan_path)], check=True)


class StageController:
    def __init__(self, client, store, bucket, clock=None):
        self.s3, self.store, self.bucket = client, store, bucket
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def owned(self, session_id, owner, epoch):
        record, etag = self.store.read()
        lease = record.get("lease", {})
        if (record.get("sessionId") != session_id or lease.get("owner") != owner
                or lease.get("operation") != "deploy" or lease.get("epoch") != epoch):
            raise ValueError("Current deployment lease is required")
        # Validate the operation/state/time without mutating or extending TTL.
        # transition below also checks that the existing lease has not expired.
        claim(record, owner, "deploy", now=self.clock())
        if utc(lease["expiresAt"]) <= self.clock():
            raise ValueError("Deployment lease expired")
        return record, etag

    def propose(self, session_id, owner, epoch, stage, attempt, payload, context, changes, estimate, expires_at):
        record, _ = self.owned(session_id, owner, epoch)
        now = self.clock()
        context_for_session(record, context, now)
        if context["backendBucket"] != self.bucket:
            raise ValueError("Stage artifacts and backend must use the same control bucket")
        validate_estimate(record, estimate)
        validate_changes(changes)
        # Validate all fields before uploading private data. A real object
        # version replaces this placeholder immediately after the write.
        request = create_request(record, stage, attempt, payload, "pending-upload", context, expires_at=expires_at, now=now)
        previous = validate_planning(record, stage, attempt)
        key = request["plan"]["key"]
        try:
            result = self.s3.put_object(Bucket=self.bucket, Key=key, Body=payload,
                                       IfNoneMatch="*", ExpectedBucketOwner=ACCOUNT)
            object_version = result.get("VersionId")
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            result = self.s3.get_object(Bucket=self.bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
            if result["Body"].read(len(payload) + 1) != payload:
                raise ValueError("Plan attempt already contains different saved-plan bytes") from error
            object_version = result.get("VersionId")
        version(object_version)
        request["plan"]["versionId"] = object_version
        ref = publish_review(self.s3, self.bucket, request, changes, now, estimate=estimate)
        # Recheck after S3 calls; do not publish a pointer after lease takeover.
        current, etag = self.owned(session_id, owner, epoch)
        if current.get("stages", {}).get(stage) != previous or current.get("pendingPlan") != record.get("pendingPlan"):
            raise ValueError("Pending stage changed while publishing the review")
        context_for_session(current, context, self.clock())
        updated = deepcopy(current)
        if updated["status"] == "PLANNING":
            updated = transition(updated, owner, "PROVISIONING", now=self.clock())
        updated.setdefault("stages", {})[stage] = dict(attempt=attempt, state="AWAITING_APPROVAL", reviewReference=ref)
        updated["pendingPlan"] = ref
        self.store.write(updated, etag)
        return ref

    def apply(self, session_id, owner, epoch, approval_ref, observe_context, run_apply):
        """Apply at most once; ambiguous/error results require cleanup/reconciliation.

        observe_context must read actual backend state/config/network/toolchain,
        not copy request.context. The independent executor/heartbeat owns the
        lease throughout run_apply, including cancellation and cleanup fencing.
        """
        record, _ = self.owned(session_id, owner, epoch)
        stage = approval_ref.get("stage")
        checkpoint = record.get("stages", {}).get(stage, {})
        ref = checkpoint.get("reviewReference")
        if checkpoint.get("state") != "AWAITING_APPROVAL" or ref != record.get("pendingPlan"):
            raise ValueError("Stage is not awaiting approval; never replay an applied/uncertain plan")
        context = observe_context()
        payload, permit = read_authorized_plan(self.s3, self.bucket, ref, approval_ref, record, context, self.clock())
        current, etag = self.owned(session_id, owner, epoch)
        if current.get("stages", {}).get(stage) != checkpoint or current.get("pendingPlan") != ref:
            raise ValueError("Stage changed before apply consumption")
        applying = deepcopy(current)
        applying["stages"][stage] = dict(checkpoint, state="APPLYING", permit=permit, startedAt=self.clock().isoformat())
        self.store.write(applying, etag)  # Durable before the first Terraform mutation.
        try:
            # Recheck the lease and fresh observations after download/CAS.
            self.owned(session_id, owner, epoch)
            if observe_context() != context:
                raise ValueError("Observed deployment context changed immediately before apply")
            with tempfile.TemporaryDirectory(prefix="techx-approved-plan-") as directory:
                path = Path(directory) / "saved.tfplan"
                path.write_bytes(payload)
                path.chmod(0o600)
                run_apply(path, stage)
            after = observe_context()
            if (after.get("stateLineage") != context["stateLineage"]
                    or type(after.get("stateSerial")) is not int or after["stateSerial"] <= context["stateSerial"]
                    or after != dict(context, stateSerial=after["stateSerial"])):
                raise ValueError("Successful create-only apply requires observed state advancement")
            current, etag = self.owned(session_id, owner, epoch)
            if current.get("stages", {}).get(stage) != applying["stages"][stage]:
                raise ValueError("Apply checkpoint changed unexpectedly")
            completed = deepcopy(current)
            completed["stages"][stage] = dict(applying["stages"][stage], state="APPLIED", completedAt=self.clock().isoformat(),
                                              stateLineage=after["stateLineage"], stateSerial=after["stateSerial"],
                                              savedPlanSha256=digest(payload))
            completed.pop("pendingPlan", None)
            self.store.write(completed, etag)
            return completed["stages"][stage]
        except BaseException:
            # Do not overwrite a cleanup takeover or a newer CAS owner. No
            # automatic retry can know whether Terraform already committed.
            current, etag = self.store.read()
            lease = current.get("lease", {})
            if (current.get("sessionId") == session_id and lease.get("owner") == owner
                    and lease.get("epoch") == epoch and current.get("stages", {}).get(stage) == applying["stages"][stage]):
                uncertain = deepcopy(current)
                uncertain["stages"][stage]["state"] = "UNCERTAIN"
                uncertain["status"] = "FAILED_NEEDS_CLEANUP"
                self.store.write(uncertain, etag)
            raise
