"""Sensitive synchronous manager handoff with independent receiver verification.

Authentication is not launch/admission authority. A VerifiedHandoff must still
enter the trusted registry/lease/admission runtime before any resource mutation.
No token is placed in a receipt, ClientContext, log tail, async queue or output.
"""
from copy import deepcopy
import hashlib
import re

from execution_v2 import ACCOUNT, PREFIX, REGION, fields, integer, reference
from manager_bridge_v2 import (AuthenticatedManager, AuthenticationFailed, BINDINGS, CLEANUP, LIFECYCLE,
                               ManagerBridge, VerifiedManager, strict_json, token_claims)
from session_runtime_v2 import encoded, positive

REQUEST_FIELDS = {"requestId", "executionReference", "leaseEpoch", "operation", "intentReference", "workflow", "job"}
MUTATIONS = {"launch-deployment", "launch-probe", "arm-cleanup"}
OPERATIONS = MUTATIONS | {"request-cleanup", "observe"}
LIFECYCLE_OPS = {"launch-deployment", "launch-probe", "observe"}
CLEANUP_OPS = {"arm-cleanup", "request-cleanup", "observe"}
SOURCES = {"launch-deployment":{("staging-lifecycle.yml", "foundation-plan"), ("staging-continue.yml", "continue")},
           "launch-probe":{("staging-acceptance.yml", "public"), ("staging-continue.yml", "continue")},
           "arm-cleanup":{("staging-lifecycle.yml", "start")}}
_SEAL = object()


class HandoffRejected(ValueError):
    pass


class HandoffUncertain(ValueError):
    def __init__(self, request_id):
        self.request_id = request_id
        super().__init__("Handoff outcome unknown; observe the same durable request/intent, never replay a resource API")


def request_id(request):
    return hashlib.sha256(encoded({k:v for k,v in request.items() if k != "requestId"})).hexdigest()


def validate_request(request):
    fields(request, REQUEST_FIELDS)
    integer(request["leaseEpoch"])
    operation = request["operation"]
    if operation not in OPERATIONS or (request["workflow"], request["job"]) not in BINDINGS:
        raise HandoffRejected("UNSUPPORTED_HANDOFF_OPERATION")
    if operation in SOURCES and (request["workflow"], request["job"]) not in SOURCES[operation]:
        raise HandoffRejected("HANDOFF_JOB_SCOPE_MISMATCH")
    ref = request["executionReference"]
    if not isinstance(ref, dict): raise HandoffRejected("EXACT_HANDOFF_EXECUTION_REQUIRED")
    from execution_v2 import execution_reference
    sid = ref.get("key", "").removeprefix(PREFIX + "executions/").removesuffix(".json")
    execution_reference(ref, sid)
    intent = request["intentReference"]
    purpose = {"launch-deployment":"deployment", "launch-probe":"probe", "arm-cleanup":"cleanup"}.get(operation)
    if operation == "request-cleanup":
        if intent is not None: raise HandoffRejected("CLEANUP_REQUEST_CANNOT_SELECT_LAUNCH_INTENT")
    elif purpose:
        reference(intent, key=f"{PREFIX}launches/{sid}/{purpose}/1/intent.json")
    elif intent is not None:
        match = re.fullmatch(re.escape(f"{PREFIX}launches/{sid}/") + r"(deployment|probe|cleanup)/([1-3])/intent.json", str(intent.get("key", ""))) if isinstance(intent, dict) else None
        if not match or match[1] != "cleanup" and match[2] != "1": raise HandoffRejected("UNSCOPED_OBSERVATION_INTENT")
        reference(intent, key=intent["key"])
    if request["requestId"] != request_id(request): raise HandoffRejected("HANDOFF_DIGEST_MISMATCH")
    return deepcopy(request)


class SensitiveHandoff:
    __slots__ = ("_request", "_payload", "_sent", "_owner")

    def __init__(self, request, token, *, owner=None, _seal=None):
        if _seal is not _SEAL: raise HandoffRejected("USE_AUTHENTICATED_HANDOFF_BUILDER")
        self._request = deepcopy(request)
        self._payload = encoded(dict(schemaVersion=2, request=request, oidcToken=token))
        self._sent = False
        self._owner = owner
        if len(self._payload) > 32768: raise HandoffRejected("HANDOFF_TOO_LARGE")

    def __repr__(self): return f"<SensitiveHandoff requestId={self._request['requestId']}; payload redacted>"

    def as_dict(self): return deepcopy(self._request)  # No token/header/credentials.


