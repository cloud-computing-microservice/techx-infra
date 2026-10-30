"""VPN/runtime acceptance of a restored rollback; this worker cannot set READY."""
from automation_session import claim, utc
from cleanup_completion import CONTEXT
from edge_activation import read_activation_receipt
from gitops_bootstrap import SESSION_ANNOTATION
from plan_review_store import checksum
from private_acceptance import PrivateAcceptance
from private_active_rollout import STAMP
from private_http import PrivateFailure
from ready_release_contract import accepted_baseline
from rollback_private_contract import private_context, read_private_receipt, restored_manifest, result_key
from rollback_public_contract import network_record, read_public_receipt
from rollback_rollout_contract import read_rollback_rollout
from rollback_task_contract import slot


class RollbackAcceptanceController:
    def __init__(self, store, clock): self.store, self.clock = store, clock

    def owned(self, session_id, owner, epoch):
        record, etag = self.store.read()
        lease = record.get("lease", {})
        if (record.get("sessionId") != session_id or record.get("status") != "FAILED_NEEDS_CLEANUP"
                or lease.get("owner") != owner or lease.get("operation") != "rollback" or lease.get("epoch") != epoch
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Rollback private checks require their live executor lease")
        claim(record, owner, "rollback", now=self.clock())
        return record, etag


class RollbackPrivateAcceptance(PrivateAcceptance):
    session_status = "FAILED_NEEDS_CLEANUP"
    task_stage = "rollback"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller = RollbackAcceptanceController(self.store, self.clock)

    def context(self, record): return private_context(record)
    def validate_start(self, record):
        if self.stage(record): raise PrivateFailure("rollback-private-task-consumed")
        return checksum(private_context(record))
    def projected(self, record): return network_record(record)
    def stage(self, record): return slot(record).get("privateAcceptance", {})
    def set_stage(self, record, value): record["rollbackAttempt"]["privateAcceptance"] = value
    def public_reference(self, record): return slot(record)["publicAcceptance"]["receiptReference"]
    def receipt_fields(self, record):
        ledger = slot(record)
        return dict(kind="rollback-private-acceptance", rollbackId=ledger["intent"]["rollbackId"], rolloutReference=ledger["rolloutReference"])
    def receipt_key(self, record): return result_key(record, self.task_id)
    def read_receipt(self, record): return read_private_receipt(self.runtime.clients["s3"], self.runtime.bucket, record, self.version)

    def validate_record(self, record):
        super().validate_record(record)
        if (any(record.get(name) != self.runtime.execution.get(name) for name in CONTEXT)
                or self.clock() >= utc(slot(record)["intent"]["expiresAt"])):
            raise PrivateFailure("rollback-private-context-expired")
        return record

    def predecessors(self, record):
        s3, bucket = self.runtime.clients["s3"], self.runtime.bucket
        # Read-only validation of the still accepted predecessor. Runtime
        # authority comes from the new restored rollout and new public receipt.
        accepted_baseline(s3, bucket, dict(record, status="TESTING"), self.version)
        self.restored = read_rollback_rollout(s3, bucket, record, self.version)
        return (read_public_receipt(s3, bucket, record, self.version),
            read_activation_receipt(s3, bucket, network_record(record), self.version),
            dict(chartSha=self.restored["chartSha"]), restored_manifest(s3, bucket, record, self.version))

    def validate_application(self, app):
        expected = self.restored["application"]
        metadata = app.get("metadata", {})
        annotations = metadata.get("annotations", {})
        if (metadata.get("uid") != expected["uid"] or annotations.get(SESSION_ANNOTATION) != self.runtime.execution["sessionId"]
                or annotations.get(STAMP) != expected["stamp"] or checksum(app.get("spec")) != expected["specSha256"]):
            raise PrivateFailure("rollback-application-identity")

    def wait_ready(self, **kwargs):
        super().wait_ready(**kwargs)
        # Readiness can take time after either resilience mutation. Recheck
        # identity after that wait as well as via authenticated private HTTP.
        self.validate_application(self.kube.get("application", "techx-staging", "argocd"))
