"""SDK-backed v2 session CAS transport, not admission or finalization authority.

Canonical execution/window versions and role identity are read through owned
clients. Reads bind body and tags to one exact S3 version. There is no create,
arbitrary-key write, lease stealing, terminal completion or mutation retry API.
R05 must enforce the tag/role matrix and protect immutable/terminal prefixes;
R06 must authenticate human review, migration admission and operation readiness.
"""
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import json
import re
import uuid
from urllib.parse import urlencode

from execution_v2 import (ACCOUNT, BUCKET, PREFIX, REGION, bind_authorized_window, decode_execution,
                          decode_object, execution_reference, fields, integer, timestamp, validate_session, version)
from plan_v2 import KMS_KEY

ENDPOINTS = dict(s3=f"https://s3.{REGION}.amazonaws.com", sts=f"https://sts.{REGION}.amazonaws.com")
ENVELOPE = {"schemaVersion", "executorBackend", "sessionId", "executionReference", "status", "leaseEpoch"}
READ_ROLES = {"manager", "lifecycle-launcher", "cleanup-launcher", "deployment-executor", "cleanup-executor",
              "probe-executor", "readiness", "finalizer", "admission"}
_SEAL = object()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def positive(response):
    meta = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    if type(meta.get("HTTPStatusCode")) is not int or meta["HTTPStatusCode"] != 200 or not isinstance(meta.get("RequestId"), str) or not meta["RequestId"]:
        raise ValueError("Positive AWS response metadata required")


