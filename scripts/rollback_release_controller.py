"""Observe one bounded rollback through review and private restore.

This source stops at ROLLBACK_PUBLIC_ACCEPTANCE. Restored images and a healthy
Application alone do not authorize READY, a new accepted baseline or history.
"""
from copy import deepcopy

from chart_promotion import PromotionPending, candidate_ci, pull_identity
from chart_selection import INFRA_REPO
from cleanup_completion import CONTEXT
from lifecycle_dispatch import completed_manager, dispatch_identity, observed_build, operation
from rollback_grant import consume_grant
from rollback_prepare_controller import RollbackHandoffDriver, RollbackPrepareController
from rollback_reservation import controller
from rollback_rollout_contract import read_rollback_rollout
from rollback_task_contract import read_proposal_receipt, slot
from workflow_handoff import LIVE, target_for


class RollbackReleaseController:
    def __init__(self, dispatcher, infra, chart, source, run_id):
        self.d, self.infra, self.chart, self.source, self.run_id = dispatcher, infra, chart, source, run_id
        self.prepare = RollbackPrepareController(dispatcher, infra, run_id)
        self.handoff = RollbackHandoffDriver(dispatcher, {INFRA_REPO: infra})

    def observe(self):
        d = self.d
        controller(d.clients, self.infra, d.execution, self.run_id, d.clock())
        scoped, _ = d.store.read()
        if (scoped.get("executionVersion") != d.version or scoped.get("status") != "FAILED_NEEDS_CLEANUP"
                or any(scoped.get(name) != d.execution.get(name) for name in CONTEXT)):
            raise ValueError("Rollback continuation belongs to another session or no failed execution")
        try:
            if scoped.get("rollbackAttempt", {}).get("phase") not in ("APPROVED", "RESTORED"):
                result = self.prepare.observe()
                if result.get("waiting") != "ROLLBACK_REVIEW": return result
                record, _ = d.current(allow_rollback=True)
                proposal = read_proposal_receipt(d.clients["s3"], d.bucket, record, d.version)["proposal"]
                try:
                    pull_identity(self.chart, proposal)
                    candidate_ci(self.chart, proposal, d.execution)
                except PromotionPending:
                    return dict(waiting="ROLLBACK_CHART_REVIEW", pullRequest=proposal["pullRequest"])
                state = record.get("workflowHandoff", {})
                if state.get("operation") != "review-rollback":
                    response = self.handoff.start(target_for("review-rollback", record, d.execution))
                    return dict(waiting="ROLLBACK_REVIEW_RUNNING", workflowRunId=response["workflow_run_id"])
                if state.get("phase") in ("PREPARED", "CALLING"):
                    self.handoff.start(target_for("review-rollback", record, d.execution))
                _, _, run = self.handoff.observe()
                if run["status"] in LIVE: return dict(waiting="ROLLBACK_REVIEW_RUNNING", workflowRunId=run["id"])
                if run.get("conclusion") != "success": raise ValueError("Rollback protected review failed or was cancelled")
                consume_grant(d, self.infra, self.chart, self.source, self.run_id)
            record, _ = d.current(idle=False, allow_rollback=True)
            value = slot(record)
            op = operation("rollback", "rollout")
            previous = deepcopy(record)
            previous["lifecycleDispatch"] = deepcopy(value["preparation"]["lifecycleDispatch"])
            expected = dispatch_identity(previous, d.version, op)
            actual = record.get("lifecycleDispatch", {})
            if actual == value["preparation"]["lifecycleDispatch"]:
                consume_grant(d, self.infra, self.chart, self.source, self.run_id)
                latest, _ = d.current(allow_rollback=True)
                if latest != record: raise ValueError("Rollback grant changed before its one rollout dispatch")
                result = d.start(record, op)
                return dict(result, waiting="ROLLBACK_ROLLOUT_RUNNING")
            if actual.get("id") != expected or actual.get("operation") != op or actual.get("phase") != "OBSERVED":
                raise ValueError("Rollback rollout manager start is unknown; never repeat StartBuild")
            build = observed_build(d.clients["codebuild"], record)
            if build.get("buildStatus") == "IN_PROGRESS" and build.get("buildComplete") is False:
                return dict(waiting="ROLLBACK_ROLLOUT_RUNNING", buildId=build["id"])
            if completed_manager(d.clients, d.execution, record) != op: raise ValueError("Rollback restore lacks its terminal private manager")
            receipt = read_rollback_rollout(d.clients["s3"], d.bucket, record, d.version)
            latest, _ = d.current(allow_rollback=True)
            if latest != record: raise ValueError("Rollback changed during restored receipt readback")
            return dict(waiting="ROLLBACK_PUBLIC_ACCEPTANCE", sessionId=record["sessionId"], executionVersion=d.version,
                rollbackId=value["intent"]["rollbackId"], chartSha=receipt["chartSha"], rolloutReference=value["rolloutReference"])
        except BaseException:
            d.cleanup()
            raise
