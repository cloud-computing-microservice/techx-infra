"""Observe one bounded rollback through protected review and fresh acceptance."""
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

    def observe_private(self):
        d = self.d
        controller(d.clients, self.infra, d.execution, self.run_id, d.clock())
        scoped, _ = d.store.read()
        if (scoped.get("executionVersion") != d.version or scoped.get("status") not in ("FAILED_NEEDS_CLEANUP", "READY")
                or scoped.get("status") == "READY" and scoped.get("lifecycleDispatch", {}).get("operation") != operation("verify", "rollback")
                or any(scoped.get(name) != d.execution.get(name) for name in CONTEXT)):
            raise ValueError("Private rollback continuation belongs to another immutable session")
        try:
            from accepted_rollback_history import accepted_projection
            from accepted_release_history import read_accepted_history
            from rollback_private_contract import restoration_record
            if scoped["status"] == "READY":
                historical = accepted_projection(scoped)
            else:
                if not slot(scoped).get("restoration"):
                    progress = self.observe_public()
                    if progress.get("waiting") != "ROLLBACK_PRIVATE_ACCEPTANCE": return progress
                scoped, _ = d.current(idle=False, allow_rollback=True)
                historical = scoped
            op = operation("verify", "rollback")
            predecessor = restoration_record(historical)
            predecessor["rollbackAttempt"].pop("privateAcceptance", None)
            expected = dispatch_identity(predecessor, d.version, op)
            actual = scoped.get("lifecycleDispatch", {})
            if scoped["status"] != "READY" and actual == slot(historical)["restoration"]["lifecycleDispatch"]:
                current, _ = d.current(allow_rollback=True)
                if current != scoped: raise ValueError("Rollback changed before private acceptance dispatch")
                return dict(d.start(current, op), waiting="ROLLBACK_PRIVATE_RUNNING")
            if actual.get("id") != expected or actual.get("operation") != op or actual.get("phase") != "OBSERVED":
                raise ValueError("Rollback private manager start is unknown; never repeat StartBuild")
            build = observed_build(d.clients["codebuild"], scoped)
            if build.get("buildStatus") == "IN_PROGRESS" and build.get("buildComplete") is False:
                return dict(waiting="ROLLBACK_PRIVATE_RUNNING", buildId=build["id"])
            if completed_manager(d.clients, d.execution, scoped) != op:
                raise ValueError("Rollback private acceptance lacks its terminal successful manager")
            accepted = read_accepted_history(d.clients["s3"], d.bucket, scoped, d.version)
            latest, _ = d.current(allow_ready=True)
            if latest != scoped: raise ValueError("Rollback changed during accepted history readback")
            return dict(waiting="READY", sessionId=scoped["sessionId"], acceptedRelease=accepted)
        except BaseException:
            d.cleanup()
            raise

    def observe_public(self):
        d = self.d
        controller(d.clients, self.infra, d.execution, self.run_id, d.clock())
        scoped, _ = d.store.read()
        if (scoped.get("executionVersion") != d.version or scoped.get("status") != "FAILED_NEEDS_CLEANUP"
                or any(scoped.get(name) != d.execution.get(name) for name in CONTEXT)):
            raise ValueError("Public rollback continuation belongs to another immutable session")
        try:
            record, _ = d.current(idle=False, allow_rollback=True)
            state = record.get("workflowHandoff", {})
            if state.get("operation") != "accept-rollback-public":
                progress = self.observe()
                if progress.get("waiting") != "ROLLBACK_PUBLIC_ACCEPTANCE": return progress
                record, _ = d.current(allow_rollback=True)
                response = self.handoff.start(target_for("accept-rollback-public", record, d.execution))
                return dict(waiting="ROLLBACK_PUBLIC_RUNNING", workflowRunId=response["workflow_run_id"])
            if state.get("phase") == "PREPARED": self.handoff.start(target_for("accept-rollback-public", record, d.execution))
            record, intent, run = self.handoff.observe()
            state = record["workflowHandoff"]
            if run["status"] in LIVE: return dict(waiting="ROLLBACK_PUBLIC_RUNNING", workflowRunId=run["id"])
            if run.get("conclusion") != "success": raise ValueError("Rollback public workflow failed/cancelled; cleanup required")
            from rollback_public_contract import read_public_receipt
            proof = read_public_receipt(d.clients["s3"], d.bucket, record, d.version)
            if proof["dispatchId"] != intent["dispatchId"] or proof["workflowRun"] != f"https://github.com/{INFRA_REPO}/actions/runs/{run['id']}":
                raise ValueError("Rollback public receipt belongs to another dispatched run")
            jobs = self.infra.get(f"actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
            rows = jobs.get("jobs", [])
            public = [job for job in rows if job.get("name") == "public"]
            if (type(jobs.get("total_count")) is not int or jobs["total_count"] != len(rows) or len(rows) > 10 or len(public) != 1
                    or any(public[0].get(name) != expected for name, expected in dict(id=proof["runner"]["jobId"], run_id=run["id"],
                        head_sha=d.execution["sourceRevisions"]["techx-infra"], runner_id=proof["runner"]["runnerId"],
                        runner_group_name="GitHub Actions", labels=["ubuntu-24.04"], status="completed", conclusion="success").items())):
                raise ValueError("Rollback public result lacks the same actual terminal hosted job")
            current, etag = d.current(allow_rollback=True)
            completed_manager(d.clients, d.execution, current)
            if current != record: raise ValueError("Rollback changed during public workflow observation")
            if state.get("phase") == "CONSUMED" and slot(current).get("restoration"):
                return dict(waiting="ROLLBACK_PRIVATE_ACCEPTANCE", rollbackId=slot(current)["intent"]["rollbackId"])
            ledger = deepcopy(current["rollbackAttempt"]); ledger.pop("publicAcceptance", None)
            if (state.get("phase") != "OBSERVED" or intent["predecessor"].get("rollbackAttempt") != ledger
                    or any(intent["predecessor"].get(name) != current.get(name) for name in
                        ("status", "lifecycleDispatch", "deploymentTask", "releaseCandidate", "acceptedRelease"))):
                raise ValueError("Rollback public predecessor changed before consumption")
            updated = deepcopy(current)
            updated["workflowHandoff"]["phase"] = "CONSUMED"
            updated["rollbackAttempt"]["restoration"] = {name: deepcopy(updated[name]) for name in ("lifecycleDispatch", "deploymentTask", "workflowHandoff")}
            slot(updated)
            d.store.write(updated, etag)
            return dict(waiting="ROLLBACK_PRIVATE_ACCEPTANCE", rollbackId=slot(updated)["intent"]["rollbackId"])
        except BaseException:
            d.cleanup()
            raise

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
