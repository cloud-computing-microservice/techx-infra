"""Private VPN/runtime suite for an active release without replacing cold proof."""
from automation_session import claim, utc
from active_private_contract import (active_private_context, private_result_key, read_active_private_receipt, validate_active_private_start)
from active_public_contract import read_active_public_receipt
from active_release_contract import read_rollout_receipt
from edge_activation import read_activation_receipt
from private_acceptance import PrivateAcceptance
from private_acceptance_contract import evidence_record
from ready_release_contract import accepted_baseline, read_manifest


class ActiveAcceptanceController:
    def __init__(self, store, clock): self.store, self.clock = store, clock

    def owned(self, session_id, owner, epoch):
        record, etag = self.store.read()
        lease = record.get("lease", {})
        if (record.get("sessionId") != session_id or record.get("status") != "TESTING"
                or lease.get("owner") != owner or lease.get("operation") != "acceptance" or lease.get("epoch") != epoch
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Active private acceptance requires its live executor lease")
        claim(record, owner, "acceptance", now=self.clock())
        return record, etag


class ActivePrivateAcceptance(PrivateAcceptance):
    session_status = "TESTING"
    task_stage = "active"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller = ActiveAcceptanceController(self.store, self.clock)

    def context(self, record): return active_private_context(record)
    def validate_start(self, record): return validate_active_private_start(record, 1)
    def projected(self, record): return evidence_record(record)
    def stage(self, record): return record.get("releaseCandidate", {}).get("privateAcceptance", {})
    def set_stage(self, record, value): record["releaseCandidate"]["privateAcceptance"] = value
    def public_reference(self, record): return record["releaseCandidate"]["publicAcceptance"]["receiptReference"]
    def receipt_fields(self, record):
        return dict(kind="active-private-acceptance", releaseId=record["releaseCandidate"]["id"], rolloutReference=record["releaseCandidate"]["rollout"]["receiptReference"])
    def receipt_key(self, record): return private_result_key(record, self.task_id)
    def read_receipt(self, record): return read_active_private_receipt(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)

    def perform(self, owner, epoch):
        try:
            return super().perform(owner, epoch)
        except BaseException as error:
            # Preserve the original failure. Cleanup/lease loss, ambiguous S3
            # writes and pre-checkpoint errors cannot manufacture authority.
            from active_private_failure import persist_failure
            try:
                if self.failure_checkpoint_owned: persist_failure(self, error)
            except Exception: pass
            raise

    def predecessors(self, record):
        s3, bucket = self.runtime.clients["s3"], self.runtime.bucket
        accepted_baseline(s3, bucket, record, self.version)
        rollout = read_rollout_receipt(s3, bucket, record, self.version)
        return (read_active_public_receipt(s3, bucket, record, self.version),
            read_activation_receipt(s3, bucket, evidence_record(record), self.version),
            dict(chartSha=rollout["chartSha"]), read_manifest(s3, bucket, record, self.version))
