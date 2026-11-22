"""Authenticated read-only legacy collection and non-attesting assessment.

Own SDK clients, fixed endpoints/scope and private raw response provenance are
mandatory. This first R02 component cannot publish a closeout, retire authority,
create compute or admit staging. A successful collection is not migration done.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from execution_v2 import ACCOUNT, BUCKET, REGION, fields, reference, text, version
from migration_v2 import capture_legacy, direct_denial_binding
from plan_v2 import KMS_KEY
from recovery_v2 import service_time

ENDPOINTS = {"sts": f"https://sts.{REGION}.amazonaws.com", "s3": f"https://s3.{REGION}.amazonaws.com",
             "cloudtrail": f"https://cloudtrail.{REGION}.amazonaws.com", "codebuild": f"https://codebuild.{REGION}.amazonaws.com",
             "iam": "https://iam.amazonaws.com", "lambda": f"https://lambda.{REGION}.amazonaws.com",
             "events": f"https://events.{REGION}.amazonaws.com", "scheduler": f"https://scheduler.{REGION}.amazonaws.com",
             "ec2": f"https://ec2.{REGION}.amazonaws.com", "ssm": f"https://ssm.{REGION}.amazonaws.com"}
READS = {"sts": {"get_caller_identity"}, "s3": {"get_object", "head_object", "list_object_versions"},
         "cloudtrail": {"lookup_events"}, "codebuild": {"list_builds_for_project", "batch_get_builds"},
         "iam": {"list_roles", "get_role", "list_role_policies", "get_role_policy", "list_attached_role_policies",
                 "get_policy", "get_policy_version", "list_instance_profiles_for_role"},
         "lambda": {"list_functions", "get_function_configuration"},
         "events": {"list_rules", "describe_rule", "list_targets_by_rule"},
         "scheduler": {"list_schedule_groups", "list_schedules", "get_schedule"},
         "ec2": {"describe_instances"}, "ssm": {"describe_instance_information", "list_commands", "list_command_invocations"}}
PROJECTS = ("techx-automation-lifecycle", "techx-automation-cleanup")
TERMINAL = {"SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"}
_COLLECTION_SEAL = object()


class IncompleteAudit(ValueError):
    """Only constant codes/service names reach public output, never SDK messages."""
    def __init__(self, code, location=""):
        self.code, self.location = code, location
        super().__init__(code + (":" + location if location else ""))


def wire(value):
    if isinstance(value, datetime):
        if value.tzinfo is None: raise IncompleteAudit("NAIVE_SERVICE_TIME")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict): return {k: wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [wire(v) for v in value]
    return value


def encoded(value):
    return json.dumps(wire(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class PrivateEvidence:
    def __init__(self, parent):
        parent = Path(parent)
        if not parent.is_absolute(): raise ValueError("Private output must be an absolute path outside Git")
        parent = parent.resolve()
        if any((p / ".git").exists() for p in (parent, *parent.parents)):
            raise ValueError("Raw migration evidence must not be stored inside a Git checkout")
        self.path = parent / ("collection-" + uuid.uuid4().hex)
        self.path.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.index, self.sequence = [], 0

    def blob(self, label, payload):
        if not isinstance(payload, bytes): raise ValueError("Evidence must be original bytes")
        name = hashlib.sha256(label.encode()).hexdigest() + ".private.blob"
        path = self.path / name
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(payload)
        return dict(path=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))

    def record(self, value):
        self.sequence += 1
        stored = self.blob(f"record-{self.sequence}", encoded(value))
        self.index.append(dict(sequence=self.sequence, **stored))
        return deepcopy(stored)

    def finish(self, summary):
        return self.blob("collection-index", encoded(dict(summary=summary, records=self.index)))


class ReadOnlySDK:
    def __init__(self, clients, evidence, *, clock=lambda: datetime.now(timezone.utc)):
        self.clients, self.evidence, self.clock = clients, evidence, clock
        for service in READS:
            client = clients[service]
            if (client.meta.endpoint_url != ENDPOINTS[service]
                    or client.meta.region_name not in ((REGION, "aws-global") if service == "iam" else (REGION,))):
                raise IncompleteAudit("UNTRUSTED_ENDPOINT_OR_REGION", service)

    def call(self, service, operation, **parameters):
        if operation not in READS.get(service, set()):
            raise IncompleteAudit("MUTATION_OR_UNSUPPORTED_OPERATION", service + "." + operation)
        if service == "s3" and (parameters.get("Bucket") != BUCKET or parameters.get("ExpectedBucketOwner") != ACCOUNT):
            raise IncompleteAudit("S3_SCOPE_MISMATCH")
        started = self.clock()
        try:
            result = getattr(self.clients[service], operation)(**parameters)
        except Exception as error:
            response = getattr(error, "response", None)
            self.evidence.record(dict(service=service, operation=operation, parameters=parameters, startedAt=started,
                                      finishedAt=self.clock(), outcome="ERROR", response=response))
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "UNCLASSIFIED_READ_ERROR"
            raise IncompleteAudit(safe_code, service + "." + operation) from None
        if not isinstance(result, dict): raise IncompleteAudit("INVALID_RESPONSE", service + "." + operation)
        result = dict(result)
        if service == "s3" and operation == "get_object":
            stream = result.pop("Body", None)
            if stream is None or not callable(getattr(stream, "read", None)): raise IncompleteAudit("MISSING_OBJECT_BODY")
            try:
                payload = stream.read(1024 * 1024 + 1)
            finally:
                if callable(getattr(stream, "close", None)): stream.close()
            result["CapturedBody"] = self.evidence.blob(f"object-{uuid.uuid4().hex}", payload)
            body = payload
        else:
            body = None
        self.evidence.record(dict(service=service, operation=operation, parameters=parameters, startedAt=started,
                                  finishedAt=self.clock(), outcome="RESPONSE", response=result))
        metadata = result.get("ResponseMetadata", {})
        if (type(metadata.get("HTTPStatusCode")) is not int or metadata["HTTPStatusCode"] != 200
                or not isinstance(metadata.get("RequestId"), str) or not metadata["RequestId"]):
            raise IncompleteAudit("MISSING_POSITIVE_RESPONSE_METADATA", service + "." + operation)
        if body is not None:
            if len(body) > 1024 * 1024: raise IncompleteAudit("OBJECT_TOO_LARGE")
            result["BodyBytes"] = body
        return result


class CollectedLegacy:
    """In-process collection boundary; decoded operator JSON is not this object."""
    __slots__ = ("snapshot", "report", "evidence_path")

    def __init__(self, snapshot, report, path, *, _seal=None):
        if _seal is not _COLLECTION_SEAL: raise ValueError("Use the authenticated collector")
        self.snapshot, self.report, self.evidence_path = snapshot, deepcopy(report), str(path)


class LegacyCollector:
    def __init__(self, sdk, *, sleep=time.sleep):
        self.sdk, self.sleep = sdk, sleep

    def pages(self, service, operation, parameters, list_key, *, input_token="nextToken", output_token="nextToken", maximum=200):
        token, seen, seen_requests = None, set(), set()
        for _ in range(maximum):
            args = dict(parameters)
            if token: args[input_token] = token
            response = self.sdk.call(service, operation, **args)
            request_id = response["ResponseMetadata"]["RequestId"]
            if request_id in seen_requests: raise IncompleteAudit("REUSED_PAGE_RESPONSE", service + "." + operation)
            seen_requests.add(request_id)
            if list_key not in response or not isinstance(response[list_key], list):
                raise IncompleteAudit("MISSING_EXPLICIT_RESULT_LIST", service + "." + operation)
            yield response[list_key]
            token = response.get(output_token)
            if service == "iam":
                truncated = response.get("IsTruncated")
                if type(truncated) is not bool or truncated != bool(token):
                    raise IncompleteAudit("INCONSISTENT_IAM_PAGINATION")
            if token is None or token == "": return
            if not isinstance(token, str) or token in seen: raise IncompleteAudit("INVALID_OR_REPEATED_PAGE_TOKEN", service + "." + operation)
            seen.add(token)
            if service == "cloudtrail": self.sleep(0.55)  # LookupEvents regional 2 requests/second.
        raise IncompleteAudit("PAGE_BOUND_EXCEEDED", service + "." + operation)

    def object(self, key, *, object_version=None):
        args = dict(Bucket=BUCKET, Key=key, ExpectedBucketOwner=ACCOUNT)
        if object_version is not None: args["VersionId"] = object_version
        response = self.sdk.call("s3", "get_object", **args)
        version(response.get("VersionId"))
        if (object_version is not None and response["VersionId"] != object_version
                or response.get("ServerSideEncryption") != "aws:kms" or response.get("SSEKMSKeyId") != KMS_KEY):
            raise IncompleteAudit("WRONG_OBJECT_VERSION_OR_KMS")
        body = response["BodyBytes"]
        return dict(reference=dict(bucket=BUCKET, key=key, versionId=response["VersionId"], sha256=hashlib.sha256(body).hexdigest()), body=body)

    def unchanged(self, objects):
        for name in ("session", "pointer", "inputs"):
            ref = objects[name]["reference"]
            result = self.sdk.call("s3", "head_object", Bucket=BUCKET, Key=ref["key"], ExpectedBucketOwner=ACCOUNT)
            if (result.get("VersionId") != ref["versionId"] or result.get("ServerSideEncryption") != "aws:kms" or result.get("SSEKMSKeyId") != KMS_KEY):
                raise IncompleteAudit("SOURCE_CHANGED_DURING_CAPTURE", name)

    def session_history(self, session_id):
        key = "automation/staging/session.json"
        args = dict(Bucket=BUCKET, Prefix=key, ExpectedBucketOwner=ACCOUNT, MaxKeys=100)
        seen, versions_seen, rows, markers = set(), set(), [], []
        for _ in range(100):
            page = self.sdk.call("s3", "list_object_versions", **args)
            if type(page.get("IsTruncated")) is not bool or not isinstance(page.get("Versions"), list):
                raise IncompleteAudit("INCOMPLETE_VERSION_ENVELOPE")
            deletes = page.get("DeleteMarkers", [])  # Optional absent XML element is valid for this API only.
            if not isinstance(deletes, list): raise IncompleteAudit("INVALID_DELETE_MARKER_ENVELOPE")
            for marker in deletes:
                if not isinstance(marker, dict): raise IncompleteAudit("INVALID_DELETE_MARKER")
                if marker.get("Key") == key: markers.append(wire(marker))
            for row in page["Versions"]:
                if not isinstance(row, dict): raise IncompleteAudit("INVALID_VERSION_ROW")
                if row.get("Key") != key: continue
                version(row.get("VersionId"))
                if row["VersionId"] in versions_seen: raise IncompleteAudit("DUPLICATE_VERSION_ROW")
                versions_seen.add(row["VersionId"])
                if len(versions_seen) > 512: raise IncompleteAudit("VERSION_HISTORY_BOUND_EXCEEDED")
                if type(row.get("IsLatest")) is not bool or not isinstance(row.get("LastModified"), datetime) or row["LastModified"].tzinfo is None:
                    raise IncompleteAudit("INCOMPLETE_VERSION_METADATA")
                obj = self.object(key, object_version=row["VersionId"])
                value = json.loads(obj["body"])
                if value.get("sessionId") != session_id: continue
                rows.append(dict(reference=obj["reference"], lastModified=wire(row.get("LastModified")),
                                 isLatest=row.get("IsLatest"), originalRecord=value))
            if not page["IsTruncated"]:
                if not rows: raise IncompleteAudit("MISSING_ORIGINAL_VERSION_HISTORY")
                return dict(paginationComplete=True, versions=rows, deleteMarkers=markers,
                            limitation="Pagination does not prove deleted/expired historical versions never existed")
            key_marker, version_marker = page.get("NextKeyMarker"), page.get("NextVersionIdMarker")
            if not isinstance(key_marker, str) or not key_marker: raise IncompleteAudit("MISSING_VERSION_PAGE_MARKER")
            if version_marker is not None and not isinstance(version_marker, str): raise IncompleteAudit("INVALID_VERSION_PAGE_MARKER")
            pair = (key_marker, version_marker)
            if pair in seen: raise IncompleteAudit("REPEATED_VERSION_PAGE_MARKER")
            seen.add(pair); args["KeyMarker"] = key_marker
            args.pop("VersionIdMarker", None)
            if version_marker: args["VersionIdMarker"] = version_marker
        raise IncompleteAudit("VERSION_PAGE_BOUND_EXCEEDED")

    def builds(self):
        collected = {}
        for project in PROJECTS:
            ids = [i for page in self.pages("codebuild", "list_builds_for_project", dict(projectName=project, sortOrder="ASCENDING"), "ids") for i in page]
            if any(not isinstance(i, str) or not re.fullmatch(re.escape(project) + r":[a-f0-9-]+", i) for i in ids) or len(set(ids)) != len(ids):
                raise IncompleteAudit("INVALID_OR_DUPLICATE_BUILD_ID")
            builds = []
            for offset in range(0, len(ids), 100):
                batch = ids[offset:offset + 100]
                response = self.sdk.call("codebuild", "batch_get_builds", ids=batch)
                values = response.get("builds")
                if not isinstance(values, list) or response.get("buildsNotFound"):
                    raise IncompleteAudit("BUILD_STATE_UNKNOWN")
                if any(not isinstance(b, dict) for b in values) or len(values) != len(batch) or {b.get("id") for b in values} != set(batch):
                    raise IncompleteAudit("BUILD_BATCH_INCOMPLETE")
                for build in values:
                    if (build.get("projectName") != project or build.get("arn") != f"arn:aws:codebuild:{REGION}:{ACCOUNT}:build/{build['id']}"
                            or build.get("buildStatus") not in TERMINAL | {"IN_PROGRESS"}):
                        raise IncompleteAudit("BUILD_SCOPE_OR_STATE_MISMATCH")
                builds.extend(values)
            collected[project] = dict(paginationComplete=True, ids=ids, builds=builds)
        return collected

    def cloudtrail(self, snapshot, start, end):
        events, matches, seen = [], [], {}
        params = dict(LookupAttributes=[dict(AttributeKey="EventName", AttributeValue="StartBuild")], StartTime=start, EndTime=end, MaxResults=50)
        for page in self.pages("cloudtrail", "lookup_events", params, "Events", input_token="NextToken", output_token="NextToken"):
            for row in page:
                event = json.loads(row["CloudTrailEvent"])
                request = event.get("requestParameters") or {}
                if request.get("projectName") not in PROJECTS: continue
                event_id = event.get("eventID")
                if not isinstance(event_id, str) or not event_id: raise IncompleteAudit("INVALID_TRAIL_EVENT_ID")
                digest = hashlib.sha256(encoded(event)).hexdigest()
                if event_id in seen:
                    if seen[event_id] != digest: raise IncompleteAudit("CONFLICTING_TRAIL_EVENT")
                    continue  # LookupEvents may repeat the same event across pages; raw pages remain journaled.
                seen[event_id] = digest; events.append(event)
                try:
                    matches.append(direct_denial_binding(event, snapshot, now=end))
                except ValueError:
                    pass  # Preserve raw unbound events; do not convert them into denial/zero proof.
        return dict(paginationComplete=True, scannedFrom=wire(start), scannedUntil=wire(end), events=events, directDenialBindings=matches,
                    limitation="Current scan may have delivery lag; redacted/missing binding is not rejected creation proof")

    def controls(self):
        roles = [r for p in self.pages("iam", "list_roles", {}, "Roles", input_token="Marker", output_token="Marker") for r in p
                 if r.get("RoleName", "").startswith("techx-automation-") and not re.match(r"techx-automation-v2(?:-|$)", r["RoleName"])]
        functions = [f for p in self.pages("lambda", "list_functions", {}, "Functions", input_token="Marker", output_token="NextMarker") for f in p
                     if f.get("FunctionName", "").startswith("techx-automation-") and not re.match(r"techx-automation-v2(?:-|$)", f["FunctionName"])]
        rules = [r for p in self.pages("events", "list_rules", dict(NamePrefix="techx-automation-"), "Rules", input_token="NextToken", output_token="NextToken") for r in p
                 if not re.match(r"techx-automation-v2(?:-|$)", r.get("Name", ""))]
        return dict(roles=roles, functions=functions, rules=rules,
                    limitation="Candidate names are not effective policy retirement or proof that Lambda invocations drained")

    def collect(self, session_id, expected_execution_ref):
        text(session_id, r"staging-gh-[1-9][0-9]{0,19}")
        reference(expected_execution_ref, key=f"executions/{session_id}.json")
        identity = self.sdk.call("sts", "get_caller_identity")
        if identity.get("Account") != ACCOUNT: raise IncompleteAudit("WRONG_CALLER_ACCOUNT")
        objects = dict(session=self.object("automation/staging/session.json"),
                       execution=self.object(expected_execution_ref["key"], object_version=expected_execution_ref["versionId"]),
                       pointer=self.object("control/staging/current-execution.json"), inputs=self.object(f"private/staging/{session_id}/inputs.json"))
        snapshot = capture_legacy(objects, session_id=session_id, execution_ref=expected_execution_ref)
        self.unchanged(objects)
        original = json.loads(objects["execution"]["body"])
        start, end = service_time(original["createdAt"]) - timedelta(minutes=10), self.sdk.clock()
        if end <= start or end - start > timedelta(days=89):
            raise IncompleteAudit("CLOUDTRAIL_RETENTION_CANNOT_COVER_SCOPE")
        history = self.session_history(session_id)
        builds = self.builds()
        trail = self.cloudtrail(snapshot, start, end)
        controls = self.controls()
        self.unchanged(objects)  # Refuse a head/pointer/input race during long reads.
        bundle = dict(purpose="READ_ONLY_RECONCILIATION_INPUT_NOT_ATTESTATION", account=ACCOUNT, region=REGION,
                      observedAt=wire(self.sdk.clock()), snapshot=snapshot.as_dict(), sessionHistory=history,
                      buildHistory=builds, callHistory=trail, controlCandidates=controls)
        self.sdk.evidence.record(bundle)
        active = [b["id"] for p in builds.values() for b in p["builds"] if b["buildStatus"] == "IN_PROGRESS"]
        report = dict(collectionStatus="COMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False,
                      sessionId=session_id, originalPhase=snapshot.as_dict()["originalDispatch"]["phase"],
                      originalVersionCount=len(history["versions"]), projectBuildCounts={k:len(v["ids"]) for k,v in builds.items()},
                      activeBuildCount=len(active), directDenialBindingCount=len(trail["directDenialBindings"]),
                      roleCandidateCount=len(controls["roles"]), functionCandidateCount=len(controls["functions"]),
                      enabledRuleCount=sum(r.get("State") == "ENABLED" for r in controls["rules"]),
                      requiredNext=["actual-human-review", "complete-actor-and-workload-verification", "effective-authority-retirement",
                                    "recovery-owner-health-and-termination", "trusted-closeout-producer-verifier"])
        self.sdk.evidence.finish(report)
        return CollectedLegacy(snapshot, report, self.sdk.evidence.path, _seal=_COLLECTION_SEAL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("collect",))
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--expected-execution-reference", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    evidence = PrivateEvidence(args.private_output)
    try:
        import boto3
        from botocore.config import Config
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        config = Config(connect_timeout=10, read_timeout=20, retries={"max_attempts":2})
        clients = {name:session.client(name, region_name=REGION, endpoint_url=url, config=config) for name,url in ENDPOINTS.items()}
        sdk = ReadOnlySDK(clients, evidence)
        ref = json.loads(args.expected_execution_reference.read_bytes())
        result = LegacyCollector(sdk).collect(args.session_id, ref)
    except Exception as error:
        code = str(error) if isinstance(error, IncompleteAudit) else "INVALID_OR_INCOMPLETE_COLLECTION"
        evidence.record(dict(outcome="INCOMPLETE", errorType=type(error).__name__, code=code))
        evidence.finish(dict(collectionStatus="INCOMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False, reason=code))
        print(json.dumps(dict(collectionStatus="INCOMPLETE", migrationStatus="NOT_VERIFIED", attestationEligible=False,
                              reason=code, privateEvidence=str(evidence.path))))
        return 2
    print(json.dumps(dict(result.report, privateEvidence=result.evidence_path)))
    return 0  # Complete read-only collection, explicitly NOT migration acceptance.


if __name__ == "__main__":
    raise SystemExit(main())
