"""Single-dispatch private rollback preparation, bounded to the original window.

This controller stops at a positively terminal, versioned proposal. Review,
merge and runtime acceptance are separate phases; the accepted baseline and
FAILED_NEEDS_CLEANUP status are never changed by this module.
"""
from copy import deepcopy

from lifecycle_dispatch import completed_manager, dispatch_identity, observed_build, operation
from rollback_reservation import arm_rollback, controller
from rollback_task_contract import read_proposal_receipt, slot
from workflow_handoff import HandoffDriver


class RollbackHandoffDriver(HandoffDriver):
    """The failed-session read path is restricted to its prepared review."""
    def current(self, **kwargs):
        return self.dispatcher.current(allow_rollback=True, **kwargs)

    def start(self, target):
        if target.get("operation") not in ("review-rollback", "accept-rollback-public"):
            raise ValueError("Rollback may dispatch only its exact protected review or public acceptance")
        if target["operation"] == "accept-rollback-public":
            from rollback_rollout_contract import read_rollback_rollout
            record, _ = self.current(idle=False)
            read_rollback_rollout(self.dispatcher.clients["s3"], self.dispatcher.bucket, record, self.dispatcher.version)
            state = record.get("workflowHandoff", {})
            if state.get("operation") == "accept-rollback-public" and state.get("phase") in ("CALLING", "OBSERVED", "CONSUMED"):
                _, intent = self.load()
                if intent["target"] != target: raise ValueError("Rollback public recovery changed its target")
                return self.recover()
            if record["rollbackAttempt"].get("publicAcceptance"):
                raise ValueError("Rollback public acceptance cannot dispatch another attempt")
            record, _ = self.current()
            if completed_manager(self.dispatcher.clients, self.dispatcher.execution, record) != operation("rollback", "rollout"):
                raise ValueError("Rollback public handoff requires its terminal restore manager")
            return super().start(target)
        from chart_selection import INFRA_REPO
        from rollback_admission import preview_rollback
        from rollback_task_contract import original_record, validate_prepare_checkpoint
        from types import SimpleNamespace
        record, etag = self.current()
        value = validate_prepare_checkpoint(record)
        if completed_manager(self.dispatcher.clients, self.dispatcher.execution, record) != operation("rollback", "prepare"):
            raise ValueError("Rollback review handoff requires the terminal private preparation manager")
        read_proposal_receipt(self.dispatcher.clients["s3"], self.dispatcher.bucket, record, self.dispatcher.version)
        original_handoff = value["origin"]["workflowHandoff"]
        if record.get("workflowHandoff") == original_handoff:
            def original():
                current, current_etag = self.current()
                return original_record(current), current_etag
            intent = preview_rollback(self.dispatcher.clients, self.dispatcher.bucket, SimpleNamespace(read=original),
                self.dispatcher.execution, self.dispatcher.version, self.apis[INFRA_REPO], clock=self.dispatcher.clock,
                created_at=value["intent"]["createdAt"])
            latest, latest_etag = self.current()
            if intent != value["intent"] or latest != record or latest_etag != etag:
                raise ValueError("Original failed handoff changed before bounded rollback consumption")
            updated = deepcopy(record)
            updated["workflowHandoff"]["phase"] = "CONSUMED"
            self.dispatcher.store.write(updated, etag)
        return super().start(target)


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
                arm_rollback(d.clients, d.bucket, d.store, d.execution, d.version, self.github, self.run_id, clock=d.clock)
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
                arm_rollback(d.clients, d.bucket, d.store, d.execution, d.version, self.github, self.run_id, clock=d.clock)
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
