"""AWS-only cleanup launcher: immutable session input, bounded retries, no GitHub dependency."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re


def validate_execution(record, session_id, version):
    if (record.get("sessionId") != session_id or record.get("account") != "058114477594"
            or record.get("region") != "us-east-1" or record.get("environment") != "staging"):
        raise ValueError("Execution does not match the approved staging scope")
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", session_id) or not version or version == "null":
        raise ValueError("An immutable S3 execution version is required")
    deadline = datetime.fromisoformat(record["deadline"].replace("Z", "+00:00"))
    cleanup = datetime.fromisoformat(record["cleanupStartAt"].replace("Z", "+00:00"))
    if deadline.tzinfo is None or cleanup.tzinfo is None or cleanup >= deadline:
        raise ValueError("Execution has invalid cleanup/deadline timestamps")
    artifact = record["cleanupBundle"]
    if (not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", ""))
            or not re.fullmatch(r"bundles/[a-f0-9]{64}\.tar\.gz", artifact.get("key", ""))
            or not artifact.get("versionId") or artifact["versionId"] == "null"):
        raise ValueError("Cleanup bundle must be immutable and content addressed")
    return cleanup


def launch(event, s3, codebuild, now=None):
    now = now or datetime.now(timezone.utc)
    bucket, project = os.environ["CONTROL_BUCKET"], os.environ["CLEANUP_PROJECT"]
    if event.get("watchdog") is True:
        # This pointer is writable only by the lifecycle coordinator, never publisher.
        pointer = s3.get_object(Bucket=bucket, Key="control/staging/current-execution.json", ExpectedBucketOwner="058114477594")
        event = json.loads(pointer["Body"].read())
        if event.get("status") == "OFF":
            return {"status": "off"}
    session_id, version = event["sessionId"], event["executionVersion"]
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", session_id):
        raise ValueError("Invalid cleanup session")
    response = s3.get_object(Bucket=bucket, Key=f"executions/{session_id}.json", VersionId=version,
                             ExpectedBucketOwner="058114477594")
    record = json.loads(response["Body"].read())
    cleanup = validate_execution(record, session_id, version)
    if now < cleanup and event.get("requestedEarly") is not True:
        return {"status": "not-due", "sessionId": session_id}
    # This completion marker lives outside publisher-writable session state.
    marker_key = f"cleanup/{session_id}/complete.json"
    # Explicit prefix listing proves absence; GetObject without ListBucket can
    # return AccessDenied for a missing key, which must not be swallowed.
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=marker_key, MaxKeys=1, ExpectedBucketOwner="058114477594")
    if any(item.get("Key") == marker_key for item in listed.get("Contents", [])):
        marker = s3.get_object(Bucket=bucket, Key=marker_key, ExpectedBucketOwner="058114477594")
        completed = json.loads(marker["Body"].read())
        if (completed.get("executionVersion") == version and completed.get("inventoryEmpty") is True
                and re.fullmatch(r"[a-f0-9]{64}", completed.get("artifactSha256", ""))):
            return {"status": "already-destroyed", "sessionId": session_id}
        raise ValueError("Invalid cleanup completion marker")
    # Reuse an in-flight cleanup. Do not start a second worker if its status is unknown.
    builds = codebuild.list_builds_for_project(projectName=project, sortOrder="DESCENDING").get("ids", [])
    if builds:
        statuses = codebuild.batch_get_builds(ids=builds[:20])
        if statuses.get("buildsNotFound") or {b.get("id") for b in statuses.get("builds", [])} != set(builds[:20]):
            raise ValueError("Cleanup build status could not be verified")
        for build in statuses.get("builds", []):
            values = {item["name"]: item.get("value") for item in build.get("environment", {}).get("environmentVariables", [])}
            if build.get("buildStatus") == "IN_PROGRESS":
                if values.get("TECHX_SESSION_ID") == session_id and values.get("TECHX_EXECUTION_VERSION") == version:
                    return {"status": "running", "buildId": build["id"], "sessionId": session_id}
                raise ValueError("Another staging cleanup is still running")
    result = codebuild.start_build(
        projectName=project,
        idempotencyToken=hashlib.sha256(f"{session_id}:{version}:{int(now.timestamp()) // 300}".encode()).hexdigest(),
        environmentVariablesOverride=[dict(name="TECHX_SESSION_ID", value=session_id, type="PLAINTEXT"),
                                      dict(name="TECHX_EXECUTION_VERSION", value=version, type="PLAINTEXT")],
    )
    return {"status": "started", "buildId": result["build"]["id"], "sessionId": session_id}


def handler(event, context):
    import boto3
    return launch(event, boto3.client("s3"), boto3.client("codebuild"))
