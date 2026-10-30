"""Read-only review policy for the original, bounded rollback proposal.

The returned request is a preview, never approval or merge authority. A future
protected review workflow and private merge worker must repeat these checks.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

from automation_budget import validate_estimate
from automation_session import utc, validate
from chart_promotion import ColdPromotionChecks
from chart_selection import ALLOWED_FILES, blob, git_tree
from cleanup_completion import CONTEXT
from cleanup_schedule import verify_existing
from deployment_tasks import read_task
from lifecycle_dispatch import completed_manager, operation
from plan_review_store import read_json
from rollback_admission import preview_public_rollback
from rollback_proposal import baseline_restore_tree
from rollback_task_contract import original_record, read_proposal_receipt, slot, validate_prepare_checkpoint

class RollbackPromotionChecks(ColdPromotionChecks):
    kind = "rollback-promotion"

    def __init__(self, *args, infra, **kwargs):
        super().__init__(*args, **kwargs)
        self.infra = infra

    def base(self):
        record, etag = self.store.read()
        validate(record)
        value = validate_prepare_checkpoint(record)
        if (self.execution.get("action") != "deploy-window-rollback"
                or any(record.get(name) != self.execution.get(name) for name in CONTEXT)
                or record.get("executionVersion") != self.version or record.get("lease")
                or self.clock() >= utc(value["intent"]["expiresAt"])):
            raise ValueError("Rollback review requires the idle original opted-in proposal before expiry")
        validate_estimate(record, self.execution["costEstimate"])
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        return record, etag

    def guard(self, *, owner=None, epoch=None):
        if owner is not None or epoch is not None:
            raise ValueError("Read-only rollback preview cannot authorize an executor lease or merge")
        record, etag = self.base()
        execution, _ = read_json(self.clients["s3"], self.bucket, f"executions/{record['sessionId']}.json", self.version)
        if execution != self.execution: raise ValueError("Rollback review execution differs from its pinned version")
        if completed_manager(self.clients, self.execution, record) != operation("rollback", "prepare"):
            raise ValueError("Rollback review requires a positively terminal private preparation manager")
        value = slot(record)
        task = read_task(self.clients["s3"], self.bucket, record, value["prepareTaskId"], self.clock())
        if record["deploymentTask"].get("state") != "SUCCEEDED":
            raise ValueError("Rollback preparation remote task has not completed successfully")
        validate_prepare_checkpoint(record, task)
        read_proposal_receipt(self.clients["s3"], self.bucket, record, self.version)
        def original():
            current, current_etag = self.base()
            return original_record(current), current_etag
        intent = preview_public_rollback(self.clients, self.bucket, SimpleNamespace(read=original), self.execution, self.version,
            self.infra, clock=self.clock, created_at=value["intent"]["createdAt"])
        if intent != value["intent"]: raise ValueError("Rollback review has different original failure or baseline proof")
        latest, latest_etag = self.base()
        if latest != record or latest_etag != etag: raise ValueError("Rollback changed during preview evidence reads")
        return record

    def snapshot(self, record):
        return {name: deepcopy(record.get(name)) for name in (
            "rollbackAttempt", "acceptedRelease", "releaseCandidate", "deploymentTask", "lifecycleDispatch", "workflowHandoff")}

    def artifacts(self, record):
        proposal = read_proposal_receipt(self.clients["s3"], self.bucket, record, self.version)["proposal"]
        _, accepted = git_tree(self.chart, proposal["acceptedBaseline"]["chartSha"])
        manifest = json.loads(blob(self.chart, accepted["releases/staging.json"]["sha"]))
        return dict(manifest=manifest), proposal

    def tree(self, source, github, record, release, proposal):
        _, failed, _, expected, manifest = baseline_restore_tree(github, slot(record)["intent"], source)
        tree_sha, chosen = git_tree(github, proposal["headSha"])
        changed = sorted(name for name in set(failed) | set(chosen) if failed.get(name) != chosen.get(name))
        if chosen != expected or manifest != release["manifest"] or not set(changed) <= ALLOWED_FILES:
            raise ValueError("Rollback review candidate differs from the exact accepted release blobs")
        return dict(treeSha=tree_sha, changedFiles=changed)

    def request_context(self, record):
        value = slot(record)
        return dict(kind=self.kind, rollbackId=value["intent"]["rollbackId"], intentReference=deepcopy(value["intentReference"]),
            proposalReference=deepcopy(value["proposalReference"]), intent=deepcopy(value["intent"]))

    def evaluate(self, source, github, **kwargs):
        self.chart = github
        return super().evaluate(source, github, **kwargs)
