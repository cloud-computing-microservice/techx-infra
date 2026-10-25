"""Reserve a bounded staging session and arm cleanup before starting paid work."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from automation_inputs import FIXED, validate_variables
from automation_budget import approved_budget, validate_estimate
from automation_session import utc, validate
from cleanup_execution import assert_inventory
from cleanup_schedule import register, verify_existing
from deployment_tasks import executor_config
from lifecycle_dispatch import Dispatcher, operation
from plan_approval import context_for_session, issue_approval
from plan_approval import version as validate_version
from plan_review_store import approval_key, checksum, load_review, read_json
from workload_state import ensure_empty_state

ACCOUNT = "058114477594"
PROJECT = "techx-automation-lifecycle"
LAUNCHER = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher"


def encoded(record): return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


def immutable_put(s3, bucket, key, body):
    try:
        response = s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ExpectedBucketOwner=ACCOUNT)
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed": raise
        response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
        if response["Body"].read(len(body) + 1) != body: raise ValueError("An immutable execution object already has different content")
    version = response.get("VersionId")
    if not version or version == "null": raise ValueError("Versioned control storage is required")
    return version


def initial_record(session_id, sources, hours, email, action, now=None, *, approved_budget_usd):
    now = now or datetime.now(timezone.utc)
    if not re.fullmatch(r"staging-[a-zA-Z0-9-]{1,64}", session_id): raise ValueError("Invalid staging session ID")
    if set(sources) != {"techx-infra", "techx-chart", "techx-platform"} or any(not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in sources.values()):
        raise ValueError("Exactly three full source revisions are required")
    if type(hours) is not int or not 3 <= hours <= 24: raise ValueError("Session window must be between 3 and 24 hours")
    if action not in ("deploy-test-destroy", "deploy-window"): raise ValueError("Unsupported lifecycle action")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email): raise ValueError("Budget alert email is invalid")
    budget = approved_budget(approved_budget_usd)
    deadline = now + timedelta(hours=hours)
    return dict(sessionId=session_id, account=ACCOUNT, region="us-east-1", environment="staging", status="PLANNING",
                sourceRevisions=sources, deadline=deadline.isoformat(), cleanupStartAt=(deadline - timedelta(minutes=90)).isoformat(),
                budgetAlertEmail=email, approvedBudgetUsd=budget, action=action, createdAt=now.isoformat(), maximumHours=hours)


class Coordinator:
    def __init__(self, clients, store, bucket):
        self.clients, self.store, self.bucket = clients, store, bucket

    def start(self, record, bundle_path: Path, inventory, estimate, caller_ip, now=None):
        validate(record)
        assert_inventory(inventory)
        budget = validate_estimate(record, estimate)
        if self.clients["sts"].get_caller_identity()["Account"] != ACCOUNT: raise ValueError("Wrong deployment account")
        projects = self.clients["codebuild"].batch_get_projects(names=[PROJECT, "techx-automation-cleanup"])
        if projects.get("projectsNotFound") or {p["name"] for p in projects.get("projects", [])} != {PROJECT, "techx-automation-cleanup"}:
            raise ValueError("Both lifecycle and independent cleanup executors must exist")
        if self.clients["s3"].get_bucket_versioning(Bucket=self.bucket, ExpectedBucketOwner=ACCOUNT).get("Status") != "Enabled":
            raise ValueError("Control bucket versioning is not enabled")
        config_key = "control/staging/executor-config.json"
        config, config_version = read_json(self.clients["s3"], self.bucket, config_key)
        executor_config(config)
        if not config_version or config_version == "null": raise ValueError("Executor configuration must be versioned")
        try:
            response = self.clients["s3"].get_object(Bucket=self.bucket, Key="automation/staging/session.json", ExpectedBucketOwner=ACCOUNT)
            current, etag = json.loads(response["Body"].read()), response["ETag"]
            if current.get("status") not in ("OFF", "DESTROYED"):
                raise ValueError("An existing staging session must be cleaned up before another starts")
        except self.clients["s3"].exceptions.NoSuchKey:
            etag = None
        self.store.write(record, etag)  # CAS reservation; publishers cannot claim PLANNING sessions.
        recovery_prepared = False
        try:
            state_identity = ensure_empty_state(self.clients["s3"], self.bucket, inventory)
            payload = bundle_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            key = f"bundles/{digest}.tar.gz"
            bundle_version = immutable_put(self.clients["s3"], self.bucket, key, payload)
            execution = dict(record, cleanupBundle=dict(key=key, sha256=digest, versionId=bundle_version), costEstimate=estimate,
                             initialState=state_identity, deploymentConfig=config,
                             deploymentConfigReference=dict(key=config_key, versionId=config_version, sha256=checksum(config)))
            execution_version = immutable_put(self.clients["s3"], self.bucket, f"executions/{record['sessionId']}.json", encoded(execution))
            variables = dict(FIXED, public_access_cidrs=[f"{caller_ip}/32"], budget_alert_email=record["budgetAlertEmail"],
                             budget_limit_usd=budget,
                             enable_domain_vpn_foundation=False, enable_domain_vpn_edge=False)
            validate_variables(variables)
            private = dict(schemaVersion=1, sessionId=record["sessionId"], terraformVars=variables,
                           dnsMutationAttempted=False, validationDnsPlans=[])
            immutable_put(self.clients["s3"], self.bucket, f"private/staging/{record['sessionId']}/inputs.json", encoded(private))
            pointer = dict(sessionId=record["sessionId"], executionVersion=execution_version)
            current, etag = self.store.read()
            if current["sessionId"] != record["sessionId"] or current["status"] != "PLANNING" or current.get("lease"):
                raise ValueError("Session reservation was lost before cleanup preparation")
            self.store.write(dict(current, executionVersion=execution_version), etag)
            recovery_prepared = True
            # Publish complete recoverable inputs before attempting a schedule.
            # A lost CreateSchedule response must not reopen OFF or leave a
            # scheduled cleanup without the current execution VersionId.
            self.clients["s3"].put_object(Bucket=self.bucket, Key="control/staging/current-execution.json",
                                          Body=encoded(pointer), ExpectedBucketOwner=ACCOUNT)
            schedule_arn = register(self.clients["scheduler"], record, execution_version, LAUNCHER,
                                    f"arn:aws:iam::{ACCOUNT}:role/techx-automation-scheduler",
                                    f"arn:aws:sqs:us-east-1:{ACCOUNT}:techx-automation-cleanup-dlq", now)
            current, etag = self.store.read()
            if current["sessionId"] != record["sessionId"] or current["status"] != "PLANNING": raise ValueError("Session reservation was lost")
            checked = dict(current, executionVersion=execution_version, cleanupScheduleArn=schedule_arn)
            self.store.write(checked, etag)
            result = Dispatcher(self.clients, self.store, self.bucket, execution, execution_version,
                                clock=lambda: now or datetime.now(timezone.utc)).start(checked, operation("prepare", "foundation"), initial=True)
            return dict(pointer, buildId=result["buildId"], deadline=record["deadline"], cleanupScheduleArn=schedule_arn)
        except BaseException:
            if recovery_prepared:
                # StartBuild may have succeeded despite a lost response. Never reopen
                # the session; AWS cleanup can fence/stop the actual executor.
                try:
                    current, etag = self.store.read()
                    if current.get("sessionId") == record["sessionId"] and current.get("status") == "PLANNING" and not current.get("lease"):
                        self.store.write(dict(current, status="FAILED_NEEDS_CLEANUP"), etag)
                except Exception:
                    pass  # Cleanup owns fencing; a CAS conflict must not suppress its dispatch.
                try:
                    self.clients["lambda"].invoke(FunctionName=LAUNCHER, InvocationType="Event",
                        Payload=encoded(dict(pointer, requestedEarly=True)))
                except Exception:
                    # A schedule may have been created despite a lost response.
                    # Keep complete inputs/current pointer and the original error.
                    print("Immediate cleanup dispatch failed; preserve the failed session and reconcile its AWS schedule/watchdog.", file=sys.stderr)
            else:
                current, etag = self.store.read()
                if current.get("sessionId") == record["sessionId"] and current.get("status") == "PLANNING" and not current.get("lease"):
                    self.store.write(dict(current, status="OFF", startFailureBeforeWorkload=True), etag)
            raise

    def resume_approved(self, session_id, now=None):
        """Dispatch apply after bridge approval; actual context is re-observed on EC2.

        This does not authorize Terraform from a copied plan context or claim a
        workload lease on a hosted runner. The manager/worker repeats guards.
        """
        clock = (lambda: now) if now is not None else None
        now = now or datetime.now(timezone.utc)
        if self.clients["sts"].get_caller_identity().get("Account") != ACCOUNT:
            raise ValueError("Wrong deployment account")
        record, _ = self.store.read()
        validate(record)
        if (record.get("sessionId") != session_id or record.get("status") != "PROVISIONING" or record.get("lease")
                or now >= utc(record["cleanupStartAt"])):
            raise ValueError("Session is not idle and open for approved resume")
        verify_existing(self.clients["scheduler"], record, record.get("executionVersion"), now)
        ref = record.get("pendingPlan")
        if not isinstance(ref, dict): raise ValueError("There is no pending review to resume")
        checkpoint = record.get("stages", {}).get(ref["stage"], {})
        if checkpoint.get("state") != "AWAITING_APPROVAL" or checkpoint.get("attempt") != ref["attempt"] or checkpoint.get("reviewReference") != ref:
            raise ValueError("Stage is not awaiting this exact reviewed attempt")
        envelope = load_review(self.clients["s3"], self.bucket, ref, now)
        request = envelope["request"]
        context_for_session(record, request["context"], now)
        validate_estimate(record, envelope["costEstimate"])
        approval, _ = read_json(self.clients["s3"], self.bucket, approval_key(request, ref["sha256"]))
        if (approval.get("request") != request or approval.get("reviewReference") != ref or approval.get("schemaVersion") != 1
                or not utc(request["createdAt"]) <= utc(approval["approvedAt"]) <= now):
            raise ValueError("Approval does not match the current immutable review")
        issue_approval(request, reviewer=approval.get("reviewer"), review_run=approval.get("reviewRun"), now=now)
        execution, _ = read_json(self.clients["s3"], self.bucket, f"executions/{session_id}.json", record["executionVersion"])
        for key in ("sessionId", "sourceRevisions", "approvedBudgetUsd", "deadline", "cleanupStartAt", "maximumHours", "action"):
            if execution.get(key) != record.get(key): raise ValueError("Session differs from immutable execution")
        # A real SSM terminal observation is required even if the lease is gone.
        # The manager will verify document/instance/config and this result again.
        from deployment_manager import previous_terminal
        task = record.get("deploymentTask", {})
        if (task.get("mode") != "plan" or task.get("stage") != ref["stage"] or task.get("attempt") != ref["attempt"]):
            raise ValueError("The approved review has no matching completed SSM planning task")
        previous_terminal(self.clients["ssm"], executor_config(execution["deploymentConfig"]), record)
        current, _ = self.store.read()
        if current != record: raise ValueError("Session changed during approval resume checks")
        return Dispatcher(self.clients, self.store, self.bucket, execution, record["executionVersion"],
                          clock=clock).start(record, operation("apply", ref["stage"], ref["attempt"]))

    def advance(self, session_id, now=None):
        record, _ = self.store.read()
        if record.get("sessionId") != session_id: raise ValueError("Stale lifecycle continuation")
        validate_version(record.get("executionVersion"))
        execution, _ = read_json(self.clients["s3"], self.bucket, f"executions/{session_id}.json", record["executionVersion"])
        return Dispatcher(self.clients, self.store, self.bucket, execution, record["executionVersion"],
                          clock=(lambda: now) if now else None).advance()