def build_handoff(manager, fresh_token, *, operation, intent_reference=None, observe_epoch=None):
    if not isinstance(manager, AuthenticatedManager): raise HandoffRejected("AUTHENTICATED_MANAGER_REQUIRED")
    observed = manager.require_current(mutation=operation in MUTATIONS)
    identity = manager.as_dict()["identity"]
    claims = token_claims(fresh_token, identity["workflow"], identity["job"], manager._clock())
    if claims["run_id"] != identity["runId"] or claims["sha"] != identity["headSha"]:
        raise HandoffRejected("FRESH_TOKEN_DIFFERS_FROM_MANAGER")
    if observe_epoch is not None and operation != "observe": raise HandoffRejected("MUTATION_CANNOT_SELECT_HISTORICAL_EPOCH")
    epoch = observed.as_dict()["leaseEpoch"] if observe_epoch is None else integer(observe_epoch)
    value = dict(executionReference=manager.session_store.execution_reference, leaseEpoch=epoch, operation=operation,
                 intentReference=deepcopy(intent_reference), workflow=identity["workflow"], job=identity["job"])
    value["requestId"] = request_id(value)
    return SensitiveHandoff(validate_request(value), fresh_token, owner=manager, _seal=_SEAL)


class VerifiedHandoff:
    """In-process receiver identity only. It is NOT an admission/launch permit."""
    __slots__ = ("_manager", "_request", "_guard", "_invocation_id")

    def __init__(self, manager, request, guard, invocation_id, *, _seal=None):
        if _seal is not _SEAL or not isinstance(manager, VerifiedManager): raise HandoffRejected("USE_SERVER_VERIFICATION")
        self._manager, self._request, self._guard, self._invocation_id = manager, deepcopy(request), guard, invocation_id

    def as_dict(self):
        return dict(request=deepcopy(self._request), receiverInvocationId=self._invocation_id, admission="NOT_ESTABLISHED")

    @property
    def execution(self): return self._manager.execution

    @property
    def execution_reference(self): return self._manager.execution_reference

    def require_current(self):
        self._guard()
        snapshot = self._manager.require_current(mutation=self._request["operation"] in MUTATIONS)
        if self._request["operation"] != "observe" and snapshot.as_dict()["leaseEpoch"] != self._request["leaseEpoch"]:
            raise HandoffRejected("HANDOFF_FENCED_BY_CURRENT_EPOCH")
        return snapshot


class ServerManagerHandoff:
    """Construct with receiver-owned STS/GitHub providers, never event providers."""
    def __init__(self, bridge, sts, *, function_arn):
        if not isinstance(bridge, ManagerBridge) or function_arn not in (LIFECYCLE, CLEANUP): raise HandoffRejected("FIXED_RECEIVER_REQUIRED")
        self._bridge, self._sts, self._arn = bridge, sts, function_arn
        if (sts.meta.endpoint_url != f"https://sts.{REGION}.amazonaws.com" or sts.meta.region_name != REGION
                or sts.meta.config.retries.get("total_max_attempts") != 1): raise HandoffRejected("FIXED_RECEIVER_STS_REQUIRED")

    def _guard(self):
        name = self._arn.rsplit(":",1)[-1]
        result = self._sts.get_caller_identity(); positive(result)
        if (result.get("Account") != ACCOUNT or result.get("Arn") != f"arn:aws:sts::{ACCOUNT}:assumed-role/{name}/{name}"):
            raise HandoffRejected("ACTUAL_RECEIVER_EXECUTION_ROLE_REQUIRED")

    def verify(self, event, context):
        try:
            self._guard()
            if (getattr(context, "invoked_function_arn", None) != self._arn
                    or not isinstance(getattr(context, "aws_request_id", None), str) or not context.aws_request_id):
                raise HandoffRejected("ACTUAL_RECEIVER_INVOCATION_REQUIRED")
            fields(event, {"schemaVersion", "request", "oidcToken"})
            if type(event["schemaVersion"]) is not int or event["schemaVersion"] != 2 or len(encoded(event)) > 32768:
                raise HandoffRejected("INVALID_HANDOFF_ENVELOPE")
            request = validate_request(event["request"])
            if request["operation"] not in (LIFECYCLE_OPS if self._arn == LIFECYCLE else CLEANUP_OPS):
                raise HandoffRejected("WRONG_HANDOFF_RECEIVER")
            if request["operation"] == "observe" and request["intentReference"] is not None:
                cleanup_intent = "/cleanup/" in request["intentReference"]["key"]
                if cleanup_intent != (self._arn == CLEANUP): raise HandoffRejected("WRONG_OBSERVATION_RECEIVER")
            verified = self._bridge.verify(event["oidcToken"], request["executionReference"], workflow=request["workflow"], job=request["job"])
            result = VerifiedHandoff(verified, request, self._guard, context.aws_request_id, _seal=_SEAL)
            result.require_current()
            return result
        except (HandoffRejected, AuthenticationFailed): raise
        except Exception: raise HandoffRejected("SERVER_HANDOFF_VERIFICATION_FAILED") from None


