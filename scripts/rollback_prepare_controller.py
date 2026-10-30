"""Single-dispatch private rollback preparation, bounded to the original window.

This controller stops at a positively terminal, versioned proposal. Review,
merge and runtime acceptance are separate phases; the accepted baseline and
FAILED_NEEDS_CLEANUP status are never changed by this module.
"""
from copy import deepcopy

from lifecycle_dispatch import completed_manager, dispatch_identity, observed_build, operation
from rollback_reservation import arm_public_rollback, controller
from rollback_task_contract import read_proposal_receipt, slot


class RollbackPrepareController:
    def __init__(self, dispatcher, github, run_id):
        self.d, self.github, self.run_id = dispatcher, github, run_id

    def observe(self):
        d = self.d
        controller(d.clients, self.github, d.execution, self.run_id, d.clock())
        record, _ = d.store.read()
        # Validate identity/session first. A stale callback must not clean up a
        # different execution or decide that a new paid session is admitted.
        from cleanup_completion import CONTEXT
        if (record.get("executionVersion") != d.version
                or any(record.get(name) != d.execution.get(name) for name in CONTEXT)):
            raise ValueError("Rollback observation belongs to another immutable session")
        if record.get("status") != "FAILED_NEEDS_CLEANUP":
            raise ValueError("Rollback preparation cannot consume a healthy or closing session")
        try:
            if not record.get("rollbackAttempt"):
                arm_public_rollback(d.clients, d.bucket, d.store, d.execution, d.version, self.github, self.run_id, clock=d.clock)
            record, _ = d.current(idle=False, allow_rollback=True)
            value = slot(record)
            original = deepcopy(record)
            original["lifecycleDispatch"] = deepcopy(value["origin"]["lifecycleDispatch"])
            op = operation("rollback", "prepare")
            expected = dispatch_identity(original, d.version, op)
            dispatch = record.get("lifecycleDispatch", {})
            if dispatch == value["origin"]["lifecycleDispatch"]:
                # A recovered reservation is revalidated with its original
                # createdAt; retries never buy another 30 minutes or attempt.
                arm_public_rollback(d.clients, d.bucket, d.store, d.execution, d.version, self.github, self.run_id, clock=d.clock)
                latest, _ = d.current(allow_rollback=True)
                if latest != record: raise ValueError("Rollback reservation changed before its single dispatch")
                result = d.start(record, op)
                return dict(result, waiting="ROLLBACK_PREPARATION_RUNNING")
            if dispatch.get("id") != expected or dispatch.get("operation") != op or dispatch.get("phase") != "OBSERVED":
                raise ValueError("Rollback manager start is unknown; never repeat StartBuild")
            build = observed_build(d.clients["codebuild"], record)
            if build.get("buildStatus") == "IN_PROGRESS" and build.get("buildComplete") is False:
                return dict(waiting="ROLLBACK_PREPARATION_RUNNING", buildId=build["id"], sessionId=record["sessionId"])
            if completed_manager(d.clients, d.execution, record) != op:
                raise ValueError("Rollback proposal has no positively terminal private manager")
            receipt = read_proposal_receipt(d.clients["s3"], d.bucket, record, d.version)
            latest, _ = d.current(allow_rollback=True)
            if latest != record: raise ValueError("Rollback changed while reading its terminal proposal")
            return dict(waiting="ROLLBACK_REVIEW", sessionId=record["sessionId"], executionVersion=d.version,
                rollbackId=value["intent"]["rollbackId"], proposalReference=value["proposalReference"],
                pullRequest=receipt["proposal"]["pullRequest"])
        except BaseException:
            d.cleanup()
            raise