def etag(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Opaque service ETag required")
    return value


def runtime_session(value, execution, ref):
    fields(value, ENVELOPE | {"runtimeRevision", "lastMutationId"})
    validate_session({k:value[k] for k in ENVELOPE}, execution, ref)
    integer(value["runtimeRevision"])
    if not isinstance(value["lastMutationId"], str) or not re.fullmatch(r"[a-f0-9]{32}", value["lastMutationId"]):
        raise ValueError("A stable runtime mutation identifier is required")
    return deepcopy(value)


def session_tags(value, ref):
    return {"techx-schema":"2", "techx-backend":"ec2-ssm", "techx-session-id":value["sessionId"],
            "techx-execution-sha256":ref["sha256"], "techx-status":value["status"],
            "techx-lease-epoch":str(value["leaseEpoch"]), "techx-runtime-revision":str(value["runtimeRevision"]),
            "techx-mutation-id":value["lastMutationId"]}


class CASConflict(ValueError):
    pass


class MutationConsumed(ValueError):
    pass


class MutationUncertain(ValueError):
    def __init__(self, prepared):
        self.prepared = prepared
        super().__init__("Session write outcome unknown; reconcile the same prepared mutation, never blindly retry")


class WriteSuperseded(ValueError):
    pass


class SessionObservation:
    """In-process provenance guard, not a sandbox against arbitrary Python."""
    __slots__ = ("_store", "_record", "_ref", "_etag")

    def __init__(self, store, record, ref, object_etag, *, _seal=None):
        if _seal is not _SEAL: raise ValueError("Use authenticated session reads")
        self._store, self._record, self._ref, self._etag = store, deepcopy(record), deepcopy(ref), object_etag

    def as_dict(self): return deepcopy(self._record)

    def envelope(self): return {k:deepcopy(self._record[k]) for k in ENVELOPE}

    @property
    def reference(self): return deepcopy(self._ref)


class PreparedMutation:
    __slots__ = ("_store", "_base", "_record", "_body", "_sha256", "_state", "_nonce")

    def __init__(self, store, base, record, *, _seal=None):
        if _seal is not _SEAL: raise ValueError("Use scoped transition preparation")
        self._store, self._base, self._record = store, base, deepcopy(record)
        self._body = encoded(record)
        self._sha256 = hashlib.sha256(self._body).hexdigest()
        self._state = "PREPARED"
        self._nonce = uuid.uuid4().hex

    def as_dict(self): return deepcopy(self._record)


class SessionStore:
    def __init__(self, clients, execution_ref, *, clock=lambda: datetime.now(timezone.utc)):
        self.clients, self.clock, self._token = clients, clock, object()
        for name, endpoint in ENDPOINTS.items():
            client = clients[name]
            if client.meta.endpoint_url != endpoint or client.meta.region_name != REGION:
                raise ValueError("Fixed regional AWS clients required")
        # Botocore must not retry a lost conditional-write response internally.
        retries = clients["s3"].meta.config.retries
        if not isinstance(retries, dict) or retries.get("total_max_attempts") != 1:
            raise ValueError("Session S3 transport must disable SDK retries (total_max_attempts=1)")
        self._principal, self._role = self._caller()
        if not isinstance(execution_ref, dict): raise ValueError("Exact execution reference required")
        sid = execution_ref.get("key", "").removeprefix(PREFIX + "executions/").removesuffix(".json")
        execution_reference(execution_ref, sid)
        body, _, _ = self._object(execution_ref["key"], expected=execution_ref)
        self._execution = decode_execution(body, execution_ref, sid)
        self._execution_ref = deepcopy(execution_ref)
        grant_ref = self._execution["authorizationReference"]
        grant_body, _, _ = self._object(grant_ref["key"], expected=grant_ref)
        bind_authorized_window(self._execution, grant_body, grant_ref, purpose="staging")
        self.key = f"{PREFIX}sessions/{sid}.json"

    @property
    def execution(self): return deepcopy(self._execution)

    @property
    def execution_reference(self): return deepcopy(self._execution_ref)

    def _caller(self):
        result = self.clients["sts"].get_caller_identity()
        positive(result)
        match = re.fullmatch(rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-([a-z-]+)/([A-Za-z0-9+=,.@_-]{{1,64}})", str(result.get("Arn", "")))
        if result.get("Account") != ACCOUNT or not match or match[1] not in READ_ROLES:
            raise ValueError("Actual dedicated v2 role identity required")
        if match[1].endswith("-executor") and not re.fullmatch(r"i-[a-f0-9]{17}", match[2]):
            raise ValueError("Executor requires its actual instance role session")
        if match[1] == "manager" and not re.fullmatch(r"techx-v2-[1-9][0-9]{0,19}-1", match[2]):
            raise ValueError("Manager role session must bind the first GitHub attempt")
        if hasattr(self, "_principal") and result["Arn"] != self._principal:
            raise ValueError("Runtime principal changed during the operation")
        return result["Arn"], match[1]

    def _object(self, key, *, expected=None):
        args = dict(Bucket=BUCKET, Key=key, ExpectedBucketOwner=ACCOUNT)
        if expected is not None: args["VersionId"] = expected["versionId"]
        result = self.clients["s3"].get_object(**args)
        stream = result.get("Body") if isinstance(result, dict) else None
        try:
            positive(result)
            version(result.get("VersionId")); object_etag = etag(result.get("ETag"))
            if result.get("ServerSideEncryption") != "aws:kms" or result.get("SSEKMSKeyId") != KMS_KEY:
                raise ValueError("Session context has unexpected encryption")
            if not callable(getattr(stream, "read", None)): raise ValueError("Original object bytes required")
            body = stream.read(32769)
        finally:
            if callable(getattr(stream, "close", None)): stream.close()
        if not isinstance(body, bytes) or not 0 < len(body) <= 32768: raise ValueError("Bounded original object bytes required")
        ref = dict(bucket=BUCKET, key=key, versionId=result["VersionId"], sha256=hashlib.sha256(body).hexdigest())
        if expected is not None and ref != expected: raise ValueError("Exact original object version/hash changed")
        return body, ref, object_etag

    def read(self):
        self._caller()
        body, ref, object_etag = self._object(self.key)
        value = runtime_session(decode_object(body, ref, key=self.key), self._execution, self._execution_ref)
        if value["status"] == "DESTROYED":
            raise ValueError("Terminal session requires the protected finalizer receipt reader")
        self._tags(ref, session_tags(value, self._execution_ref))
        return SessionObservation(self._token, value, ref, object_etag, _seal=_SEAL)

    def _tags(self, ref, expected):
        result = self.clients["s3"].get_object_tagging(Bucket=BUCKET, Key=ref["key"], VersionId=ref["versionId"], ExpectedBucketOwner=ACCOUNT)
        positive(result)
        if result.get("VersionId") != ref["versionId"] or not isinstance(result.get("TagSet"), list):
            raise ValueError("Version-specific session tags required")
        found = {}
        for row in result["TagSet"]:
            if not isinstance(row, dict) or not isinstance(row.get("Key"), str) or not isinstance(row.get("Value"), str) or row["Key"] in found:
                raise ValueError("Invalid or duplicate session tag")
            found[row["Key"]] = row["Value"]
        if found != expected: raise ValueError("Session body differs from its exact version tags")

    def _snapshot(self, snapshot):
        if not isinstance(snapshot, SessionObservation) or snapshot._store is not self._token:
            raise ValueError("A same-store authenticated observation is required")
        return runtime_session(snapshot.as_dict(), self._execution, self._execution_ref)

    def _guard(self, old, target):
        self._caller()
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now < timestamp(self._execution["startedAt"]):
            raise ValueError("Session window has not started")
        if old["status"] == "DESTROYED" or target == "DESTROYED":
            raise ValueError("Terminal completion requires the separate trusted finalizer")
        cleanup = self._role in ("cleanup-launcher", "cleanup-executor")
        if not cleanup and now >= timestamp(self._execution["cleanupStartAt"]):
            raise ValueError("New deployment/session transition forbidden in cleanup reserve")
        allowed = False
        if cleanup:
            allowed = (target == "DESTROYING" and old["status"] != "DESTROYING") or (old["status"] == "DESTROYING" and target == "FAILED_NEEDS_CLEANUP")
        elif self._role == "deployment-executor":
            allowed = ((old["status"], target) == ("PLANNING", "PROVISIONING") or
                       old["status"] in ("PLANNING", "PROVISIONING", "TESTING") and target == "FAILED_NEEDS_CLEANUP")
        elif self._role == "manager": allowed = (old["status"], target) == ("READY", "TESTING")
        elif self._role == "readiness": allowed = old["status"] in ("PROVISIONING", "TESTING") and target in ("READY", "FAILED_NEEDS_CLEANUP")
        if not allowed: raise ValueError("Transition is outside the actual role/state scope")

    def prepare(self, snapshot, target):
        old = self._snapshot(snapshot)
        self._guard(old, target)
        updated = dict(old, status=target, runtimeRevision=old["runtimeRevision"] + 1)
        if target == "DESTROYING": updated["leaseEpoch"] += 1
        updated["lastMutationId"] = self._mutation_id(snapshot.reference, updated)
        runtime_session(updated, self._execution, self._execution_ref)
        return PreparedMutation(self._token, snapshot, updated, _seal=_SEAL)

    @staticmethod
    def _mutation_id(base, record):
        # A semantic transition has one durable slot across processes/principals.
        return hashlib.sha256(encoded(dict(base=base, target=record["status"], epoch=record["leaseEpoch"],
                                          revision=record["runtimeRevision"]))).hexdigest()[:32]

    def _claim_key(self, mutation_id):
        if not isinstance(mutation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", mutation_id): raise ValueError("Exact mutation identifier required")
        return f"{PREFIX}session-mutations/{self._execution['sessionId']}/{mutation_id}.json"

    def _put(self, key, body, object_tags, **condition):
        return self.clients["s3"].put_object(Bucket=BUCKET, Key=key, ExpectedBucketOwner=ACCOUNT,
            Body=body, ContentType="application/json", ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY,
            Tagging=urlencode(sorted(object_tags.items())), ChecksumAlgorithm="SHA256",
            ChecksumSHA256=base64.b64encode(hashlib.sha256(body).digest()).decode(), **condition)

    @staticmethod
    def _write_response(response, sha256):
        positive(response); version(response.get("VersionId")); etag(response.get("ETag"))
        if (response.get("ServerSideEncryption") != "aws:kms" or response.get("SSEKMSKeyId") != KMS_KEY
                or response.get("ChecksumSHA256") != base64.b64encode(bytes.fromhex(sha256)).decode()):
            raise ValueError("Unverified write response")

    def _prepared(self, prepared):
        if not isinstance(prepared, PreparedMutation) or prepared._store is not self._token:
            raise ValueError("A same-store prepared mutation is required")

    def commit(self, prepared):
        self._prepared(prepared)
        if prepared._state != "PREPARED": raise ValueError("Mutation is consumed or uncertain; reconcile, never retry")
        old = self._snapshot(prepared._base)
        self._guard(old, prepared._record["status"])
        current = self.read()
        if current.reference != prepared._base.reference or current._etag != prepared._base._etag:
            prepared._state = "CONFLICT"
            raise CASConflict("Session changed since preparation")
        # Durable create-once claim prevents re-preparation in another process
        # from replaying the same unknown session write. Existing claims are
        # observation-only, never permission to execute their write again.
        claim = dict(schemaVersion=2, executionReference=self._execution_ref, baseSessionReference=prepared._base.reference,
                     mutationId=prepared._record["lastMutationId"], principalArn=self._principal, claimNonce=prepared._nonce,
                     intendedRecord=prepared._record, intendedSha256=prepared._sha256)
        claim_body = encoded(claim)
        prepared._state = "CLAIMING"
        try:
            response = self._put(self._claim_key(claim["mutationId"]), claim_body,
                                 session_tags(prepared._record, self._execution_ref), IfNoneMatch="*")
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
            if (code, status) in (("PreconditionFailed", 412), ("ConditionalRequestConflict", 409)):
                prepared._state = "CONSUMED"
                raise MutationConsumed("Durable mutation slot is consumed; recover/observe without another session write") from None
            raise MutationUncertain(prepared) from None
        try: self._write_response(response, hashlib.sha256(claim_body).hexdigest())
        except Exception: raise MutationUncertain(prepared) from None
        prepared._state = "CLAIMED"
        self._guard(old, prepared._record["status"])
        current = self.read()
        if current.reference != prepared._base.reference or current._etag != prepared._base._etag:
            prepared._state = "CONFLICT"
            raise CASConflict("Session changed while mutation claim was being consumed")
        prepared._state = "CALLING"
        try:
            response = self._put(self.key, prepared._body, session_tags(prepared._record, self._execution_ref), IfMatch=prepared._base._etag)
        except Exception as error:
            response = getattr(error, "response", {})
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
            if (code, status) in (("PreconditionFailed", 412), ("ConditionalRequestConflict", 409)):
                prepared._state = "CONFLICT"
                raise CASConflict("Conditional session write conflicted; reload without automatic retry") from None
            raise MutationUncertain(prepared) from None
        try:
            self._write_response(response, prepared._sha256)
            latest = self.read()
        except Exception:
            raise MutationUncertain(prepared) from None
        if latest.reference["versionId"] != response["VersionId"] or latest.reference["sha256"] != prepared._sha256:
            prepared._state = "SUPERSEDED"
            raise WriteSuperseded("Write was accepted but session was superseded; observe the current fence")
        if latest._etag != response["ETag"]: raise MutationUncertain(prepared)
        prepared._state = "OBSERVED"
        return latest

    def reconcile(self, prepared):
        self._prepared(prepared)
        if prepared._state not in ("CLAIMING", "CLAIMED", "CONSUMED", "CALLING", "OBSERVED", "SUPERSEDED"):
            raise ValueError("Only an attempted mutation may be reconciled")
        current = self.read()
        if current.reference["sha256"] == prepared._sha256:
            prepared._state = "OBSERVED"
            return current
        return None  # Unknown/superseded is never permission to send another write.

    def recover(self, mutation_id):
        """Read-only recovery across process restarts; never consumes an old claim."""
        self._caller()
        key = self._claim_key(mutation_id)
        body, ref, _ = self._object(key)
        claim = decode_object(body, ref, key=key)
        fields(claim, {"schemaVersion", "executionReference", "baseSessionReference", "mutationId", "principalArn",
                       "claimNonce", "intendedRecord", "intendedSha256"})
        intended = runtime_session(claim["intendedRecord"], self._execution, self._execution_ref)
        from execution_v2 import reference
        reference(claim["baseSessionReference"], key=self.key)
        principal = str(claim["principalArn"])
        if (type(claim["schemaVersion"]) is not int or claim["schemaVersion"] != 2 or claim["executionReference"] != self._execution_ref
                or claim["mutationId"] != mutation_id or intended["lastMutationId"] != mutation_id
                or self._mutation_id(claim["baseSessionReference"], intended) != mutation_id
                or not re.fullmatch(rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-[a-z-]+/[A-Za-z0-9+=,.@_-]{{1,64}}", principal)
                or not isinstance(claim["claimNonce"], str) or not re.fullmatch(r"[a-f0-9]{32}", claim["claimNonce"])
                or claim["intendedSha256"] != hashlib.sha256(encoded(intended)).hexdigest()):
            raise ValueError("Durable mutation claim differs from its canonical transition")
        self._tags(ref, session_tags(intended, self._execution_ref))
        base_body, base_ref, _ = self._object(self.key, expected=claim["baseSessionReference"])
        base = runtime_session(decode_object(base_body, base_ref, key=self.key), self._execution, self._execution_ref)
        self._tags(base_ref, session_tags(base, self._execution_ref))
        transitions = {"PLANNING":{"PROVISIONING", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
                       "PROVISIONING":{"READY", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
                       "READY":{"TESTING", "DESTROYING"}, "TESTING":{"READY", "FAILED_NEEDS_CLEANUP", "DESTROYING"},
                       "FAILED_NEEDS_CLEANUP":{"DESTROYING"}, "DESTROYING":{"FAILED_NEEDS_CLEANUP"}}
        if (intended["status"] not in transitions.get(base["status"], set())
                or intended["runtimeRevision"] != base["runtimeRevision"] + 1
                or intended["leaseEpoch"] != base["leaseEpoch"] + (1 if intended["status"] == "DESTROYING" else 0)):
            raise ValueError("Claim changed its original session revision, fence or transition")
        current = self.read()
        return current if current.reference["sha256"] == claim["intendedSha256"] else None
