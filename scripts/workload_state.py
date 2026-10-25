"""Initialize a genuinely absent staging state once, before workload creation.

Never migrate or overwrite existing state. Admission has already reserved the
session and verified an empty inventory; only an If-None-Match write is allowed.
Terraform itself must subsequently read this state via its configured backend.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import uuid

from cleanup_execution import assert_inventory

ACCOUNT = "058114477594"
KEY = "staging/terraform.tfstate"
MAX_STATE_BYTES = 16 * 1024 * 1024


def validate_state(value):
    if (not isinstance(value, dict) or value.get("version") != 4
            or type(value.get("serial")) is not int or value["serial"] < 0
            or not isinstance(value.get("resources"), list)):
        raise ValueError("Missing or invalid observed Terraform state")
    if not isinstance(value.get("lineage"), str) or str(uuid.UUID(value["lineage"])) != value["lineage"]:
        raise ValueError("Invalid Terraform state lineage")
    return value


def read_state(client, bucket):
    response = client.get_object(Bucket=bucket, Key=KEY, ExpectedBucketOwner=ACCOUNT)
    body = response["Body"].read(MAX_STATE_BYTES + 1)
    if len(body) > MAX_STATE_BYTES:
        raise ValueError("Observed state exceeds the staging size limit")
    return validate_state(json.loads(body))


@contextmanager
def initialization_lock(client, bucket):
    """Use the same conditional S3 lock key as Terraform, never force-unlock."""
    lock = dict(ID=str(uuid.uuid4()), Operation="techx-state-admission", Info="Initialize absent staging state only",
                Who="techx-automation-coordinator", Version="1.15.5", Created=datetime.now(timezone.utc).isoformat(),
                Path=f"{bucket}/{KEY}")
    payload = json.dumps(lock, sort_keys=True).encode()
    try:
        client.put_object(Bucket=bucket, Key=KEY + ".tflock", ExpectedBucketOwner=ACCOUNT,
                          IfNoneMatch="*", Body=payload, ContentType="application/json")
    except Exception as error:
        if getattr(error, "response", {}).get("Error", {}).get("Code") == "PreconditionFailed":
            raise ValueError("Terraform backend is locked; state initialization refused") from error
        raise  # Unknown/ambiguous AWS writes must not be treated as acquired locks.
    response = client.get_object(Bucket=bucket, Key=KEY + ".tflock", ExpectedBucketOwner=ACCOUNT)
    if response["Body"].read(len(payload) + 1) != payload or not response.get("ETag"):
        raise ValueError("State initialization lock ownership could not be confirmed")
    try:
        yield
    finally:
        # If somebody force-replaced the lock, IfMatch refuses to delete theirs.
        client.delete_object(Bucket=bucket, Key=KEY + ".tflock", ExpectedBucketOwner=ACCOUNT, IfMatch=response["ETag"])


def ensure_empty_state(client, bucket, inventory):
    assert_inventory(inventory)
    with initialization_lock(client, bucket):
        return _ensure_empty_state(client, bucket)


def _ensure_empty_state(client, bucket):
    try:
        state = read_state(client, bucket)
    except client.exceptions.NoSuchKey:
        state = dict(version=4, terraform_version="1.15.5", serial=0, lineage=str(uuid.uuid4()), outputs={}, resources=[], check_results=None)
        try:
            client.put_object(Bucket=bucket, Key=KEY, ExpectedBucketOwner=ACCOUNT,
                              IfNoneMatch="*", Body=json.dumps(state).encode(), ContentType="application/json")
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed":
                raise
            # A competing write is observed, never overwritten or force-pushed.
            state = read_state(client, bucket)
    if any(not isinstance(resource, dict) or resource.get("mode") != "data" for resource in state["resources"]):
        raise ValueError("Existing state still owns managed/unknown resources")
    observed = read_state(client, bucket)
    if observed != state:
        raise ValueError("State changed during admission; initialization refused")
    return dict(lineage=state["lineage"], serial=state["serial"])
