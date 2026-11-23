"""Worker-owned IMDSv2/STS/EC2 identity; registration is not admission.

The fixed private registration is create-once and binds an OS boot. A reboot,
stop/start, IP drift or changed intent cannot silently reuse a saved plan.
R05 must restrict this prefix to the exact worker and forbid overwrite/delete.
Registry slots, launch authority, task leases and dispatch remain separate.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from execution_v2 import ACCOUNT, PREFIX, REGION, decode_object, reference, text, timestamp
from session_runtime_v2 import MutationConsumed, SessionStore, encoded, positive
from worker_identity import validate_launch_intent, worker_identity

_SEAL = object()
IMDS = "http://169.254.169.254"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class WorkerRejected(ValueError):
    pass


class RegistrationUncertain(ValueError):
    pass


class InstanceMetadata:
    """Fixed IMDSv2-only calls, no proxies, redirect, credential fetch or logs."""
    def __init__(self, *, open_url=None):
        self._open = open_url or build_opener(ProxyHandler({}), NoRedirect()).open

    def _request(self, path, method, headers):
        url = IMDS + path
        try:
            with self._open(Request(url, method=method, headers=headers), timeout=2) as response:
                raw = response.read(4097)
                if response.status != 200 or response.geturl() != url or not 0 < len(raw) <= 4096:
                    raise ValueError("Incomplete metadata response")
                return raw.decode("ascii")
        except Exception: raise WorkerRejected("IMDSV2_UNAVAILABLE") from None

    def instance_id(self):
        token = self._request("/latest/api/token", "PUT", {"X-aws-ec2-metadata-token-ttl-seconds":"60"})
        if any(c.isspace() or ord(c) < 33 for c in token): raise WorkerRejected("INVALID_IMDSV2_TOKEN")
        result = self._request("/latest/meta-data/instance-id", "GET", {"X-aws-ec2-metadata-token":token})
        return text(result, r"i-[a-f0-9]{17}")


def boot_id():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return text(value, r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
    except Exception: raise WorkerRejected("LINUX_BOOT_ID_REQUIRED") from None


class LiveWorker:
    """Same-process identity guard, never reconstructed from sender JSON."""
    __slots__ = ("_runtime", "_identity")

    def __init__(self, runtime, identity, *, _seal=None):
        if _seal is not _SEAL: raise WorkerRejected("USE_WORKER_OWNED_VERIFICATION")
        self._runtime, self._identity = runtime, deepcopy(identity)

    def as_dict(self): return deepcopy(self._identity)

    def require_current(self):
        actual = self._runtime._identity()
        if actual != self._identity: raise WorkerRejected("WORKER_BOOT_OR_IDENTITY_CHANGED")
        return deepcopy(actual)


class WorkerRuntime:
    def __init__(self, clients, execution_ref, intent_ref, *, metadata=None, read_boot_id=boot_id,
                 clock=lambda:datetime.now(timezone.utc)):
        self._store = SessionStore(clients, execution_ref, clock=clock)
        self._clients, self._clock = clients, clock
        self._metadata, self._boot = metadata or InstanceMetadata(), read_boot_id
        if self._store._role not in {"deployment-executor", "cleanup-executor", "probe-executor"}:
            raise WorkerRejected("ACTUAL_INSTANCE_EXECUTION_ROLE_REQUIRED")
        ec2 = clients["ec2"]
        if (ec2.meta.endpoint_url != f"https://ec2.{REGION}.amazonaws.com" or ec2.meta.region_name != REGION
                or ec2.meta.config.retries.get("total_max_attempts") != 1): raise WorkerRejected("FIXED_EC2_CLIENT_REQUIRED")
        purpose = self._store._role.removesuffix("-executor")
        sid = self._store.execution["sessionId"]
        # The instance role fixes purpose. Only cleanup has replacement slots.
        if not isinstance(intent_ref, dict): raise WorkerRejected("EXACT_LAUNCH_INTENT_REQUIRED")
        allowed = {f"{PREFIX}launches/{sid}/{purpose}/{n}/intent.json" for n in range(1,4 if purpose == "cleanup" else 2)}
        if intent_ref.get("key") not in allowed: raise WorkerRejected("WORKER_INTENT_SCOPE_MISMATCH")
        reference(intent_ref, key=intent_ref["key"])
        raw, _, _ = self._store._object(intent_ref["key"], expected=intent_ref)
        intent = decode_object(raw, intent_ref, key=intent_ref["key"])
        validate_launch_intent(intent, self._store.execution, self._store.execution_reference, intent.get("leaseEpoch"))
        if (intent["purpose"] != purpose or intent_ref["key"] != f"{PREFIX}launches/{sid}/{purpose}/{intent['attempt']}/intent.json"):
            raise WorkerRejected("WORKER_INTENT_SLOT_MISMATCH")
        self._intent, self._intent_ref = intent, deepcopy(intent_ref)

    def _identity(self):
        self._store._caller()
        before_boot = text(self._boot(), r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
        instance_id = self._metadata.instance_id()
        result = self._clients["ec2"].describe_instances(InstanceIds=[instance_id])
        positive(result)
        rows = result.get("Reservations")
        if (result.get("NextToken") or not isinstance(rows, list) or len(rows) != 1
                or rows[0].get("OwnerId") != ACCOUNT or not isinstance(rows[0].get("Instances"), list)
                or len(rows[0]["Instances"]) != 1 or rows[0]["Instances"][0].get("InstanceId") != instance_id):
            raise WorkerRejected("EXACT_OWNED_INSTANCE_REQUIRED")
        caller = self._clients["sts"].get_caller_identity(); positive(caller)
        principal_id = text(caller.get("UserId"), rf"AROA[A-Z0-9]{{17}}:{instance_id}")
        instance = rows[0]["Instances"][0]
        options = instance.get("MetadataOptions", {})
        if options.get("HttpEndpoint") != "enabled" or type(options.get("HttpPutResponseHopLimit")) is not int or options["HttpPutResponseHopLimit"] != 1:
            raise WorkerRejected("FIXED_INSTANCE_METADATA_BOUNDARY_REQUIRED")
        identity = worker_identity(self._intent, instance, caller, self._store.execution,
                                   self._store.execution_reference, self._intent["leaseEpoch"])
        current = self._store.read().as_dict()
        now, execution = self._clock(), self._store.execution
        if current["leaseEpoch"] < self._intent["leaseEpoch"] or now < timestamp(execution["startedAt"]):
            raise WorkerRejected("WORKER_SESSION_FENCE_MISMATCH")
        if identity["purpose"] != "cleanup" and (current["leaseEpoch"] != self._intent["leaseEpoch"]
                or now >= timestamp(execution["cleanupStartAt"])
                or current["status"] not in {"PLANNING", "PROVISIONING", "READY", "TESTING"}):
            raise WorkerRejected("WORKER_DEPLOYMENT_FENCED")
        if self._boot() != before_boot or self._metadata.instance_id() != instance_id:
            raise WorkerRejected("WORKER_CHANGED_DURING_VERIFICATION")
        return dict(identity, bootId=before_boot, principalId=principal_id)

    def verify(self):
        try: return LiveWorker(self, self._identity(), _seal=_SEAL)
        except WorkerRejected: raise
        except Exception: raise WorkerRejected("WORKER_VERIFICATION_FAILED") from None

    def register(self, worker):
        if not isinstance(worker, LiveWorker) or worker._runtime is not self:
            raise WorkerRejected("SAME_RUNTIME_LIVE_WORKER_REQUIRED")
        identity = worker.require_current()
        sid = self._store.execution["sessionId"]
        # The full STS UserId can be enforced in an IAM resource ARN using
        # ${aws:userid}; an isolated instance-id substring cannot be extracted
        # from that policy variable by IAM.
        key = f"{PREFIX}workers/{sid}/registrations/{identity['principalId']}.json"
        value = dict(schemaVersion=2, executionReference=self._store.execution_reference,
                     intentReference=deepcopy(self._intent_ref), workerIdentity=identity)
        body = encoded(value); digest = hashlib.sha256(body).hexdigest()
        tags = {"techx-schema":"2", "techx-session-id":sid, "techx-worker-id":identity["instanceId"], "techx-boot-id":identity["bootId"]}
        try:
            response = self._store._put(key, body, tags, IfNoneMatch="*")
        except Exception as error:
            if self._store._service_error(error) != ("PreconditionFailed",412):
                raise RegistrationUncertain("REGISTRATION_WRITE_UNKNOWN_READ_SAME_OBJECT") from None
            # Existing registration is read-only reconciliation, never overwrite.
            try: raw, ref, _ = self._store._object(key)
            except Exception: raise RegistrationUncertain("REGISTRATION_READ_UNKNOWN") from None
            if raw != body: raise MutationConsumed("INSTANCE_ALREADY_REGISTERED_WITH_DIFFERENT_BOOT_OR_INTENT") from None
        else:
            try:
                self._store._write_response(response,digest)
                raw, ref, _ = self._store._object(key)
                if ref["versionId"] != response["VersionId"] or raw != body: raise ValueError("Registration superseded")
            except Exception: raise RegistrationUncertain("REGISTRATION_WRITE_UNKNOWN_READ_SAME_OBJECT") from None
        self._store._tags(ref,tags)
        worker.require_current()
        return dict(registrationReference=ref, workerIdentity=deepcopy(identity))