def invoke_handoff(manager, packet):
    """One synchronous wakeup, never a resource-success receipt or a retry loop.

    The target runtime must durably reconcile the request/intent before effects.
    A restarted caller must observe that same request rather than call it anew.
    """
    if not isinstance(manager, AuthenticatedManager) or not isinstance(packet, SensitiveHandoff): raise HandoffRejected("TYPED_HANDOFF_REQUIRED")
    if packet._owner is not manager: raise HandoffRejected("HANDOFF_BELONGS_TO_ANOTHER_MANAGER")
    if packet._sent: raise HandoffRejected("HANDOFF_ALREADY_SENT_OBSERVE_ONLY")
    request = packet.as_dict()
    current = manager.require_current(mutation=request["operation"] in MUTATIONS)
    if request["operation"] != "observe" and current.as_dict()["leaseEpoch"] != request["leaseEpoch"]:
        raise HandoffRejected("HANDOFF_FENCED_BEFORE_TRANSPORT")
    # A fresh token must remain valid after slow caller checks too.
    envelope = strict_json(packet._payload)
    token_claims(envelope["oidcToken"], request["workflow"], request["job"], manager._clock())
    target = CLEANUP if request["operation"] in {"arm-cleanup", "request-cleanup"} or request["operation"] == "observe" and request["intentReference"] and "/cleanup/" in request["intentReference"]["key"] else LIFECYCLE
    client = manager.client("lambda")
    if (client.meta.endpoint_url != f"https://lambda.{REGION}.amazonaws.com" or client.meta.region_name != REGION
            or client.meta.config.retries.get("total_max_attempts") != 1): raise HandoffRejected("FIXED_NO_RETRY_LAMBDA_CLIENT_REQUIRED")
    packet._sent = True
    try:
        response = client.invoke(FunctionName=target, InvocationType="RequestResponse", LogType="None", Payload=packet._payload)
        stream = response.get("Payload") if isinstance(response, dict) else None
        try:
            positive(response)
            if response.get("StatusCode") != 200 or response.get("FunctionError") or not callable(getattr(stream,"read",None)):
                raise ValueError("No positive synchronous response")
            raw = stream.read(32769)
        finally:
            if callable(getattr(stream,"close",None)): stream.close()
        if not isinstance(raw,bytes) or len(raw) > 32768: raise ValueError("Unbounded response")
        value = strict_json(raw)
        fields(value, {"schemaVersion", "requestId", "executionReference", "leaseEpoch", "status"})
        if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2
                or value["requestId"] != request["requestId"] or value["executionReference"] != request["executionReference"]
                or type(value["leaseEpoch"]) is not int or value["leaseEpoch"] != request["leaseEpoch"] or value["status"] != "OBSERVATION_REQUIRED"):
            raise ValueError("No matching observation acknowledgement")
        return deepcopy(value)
    except Exception: raise HandoffUncertain(request["requestId"]) from None


def observation_ack(verified):
    if not isinstance(verified, VerifiedHandoff): raise HandoffRejected("VERIFIED_HANDOFF_REQUIRED")
    verified.require_current()
    request = verified.as_dict()["request"]
    return dict(schemaVersion=2, requestId=request["requestId"], executionReference=request["executionReference"],
                leaseEpoch=request["leaseEpoch"], status="OBSERVATION_REQUIRED")
