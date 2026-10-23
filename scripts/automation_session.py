"""S3 compare-and-swap staging session and lease primitives.

The state backend lock protects Terraform only. This lease serializes the wider
release/DNS/Kubernetes lifecycle, including requests originating in other repos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile

TRANSITIONS = {
    "OFF": {"PLANNING"},
    "PLANNING": {"PROVISIONING", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
    "PROVISIONING": {"READY", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
    "READY": {"TESTING", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
    "TESTING": {"READY", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
    "FAILED_NEEDS_CLEANUP": {"DESTROYING"},
    "DESTROYING": {"DESTROYED", "FAILED_NEEDS_CLEANUP"},
    "DESTROYED": {"PLANNING"},
}


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(timezone.utc)


def validate(record: dict):
    if record.get("account") != "058114477594" or record.get("region") != "us-east-1" or record.get("environment") != "staging":
        raise ValueError("Invalid staging scope")
    if record.get("status") not in TRANSITIONS:
        raise ValueError("Unknown lifecycle status")
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", record.get("sessionId", "")):
        raise ValueError("Invalid session ID")
    if utc(record["cleanupStartAt"]) >= utc(record["deadline"]):
        raise ValueError("Cleanup must begin before the deadline")


def claim(record: dict, owner: str, operation: str, now=None, ttl_seconds=300, previous_owner_stopped=False):
    validate(record)
    now = now or datetime.now(timezone.utc)
    if operation not in ("deploy", "release", "acceptance", "cleanup"):
        raise ValueError("Unknown lease operation")
    if not re.fullmatch(r"[a-zA-Z0-9:/_.-]{1,160}", owner):
        raise ValueError("Invalid lease owner")
    if not 30 <= ttl_seconds <= 600:
        raise ValueError("Lease TTL outside policy")
    lease = record.get("lease")
    # Repeated invocation must not let the same owner change operations mid-flight.
    if lease and utc(lease["expiresAt"]) > now and (lease["owner"] != owner or lease["operation"] != operation):
        raise ValueError("Session is busy")
    if lease and lease["owner"] != owner and utc(lease["expiresAt"]) <= now and not previous_owner_stopped:
        raise ValueError("Verify the previous executor is stopped before taking over an expired lease")
    if operation != "cleanup" and now >= utc(record["cleanupStartAt"]):
        raise ValueError("Cleanup window has started")
    if operation in ("release", "acceptance") and record["status"] != "READY":
        raise ValueError("Application operations require READY")
    if operation == "deploy" and record["status"] not in ("PLANNING", "PROVISIONING"):
        raise ValueError("Deploy cannot recreate an active environment")
    if operation == "cleanup" and record["status"] in ("OFF", "DESTROYED"):
        raise ValueError("No active session to clean up")
    result = dict(record)
    epoch = record.get("leaseEpoch", (lease or {}).get("epoch", 0)) + (0 if lease and lease["owner"] == owner else 1)
    result["leaseEpoch"] = epoch
    result["lease"] = dict(owner=owner, operation=operation, epoch=epoch, expiresAt=(now + timedelta(seconds=ttl_seconds)).isoformat())
    return result


def release(record: dict, owner: str, epoch: int):
    validate(record)
    lease = record.get("lease", {})
    if lease.get("owner") != owner or lease.get("epoch") != epoch:
        raise ValueError("Lease ownership changed; release refused")
    result = dict(record)
    result.pop("lease", None)
    return result


def transition(record: dict, owner: str, status: str, now=None):
    validate(record)
    now = now or datetime.now(timezone.utc)
    lease = record.get("lease", {})
    if lease.get("owner") != owner or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= now:
        raise ValueError("Valid owned lease required")
    if status not in TRANSITIONS[record["status"]]:
        raise ValueError("Invalid lifecycle transition")
    operation = lease["operation"]
    allowed = {
        "deploy": {"PROVISIONING", "READY", "FAILED_NEEDS_CLEANUP"},
        "release": {"FAILED_NEEDS_CLEANUP"},
        "acceptance": {"TESTING", "READY", "FAILED_NEEDS_CLEANUP"},
        "cleanup": {"DESTROYING", "DESTROYED", "FAILED_NEEDS_CLEANUP"},
    }
    if status not in allowed[operation]:
        raise ValueError("Lease operation cannot perform that transition")
    result = dict(record, status=status)
    if status == "DESTROYED":
        # A cleanup caller must provide inventory evidence, not just its intent.
        evidence = record.get("cleanupEvidence", {})
        if evidence.get("inventoryEmpty") is not True or not evidence.get("artifactSha256") or not re.fullmatch(r"[a-f0-9]{64}", evidence["artifactSha256"]):
            raise ValueError("Verified inventory evidence required before DESTROYED")
        result.pop("lease", None)
    return result


class S3SessionStore:
    """Caller handles a conditional-write conflict by rereading, never blind retry."""
    def __init__(self, bucket: str, key="automation/staging/session.json", run=subprocess.run):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid session bucket")
        if key != "automation/staging/session.json":
            raise ValueError("Unexpected session key")
        self.bucket, self.key, self.run = bucket, key, run

    def read(self):
        with tempfile.TemporaryDirectory(prefix="techx-session-") as directory:
            path = Path(directory) / "session.json"
            response = self.run(["aws", "s3api", "get-object", "--bucket", self.bucket, "--key", self.key,
                                 "--expected-bucket-owner", "058114477594", str(path), "--output", "json"],
                                check=True, capture_output=True, text=True)
            record = json.loads(path.read_text(encoding="utf-8"))
            validate(record)
            return record, json.loads(response.stdout)["ETag"]

    def write(self, record, etag=None):
        validate(record)
        with tempfile.TemporaryDirectory(prefix="techx-session-") as directory:
            path = Path(directory) / "session.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            condition = ["--if-match", etag] if etag else ["--if-none-match", "*"]
            response = self.run(["aws", "s3api", "put-object", "--bucket", self.bucket, "--key", self.key,
                                 "--expected-bucket-owner", "058114477594", "--body", str(path),
                                 "--content-type", "application/json", *condition, "--output", "json"],
                                check=True, capture_output=True, text=True)
            return json.loads(response.stdout)["ETag"]
