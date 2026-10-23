"""Reserve a bounded staging session and arm cleanup before starting paid work."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

from automation_inputs import FIXED, validate_variables
from automation_session import validate
from cleanup_execution import assert_inventory
from cleanup_schedule import register

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


def initial_record(session_id, sources, hours, email, action, now=None):
    now = now or datetime.now(timezone.utc)
    if not re.fullmatch(r"staging-[a-zA-Z0-9-]{1,64}", session_id): raise ValueError("Invalid staging session ID")
    if set(sources) != {"techx-infra", "techx-chart", "techx-platform"} or any(not re.fullmatch(r"[a-f0-9]{40}", sha) for sha in sources.values()):
        raise ValueError("Exactly three full source revisions are required")
    if type(hours) is not int or not 3 <= hours <= 24: raise ValueError("Session window must be between 3 and 24 hours")
    if action not in ("deploy-test-destroy", "deploy-window"): raise ValueError("Unsupported lifecycle action")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email): raise ValueError("Budget alert email is invalid")
    deadline = now + timedelta(hours=hours)
    return dict(sessionId=session_id, account=ACCOUNT, region="us-east-1", environment="staging", status="PLANNING",
                sourceRevisions=sources, deadline=deadline.isoformat(), cleanupStartAt=(deadline - timedelta(minutes=90)).isoformat(),
                budgetAlertEmail=email, action=action, createdAt=now.isoformat(), maximumHours=hours)


class Coordinator:
    def __init__(self, clients, store, bucket):
        self.clients, self.store, self.bucket = clients, store, bucket

    def start(self, record, bundle_path: Path, inventory, estimate, caller_ip, now=None):
        validate(record)
        assert_inventory(inventory)
        bound = estimate.get("upperBoundUsd")
        if (type(bound) not in (int, float) or not math.isfinite(bound) or not 0 <= bound <= 60
                or estimate.get("includesAutomation") is not True
                or estimate.get("approvedBudgetUsd") != 80 or estimate.get("maximumHours") != record["maximumHours"]):
            raise ValueError("Cost estimate does not cover this automation session")
        if self.clients["sts"].get_caller_identity()["Account"] != ACCOUNT: raise ValueError("Wrong deployment account")
        projects = self.clients["codebuild"].batch_get_projects(names=[PROJECT, "techx-automation-cleanup"])
        if projects.get("projectsNotFound") or {p["name"] for p in projects.get("projects", [])} != {PROJECT, "techx-automation-cleanup"}:
            raise ValueError("Both lifecycle and independent cleanup executors must exist")
        if self.clients["s3"].get_bucket_versioning(Bucket=self.bucket, ExpectedBucketOwner=ACCOUNT).get("Status") != "Enabled":
            raise ValueError("Control bucket versioning is not enabled")
        try:
            response = self.clients["s3"].get_object(Bucket=self.bucket, Key="automation/staging/session.json", ExpectedBucketOwner=ACCOUNT)
            current, etag = json.loads(response["Body"].read()), response["ETag"]
            if current.get("status") not in ("OFF", "DESTROYED"):
                raise ValueError("An existing staging session must be cleaned up before another starts")
        except self.clients["s3"].exceptions.NoSuchKey:
            etag = None
        self.store.write(record, etag)  # CAS reservation; publishers cannot claim PLANNING sessions.
        armed = False
        try:
            payload = bundle_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            key = f"bundles/{digest}.tar.gz"
            bundle_version = immutable_put(self.clients["s3"], self.bucket, key, payload)
            execution = dict(record, cleanupBundle=dict(key=key, sha256=digest, versionId=bundle_version), costEstimate=estimate)
            execution_version = immutable_put(self.clients["s3"], self.bucket, f"executions/{record['sessionId']}.json", encoded(execution))
            variables = dict(FIXED, public_access_cidrs=[f"{caller_ip}/32"], budget_alert_email=record["budgetAlertEmail"],
                             enable_domain_vpn_foundation=False, enable_domain_vpn_edge=False)
            validate_variables(variables)
            private = dict(schemaVersion=1, sessionId=record["sessionId"], terraformVars=variables,
                           dnsMutationAttempted=False, validationDnsPlans=[])
            immutable_put(self.clients["s3"], self.bucket, f"private/staging/{record['sessionId']}/inputs.json", encoded(private))
            schedule_arn = register(self.clients["scheduler"], record, execution_version, LAUNCHER,
                                    f"arn:aws:iam::{ACCOUNT}:role/techx-automation-scheduler",
                                    f"arn:aws:sqs:us-east-1:{ACCOUNT}:techx-automation-cleanup-dlq", now)
            pointer = dict(sessionId=record["sessionId"], executionVersion=execution_version)
            self.clients["s3"].put_object(Bucket=self.bucket, Key="control/staging/current-execution.json",
                                          Body=encoded(pointer), ExpectedBucketOwner=ACCOUNT)
            armed = True
            current, etag = self.store.read()
            if current["sessionId"] != record["sessionId"] or current["status"] != "PLANNING": raise ValueError("Session reservation was lost")
            self.store.write(dict(current, executionVersion=execution_version, cleanupScheduleArn=schedule_arn), etag)
            result = self.clients["codebuild"].start_build(projectName=PROJECT,
                idempotencyToken=hashlib.sha256(encoded(pointer)).hexdigest(),
                environmentVariablesOverride=[dict(name="TECHX_SESSION_ID", value=record["sessionId"], type="PLAINTEXT"),
                                              dict(name="TECHX_EXECUTION_VERSION", value=execution_version, type="PLAINTEXT")])
            return dict(pointer, buildId=result["build"]["id"], deadline=record["deadline"], cleanupScheduleArn=schedule_arn)
        except BaseException:
            if armed:
                # StartBuild may have succeeded despite a lost response. Never reopen
                # the session; AWS cleanup can fence/stop the actual executor.
                try:
                    self.clients["lambda"].invoke(FunctionName=LAUNCHER, InvocationType="Event",
                        Payload=encoded(dict(pointer, requestedEarly=True)))
                except Exception:
                    # The verified one-time schedule and AWS watchdog remain armed.
                    # Preserve the original startup error for the caller to diagnose.
                    print("Immediate cleanup dispatch failed; the verified AWS schedule/watchdog remains armed.", file=sys.stderr)
            else:
                current, etag = self.store.read()
                if current.get("sessionId") == record["sessionId"] and current.get("status") == "PLANNING" and not current.get("lease"):
                    self.store.write(dict(current, status="OFF", startFailureBeforeWorkload=True), etag)
            raise
