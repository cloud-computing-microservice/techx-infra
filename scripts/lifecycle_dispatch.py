"""Single-consumption CodeBuild dispatch and checkpoint-driven continuation.

StartBuild tokens expire. The durable CALLING intent, rather than that token,
prevents a delayed GitHub retry from creating a second manager. Unknown starts
are reconciled by independent cleanup; they are never silently resubmitted.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import re

from automation_budget import validate_estimate
from automation_session import utc, validate
from cleanup_schedule import verify_existing
from deployment_manager import previous_terminal
from deployment_tasks import ACCOUNT, executor_config, validate_bootstrap, validate_gitops_checkpoint, validate_promotion, validate_promotion_checkpoint, validate_activation, validate_exposure_checkpoint
from plan_review_store import encoded, read_json

PROJECT = "techx-automation-lifecycle"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/techx-automation-lifecycle-manager"
BUILD = rf"{PROJECT}:[a-f0-9-]+"
CONTEXT = ("sessionId", "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action")


def operation(mode, stage, attempt=1):
    if (type(attempt) is not int or not 1 <= attempt <= 999999
            or (mode, stage) not in {("prepare", "foundation"), ("plan", "foundation"), ("apply", "foundation"),
                                     ("bootstrap", "gitops"), ("promote", "chart"), ("plan", "edge"), ("apply", "edge"), ("activate", "exposure"), ("verify", "private"), ("verify", "active"), ("verify", "rollback"), ("release", "active"), ("rollback", "prepare"), ("rollback", "rollout")}
            or mode in ("prepare", "bootstrap", "promote", "activate", "verify", "release", "rollback") and attempt != 1):
        raise ValueError("Unsupported typed lifecycle operation")
    return dict(mode=mode, stage=stage, attempt=attempt)


def environment(record, op):
    operation(**op)
    return dict(TECHX_SESSION_ID=record["sessionId"], TECHX_EXECUTION_VERSION=record["executionVersion"],
                TECHX_MODE=op["mode"], TECHX_PLAN_STAGE=op["stage"], TECHX_PLAN_ATTEMPT=str(op["attempt"]))


def dispatch_identity(record, execution_version, op):
    context = dict(sessionId=record["sessionId"], executionVersion=execution_version, operation=op,
        predecessor=record.get("lifecycleDispatch", {}).get("id"),
        pendingPlan=record.get("pendingPlan") if op["mode"] == "apply" else None,
        bootstrapChart=record.get("bootstrapChart") if op["mode"] == "bootstrap" else None)
    if op["mode"] == "promote": context["promotionApproval"] = record.get("bootstrapPromotionApproval")
    if op["mode"] == "activate": context["exposureSha256"] = validate_activation(record, op["attempt"])
    if op["mode"] == "verify":
        from private_acceptance_contract import validate_private_start
        from active_private_contract import validate_active_private_start
        if op["stage"] == "rollback":
            from rollback_private_contract import validate_private_start as validate_rollback_private_start
            context["acceptanceSha256"] = validate_rollback_private_start(record, op["attempt"])
        else: context["acceptanceSha256"] = (validate_active_private_start if op["stage"] == "active" else validate_private_start)(record, op["attempt"])
    if op["mode"] == "release":
        from active_release_contract import release_context
        context["releaseContext"] = release_context(record)
    if op["mode"] == "rollback":
        from rollback_task_contract import context as rollback_context, rollout_context
        context["rollbackContext"] = (rollout_context if op["stage"] == "rollout" else rollback_context)(record)
    return hashlib.sha256(encoded(context)).hexdigest()


def guard(record, execution, version, now, *, idle=True, allow_ready=False, allow_rollback=False):
    validate(record)
    executor_config(execution["deploymentConfig"])
    active = record["status"] == "TESTING" and record.get("lifecycleDispatch", {}).get("operation") in (operation("release", "active"), operation("verify", "active"))
    rollback = allow_rollback and record["status"] == "FAILED_NEEDS_CLEANUP"
    if rollback:
        from cleanup_completion import CONTEXT as immutable_context
        from rollback_task_contract import slot
        value = slot(record)
        if (execution.get("action") != "deploy-window-rollback"
                or any(record.get(name) != execution.get(name) for name in immutable_context)
                or now >= utc(value["intent"]["expiresAt"])):
            raise ValueError("Rollback dispatch changed immutable opt-in context or expired")
    if (any(record.get(key) != execution.get(key) for key in CONTEXT) or record.get("executionVersion") != version
            or not (active or rollback or record["status"] in (("PLANNING", "PROVISIONING", "READY") if allow_ready else ("PLANNING", "PROVISIONING"))) or now >= utc(record["cleanupStartAt"])
            or idle and record.get("lease")):
        raise ValueError("Dispatch requires the same open, idle admitted staging session")
    validate_estimate(record, execution["costEstimate"])
    if active:
        from active_release_contract import release_context
        release_context(record)
    if record["status"] == "READY":
        from deployment_tasks import validate_result
        if record.get("lifecycleDispatch", {}).get("operation") not in (operation("verify", "private"), operation("verify", "active"), operation("verify", "rollback")):
            raise ValueError("READY observation requires the exact private acceptance manager")
        validate_result(record, record.get("deploymentTask", {}))


def observed_build(client, record):
    """Return the exact live service observation; never infer exit from a file."""
    ref = record.get("lifecycleDispatch", {})
    if ref.get("phase") != "OBSERVED" or not re.fullmatch(BUILD, ref.get("buildId", "")):
        raise ValueError("Previous manager start is not positively observed; cleanup/reconciliation required")
    response = client.batch_get_builds(ids=[ref["buildId"]])
    builds = response.get("builds", [])
    arn = f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/{ref['buildId']}"
    if (response.get("buildsNotFound") or len(builds) != 1 or builds[0].get("id") != ref["buildId"]
            or builds[0].get("arn") != arn or builds[0].get("projectName") != PROJECT or builds[0].get("serviceRole") != ROLE):
        raise ValueError("Manager observation differs from the fixed deployment project/role")
    build = builds[0]
    rows = [row for row in build.get("environment", {}).get("environmentVariables", []) if row.get("name", "").startswith("TECHX_")]
    expected = environment(record, ref["operation"])
    if (len(rows) != len(expected) or {row["name"]: row.get("value") for row in rows} != expected
            or any(row.get("type") != "PLAINTEXT" for row in rows)):
        raise ValueError("Manager ran different lifecycle inputs")
    return build


def completed_manager(clients, execution, record):
    build = observed_build(clients["codebuild"], record)
    if build.get("buildStatus") != "SUCCEEDED" or build.get("buildComplete") is not True:
        raise ValueError("Previous manager must be observed terminal and successful before dispatch")
    op = record["lifecycleDispatch"]["operation"]
    if op["mode"] == "prepare":
        prepared = record.get("executorPrepared", {})
        if (prepared.get("managerArn") != build["arn"] or prepared.get("imageId") != execution["deploymentConfig"]["imageId"]
                or not re.fullmatch(r"i-[a-f0-9]{8,17}", prepared.get("instanceId", "")) or record.get("deploymentTask")):
            raise ValueError("Prepared executor has no matching successful manager checkpoint")
    else:
        task = record.get("deploymentTask", {})
        if task.get("managerArn") != build["arn"] or any(task.get(key) != op[key] for key in op):
            raise ValueError("Current task is not the previous manager's exact operation")
        previous_terminal(clients["ssm"], executor_config(execution["deploymentConfig"]), record)
    return op


def next_operation(record, previous):
    """Only supported edges are returned; external phases remain explicit waits."""
    stages = record.get("stages", {})
    if record.get("status") == "TESTING":
        from active_release_contract import validate_rollout_checkpoint
        if previous == operation("verify", "active"):
            raise ValueError("Completed active private acceptance must publish atomic READY")
        if previous != operation("release", "active"): raise ValueError("Active rollout requires its exact completed manager")
        validate_rollout_checkpoint(record)
        if record["releaseCandidate"].get("publicAcceptance"):
            from active_public_contract import validate_active_public_checkpoint
            validate_active_public_checkpoint(record)
            return operation("verify", "active")
        return dict(waiting="ACTIVE_PUBLIC_ACCEPTANCE", releaseId=record["releaseCandidate"]["id"])
    if record.get("status") == "READY":
        from deployment_tasks import validate_result
        if previous not in (operation("verify", "private"), operation("verify", "active"), operation("verify", "rollback")):
            raise ValueError("READY has no completed private predecessor")
        task = record.get("deploymentTask", {})
        if any(task.get(key) != previous[key] for key in previous): raise ValueError("READY private task differs from its completed manager")
        validate_result(record, task)
        return dict(waiting="READY")
    if record.get("pendingPlan") is not None:
        ref = record["pendingPlan"]
        if previous != operation("plan", ref["stage"], ref["attempt"]):
            raise ValueError("Pending plan has no matching completed planning operation")
        return dict(waiting="PLAN_APPROVAL", stage=ref["stage"], attempt=ref["attempt"])
    if not stages:
        if previous != operation("prepare", "foundation"):
            raise ValueError("Foundation planning requires executor preparation")
        return operation("plan", "foundation")
    foundation = stages.get("foundation", {})
    if foundation.get("state") != "APPLIED":
        raise ValueError("Foundation has not reached an applied checkpoint")
    if not stages.get("gitops"):
        promoted = stages.get("chart")
        if promoted:
            validate_promotion_checkpoint(record)
            if previous != operation("promote", "chart"): raise ValueError("Promotion requires its exact completed manager")
        elif previous != operation("apply", "foundation", foundation["attempt"]):
            raise ValueError("Cold bootstrap requires the completed foundation apply")
        if not record.get("bootstrapRelease"): return dict(waiting="COLD_RELEASE")
        if not record.get("bootstrapProposal"): return dict(waiting="CHART_PROPOSAL")
        if not record.get("bootstrapChart"):
            if record.get("bootstrapPromotionApproval") and not promoted:
                validate_promotion(record, 1)
                return operation("promote", "chart")
            return dict(waiting="CHART_SELECTION")
        validate_bootstrap(record, 1)
        return operation("bootstrap", "gitops")
    validate_gitops_checkpoint(record)
    if not stages.get("edge"):
        if previous != operation("bootstrap", "gitops"):
            raise ValueError("Edge planning requires the completed GitOps bootstrap")
        return operation("plan", "edge")
    edge = stages["edge"]
    if edge.get("state") == "APPLIED":
        if not stages.get("exposure") and previous == operation("apply", "edge", edge["attempt"]):
            validate_activation(record, 1)
            return operation("activate", "exposure")
        if previous == operation("activate", "exposure"):
            validate_exposure_checkpoint(record)
            if not stages.get("publicAcceptance"): return dict(waiting="PUBLIC_ACCEPTANCE")
            from public_acceptance_contract import validate_public_checkpoint
            validate_public_checkpoint(record)
            from private_acceptance_contract import validate_private_start
            validate_private_start(record, 1)
            return operation("verify", "private")
    raise ValueError("No supported lifecycle continuation; cleanup/reconciliation required")


class Dispatcher:
    def __init__(self, clients, store, bucket, execution, version, *, clock=None):
        self.clients, self.store, self.bucket = clients, store, bucket
        self.execution, self.version = deepcopy(execution), version
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def current(self, *, idle=True, allow_ready=False, allow_rollback=False):
        if self.clients["sts"].get_caller_identity().get("Account") != ACCOUNT:
            raise ValueError("Wrong lifecycle dispatch account")
        record, etag = self.store.read()
        guard(record, self.execution, self.version, self.clock(), idle=idle, allow_ready=allow_ready, allow_rollback=allow_rollback)
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        return record, etag

    def cleanup(self):
        self.clients["lambda"].invoke(FunctionName=f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
            InvocationType="Event", Payload=encoded(dict(sessionId=self.execution["sessionId"], executionVersion=self.version, requestedEarly=True)))

    def start(self, checked, op, *, initial=False):
        """CAS consumes an already-verified predecessor before any StartBuild."""
        operation(**op)
        rollback_private = op == operation("verify", "rollback")
        rollback = op["mode"] == "rollback" or rollback_private
        current, etag = self.current(allow_ready=op["mode"] == "release", allow_rollback=rollback)
        if current != checked: raise ValueError("Session changed during dispatch admission")
        if current["status"] == "TESTING" and op != operation("verify", "active"): raise ValueError("An active task dispatch cannot be replayed")
        if initial:
            if current.get("lifecycleDispatch") or current.get("deploymentTask") or current.get("executorPrepared") or current.get("stages") or op != operation("prepare", "foundation"):
                raise ValueError("Initial dispatch is limited to unconsumed executor preparation")
        elif current.get("lifecycleDispatch", {}).get("phase") != "OBSERVED":
            raise ValueError("Previous dispatch is unknown; never replay StartBuild")
        else:
            if rollback:
                failed_predecessor = False
                from rollback_task_contract import original_record, proposal_record, validate_prepare_start, validate_rollout_start
                if rollback_private:
                    from rollback_private_contract import restoration_record, validate_private_start
                    from rollback_public_contract import read_public_receipt
                    validate_private_start(current, op["attempt"])
                    read_public_receipt(self.clients["s3"], self.bucket, current, self.version)
                    predecessor, required = restoration_record(current), operation("rollback", "rollout")
                elif op["stage"] == "prepare":
                    validate_prepare_start(current, op["attempt"])
                    predecessor, required = dict(original_record(current), status="TESTING"), operation("release", "active")
                    partial = bool(predecessor.get("releaseCandidate", {}).get("partialRollout"))
                    if partial:
                        from partial_rollout import failed_partial_terminal
                        failed_partial_terminal(self.clients, self.bucket, self.execution, predecessor, self.version, self.clock())
                        failed_predecessor = True
                    elif predecessor.get("lifecycleDispatch", {}).get("operation") == operation("verify", "active"):
                        from active_private_failure import failed_private_terminal
                        failed_private_terminal(self.clients, self.bucket, self.execution, predecessor, self.version, self.clock())
                        required = operation("verify", "active")
                        failed_predecessor = True
                else:
                    validate_rollout_start(current, op["attempt"])
                    predecessor, required = proposal_record(current), operation("rollback", "prepare")
                previous = required if failed_predecessor else completed_manager(self.clients, self.execution, predecessor)
                if previous != required: raise ValueError("Rollback requires its exact terminal private predecessor")
            else: previous = completed_manager(self.clients, self.execution, current)
            if rollback:
                pass  # Exact predecessor and single-use reservation checked above.
            elif op["mode"] == "release":
                if current["status"] != "READY" or previous not in (operation("verify", "private"), operation("verify", "active"), operation("verify", "rollback")):
                    raise ValueError("Private rollout requires the accepted READY predecessor")
                self.advance()
                from active_release_contract import admitted_release
                admitted_release(self.clients["s3"], self.bucket, current, self.execution, self.version, self.clock())
            elif op == operation("verify", "active"):
                from active_public_contract import read_active_public_receipt
                from active_private_contract import validate_active_private_start
                if previous != operation("release", "active") or current.get("workflowHandoff", {}).get("phase") != "CONSUMED":
                    raise ValueError("Private active checks require the completed rollout and consumed public handoff")
                read_active_public_receipt(self.clients["s3"], self.bucket, current, self.version)
                validate_active_private_start(current, 1)
            elif op["mode"] == "apply":
                ref = current.get("pendingPlan", {})
                if (previous != operation("plan", op["stage"], op["attempt"])
                        or ref.get("stage") != op["stage"] or ref.get("attempt") != op["attempt"]):
                    raise ValueError("Apply requires its completed planning predecessor")
            elif next_operation(current, previous) != op:
                raise ValueError("Operation is not the next supported lifecycle edge")
        dispatch_id = dispatch_identity(current, self.version, op)
        updated = deepcopy(current)
        if op["mode"] == "release": updated["status"] = "TESTING"
        updated["lifecycleDispatch"] = dict(id=dispatch_id, operation=op, phase="PREPARED", createdAt=self.clock().isoformat())
        try:
            self.store.write(updated, etag)
        except BaseException:
            # A lost CAS response may have consumed this edge. A competing
            # dispatch's conflict must not cancel that other admitted build.
            latest, _ = self.store.read()
            if latest.get("lifecycleDispatch", {}).get("id") == dispatch_id:
                self.cleanup()
            raise
        # Once this call intent exists, no retry (including a workflow rerun
        # days later, after the AWS idempotency token expires) may resend it.
        try:
            current, etag = self.current(allow_rollback=rollback)
            if current.get("lifecycleDispatch") != updated["lifecycleDispatch"]:
                raise ValueError("Dispatch preparation was superseded")
            current["lifecycleDispatch"]["phase"] = "CALLING"
            self.store.write(current, etag)
            latest, _ = self.current(allow_rollback=rollback)
            if latest != current: raise ValueError("Session changed before manager API dispatch")
            result = self.clients["codebuild"].start_build(projectName=PROJECT, idempotencyToken=dispatch_id,
                environmentVariablesOverride=[dict(name=key, value=value, type="PLAINTEXT") for key, value in environment(current, op).items()])
            build = result.get("build", {})
            build_id = build.get("id", "")
            if (not re.fullmatch(BUILD, build_id) or build.get("arn") != f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/{build_id}"
                    or build.get("projectName") != PROJECT):
                raise ValueError("StartBuild response does not identify the exact lifecycle project")
            for attempt in range(4):
                current, etag = self.current(idle=False, allow_rollback=rollback)
                ref = current.get("lifecycleDispatch", {})
                if ref.get("id") != dispatch_id or ref.get("phase") not in ("CALLING", "OBSERVED"):
                    raise ValueError("Manager dispatch was superseded")
                if ref.get("phase") == "OBSERVED":
                    if ref.get("buildId") != build_id: raise ValueError("Dispatch already refers to another build")
                    break
                current["lifecycleDispatch"].update(phase="OBSERVED", buildId=build_id)
                try:
                    self.store.write(current, etag)
                    break
                except Exception:
                    if attempt == 3: raise
            return dict(buildId=build_id, sessionId=current["sessionId"], executionVersion=self.version, **op)
        except BaseException:
            self.cleanup()
            raise

    def advance(self):
        record, _ = self.current(allow_ready=True)
        previous = completed_manager(self.clients, self.execution, record)
        op = next_operation(record, previous)
        if op.get("waiting") == "READY":
            from ready_release_contract import accepted_baseline
            accepted = accepted_baseline(self.clients["s3"], self.bucket, record, self.version)
            return dict(sessionId=record["sessionId"], waiting="READY", acceptedRelease=accepted)
        if op.get("waiting") == "PUBLIC_ACCEPTANCE":
            from edge_activation import read_activation_receipt
            read_activation_receipt(self.clients["s3"], self.bucket, record, self.version)
        if op.get("waiting") == "ACTIVE_PUBLIC_ACCEPTANCE":
            from active_release_contract import read_rollout_receipt
            read_rollout_receipt(self.clients["s3"], self.bucket, record, self.version)
        if op == operation("verify", "active"):
            from active_public_contract import read_active_public_receipt
            read_active_public_receipt(self.clients["s3"], self.bucket, record, self.version)
        if op == operation("verify", "private"):
            from edge_activation import read_activation_receipt
            from public_acceptance_contract import read_public_receipt
            read_activation_receipt(self.clients["s3"], self.bucket, record, self.version)
            read_public_receipt(self.clients["s3"], self.bucket, record, self.version)
        if op.get("waiting"): return dict(sessionId=record["sessionId"], **op)
        if op["mode"] == "bootstrap":
            from chart_selection import read_release, read_selection
            read_release(self.clients["s3"], self.bucket, record, self.execution, self.version)
            read_selection(self.clients["s3"], self.bucket, record, self.execution, self.version)
        return self.start(record, op)
