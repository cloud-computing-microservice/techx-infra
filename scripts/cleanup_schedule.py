"""Idempotently register a one-time independent AWS cleanup deadline."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from automation_session import utc, validate


def definition(record, execution_version, launcher_arn, role_arn, dlq_arn, now=None):
    validate(record)
    now = now or datetime.now(timezone.utc)
    when = utc(record["cleanupStartAt"])
    if when <= now or utc(record["deadline"]) > now + timedelta(hours=24):
        raise ValueError("Cleanup schedule must be future and within the bounded session")
    if not execution_version or execution_version == "null":
        raise ValueError("An immutable execution version must precede scheduling")
    if launcher_arn != "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher":
        raise ValueError("Unexpected cleanup target")
    if role_arn != "arn:aws:iam::058114477594:role/techx-automation-scheduler":
        raise ValueError("Unexpected scheduler role")
    if dlq_arn != "arn:aws:sqs:us-east-1:058114477594:techx-automation-cleanup-dlq":
        raise ValueError("Unexpected cleanup failure queue")
    session_id = record["sessionId"]
    target_input = json.dumps(dict(sessionId=session_id, executionVersion=execution_version), sort_keys=True)
    return dict(
        Name="cleanup-" + hashlib.sha256(session_id.encode()).hexdigest()[:40], GroupName="techx-staging", State="ENABLED",
        ScheduleExpression=f"at({when.strftime('%Y-%m-%dT%H:%M:%S')})", ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow=dict(Mode="OFF"), ActionAfterCompletion="DELETE",
        Target=dict(Arn=launcher_arn, RoleArn=role_arn, Input=target_input,
                    RetryPolicy=dict(MaximumEventAgeInSeconds=3600, MaximumRetryAttempts=3),
                    DeadLetterConfig=dict(Arn=dlq_arn)),
    )


def register(client, record, execution_version, launcher_arn, role_arn, dlq_arn, now=None):
    desired = definition(record, execution_version, launcher_arn, role_arn, dlq_arn, now)
    try:
        existing = client.get_schedule(Name=desired["Name"], GroupName=desired["GroupName"])
    except client.exceptions.ResourceNotFoundException:
        token = hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
        response = client.create_schedule(**desired, ClientToken=token)
        arn = response["ScheduleArn"]
        # Read-after-create verifies that the watchdog has a real enabled target.
        existing = client.get_schedule(Name=desired["Name"], GroupName=desired["GroupName"])
        if existing.get("Arn") != arn:
            raise ValueError("Cleanup schedule readback does not match creation")
    for field in ("State", "ScheduleExpression", "ScheduleExpressionTimezone", "FlexibleTimeWindow", "ActionAfterCompletion", "Target"):
        if existing.get(field) != desired[field]:
            raise ValueError(f"Existing cleanup schedule differs: {field}; refusing overwrite")
    expected_prefix = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/"
    if existing.get("Arn") != expected_prefix + desired["Name"]:
        raise ValueError("Unexpected cleanup schedule ARN")
    return existing["Arn"]
