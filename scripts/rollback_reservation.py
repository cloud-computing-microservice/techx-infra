"""CAS reservation of one positively admitted rollback attempt, without dispatch.

The private executor still has to persist the fixed single-use intent and pass
task/lease/approval gates before any Git or workload mutation is permitted.
"""
from copy import deepcopy
from datetime import timedelta
import re
from types import SimpleNamespace

from automation_session import utc
from chart_selection import INFRA_REPO
from plan_review_store import ACCOUNT, checksum
from rollback_admission import preview_rollback
from rollback_proposal import validate_intent

CONTROLLERS = {"staging-orchestrate.yml", "staging-continue.yml", "staging-lifecycle.yml"}


def controller(clients, github, execution, run_id, now):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("Actual protected controller run required")
    identity = clients["sts"].get_caller_identity()
    if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
            f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-{run_id}"):
        raise ValueError("Rollback reservation requires this controller's scoped dispatcher identity")
    run = github.get(f"actions/runs/{run_id}")
    if (type(run.get("id")) is not int or run["id"] != int(run_id) or run.get("status") != "in_progress" or run.get("head_branch") != "main"
            or run.get("head_sha") != execution["sourceRevisions"]["techx-infra"]
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("event") not in ("workflow_dispatch", "workflow_run")
            or run.get("path") not in {f".github/workflows/{name}{suffix}" for name in CONTROLLERS for suffix in ("", "@main")}
            or not utc(execution["createdAt"]) - timedelta(minutes=5) <= utc(run.get("created_at", "")) <= now < utc(execution["cleanupStartAt"])
            or any(run.get(name, {}).get("full_name") != INFRA_REPO for name in ("repository", "head_repository"))):
        raise ValueError("Rollback controller must be an actual admitted first-attempt main workflow")


def reservation(record):
    slot = record.get("rollbackAttempt", {})
    if (not isinstance(slot, dict) or set(slot) != {"schemaVersion", "phase", "attempt", "intent", "contextSha256", "origin"}
            or type(slot.get("schemaVersion")) is not int or slot["schemaVersion"] != 1
            or type(slot.get("attempt")) is not int or slot["attempt"] != 1 or slot.get("phase") != "REQUESTED"
            or not isinstance(slot.get("origin"), dict) or set(slot["origin"]) != {"lifecycleDispatch", "deploymentTask", "workflowHandoff"}
            or checksum(dict(intent=slot.get("intent"), origin=slot.get("origin"))) != slot.get("contextSha256")):
        raise ValueError("Rollback attempt is already consumed or its reservation changed")
    intent = validate_intent(slot["intent"])
    if (intent["sessionId"] != record.get("sessionId") or intent["executionVersion"] != record.get("executionVersion")
            or intent["failedReleaseId"] != record.get("releaseCandidate", {}).get("id")
            or intent["acceptedBaseline"] != record.get("acceptedRelease")
            or any(record.get(key) != value for key, value in slot["origin"].items())):
        raise ValueError("Rollback reservation belongs to another candidate or baseline")
    return slot


def arm_rollback(clients, bucket, store, execution, execution_version, github, run_id, *, clock, source=None):
    controller(clients, github, execution, run_id, clock())
    record, etag = store.read()
    existing = record.get("rollbackAttempt")
    if existing:
        slot = reservation(record)
        # Only the original failed predecessor is projected for read-only
        # revalidation. This cannot authorize replay of an executor/merge task.
        def original():
            current, current_etag = store.read()
            checked = reservation(current)
            projected = deepcopy(current)
            projected.update(checked["origin"])
            projected.pop("rollbackAttempt")
            return projected, current_etag
        value = preview_rollback(clients, bucket, SimpleNamespace(read=original), execution, execution_version,
            github, clock=clock, created_at=slot["intent"]["createdAt"], source=source)
        latest, latest_etag = store.read()
        if value != slot["intent"] or latest != record or latest_etag != etag:
            raise ValueError("Rollback reservation changed during positive recovery")
        return deepcopy(slot)
    value = preview_rollback(clients, bucket, store, execution, execution_version, github, clock=clock, source=source)
    latest, latest_etag = store.read()
    if latest != record or latest_etag != etag: raise ValueError("Session changed before rollback reservation CAS")
    origin = {key: deepcopy(record.get(key)) for key in ("lifecycleDispatch", "deploymentTask", "workflowHandoff")}
    slot = dict(schemaVersion=1, phase="REQUESTED", attempt=1, intent=value, origin=origin,
        contextSha256=checksum(dict(intent=value, origin=origin)))
    updated = deepcopy(record)
    updated["rollbackAttempt"] = slot
    store.write(updated, etag)
    return deepcopy(slot)


def arm_public_rollback(*args, **kwargs):
    """Retain the source restriction for callers of the old public-only API."""
    return arm_rollback(*args, **kwargs, source="public")
