"""Worker-owned single-consumption lease and private result publication.

This module does not execute untrusted shell or authorize a business stage.
The typed loader must validate operation/approval and supervise its subprocess
with ActiveTaskLease.require_current. R04/R07 integrate that actual process path.
"""
from copy import deepcopy
from datetime import timezone
import uuid
import threading

from execution_v2 import PREFIX, decode_object, fields, reference, text, timestamp
from recovery_v2 import checksum, service_time
from session_runtime_v2 import CASConflict, MutationConsumed, positive
from task_dispatch_v2 import TaskRuntime, result_tags
from worker_runtime_v2 import LiveWorker

_SEAL = object()


class ActiveTaskLease:
    """Only a fresh positive worker CAS can construct this process-local owner."""
    __slots__ = ("_runtime","_worker","_binding","_observed","_lease","_started_at","_finishing","_done","_process_consumed","_process_lock")
    def __init__(self,runtime,worker,binding,observed,lease,*,_seal=None):
        if _seal is not _SEAL: raise ValueError("Fresh same-process worker lease acknowledgement required")
        self._runtime,self._worker,self._binding,self._observed,self._lease = runtime,worker,binding,observed,lease
        self._started_at = runtime._tasks._clock().astimezone(timezone.utc).isoformat().replace("+00:00","Z")
        self._finishing,self._done = False,False
        self._process_consumed = False
        self._process_lock = threading.Lock()
    def as_dict(self):
        return dict(leaseReference=deepcopy(self._lease[1]),taskReference=deepcopy(self._observed._task_ref),
                    handle=deepcopy(self._observed._handle),workerIdentitySha256=checksum(self._worker.as_dict()),businessAcceptance="NOT_ESTABLISHED")
    def require_current(self):
        if self._finishing or self._done: raise MutationConsumed("Worker execution lease is finishing or consumed")
        self._runtime._guard(self._worker,self._binding,self._observed)
        current = self._runtime._tasks._read_lease(self._observed._envelope["task"]["purpose"])
        if current[1] != self._lease[1] or current[0] != self._lease[0] or current[0]["phase"] != "OWNED":
            raise CASConflict("Original worker no longer owns the exact task lease")
        return self.as_dict()


class WorkerTaskRuntime:
    def __init__(self,tasks):
        if not isinstance(tasks,TaskRuntime): raise ValueError("Owned task SDK runtime required")
        self._tasks = tasks

    def _guard(self,worker,binding,observed):
        if not isinstance(worker,LiveWorker): raise ValueError("Worker-owned IMDS/STS/boot identity required")
        actual = worker.require_current(); scope = binding.require_current(); task = observed._envelope["task"]
        store = self._tasks._store; store._caller()
        caller = store.clients["sts"].get_caller_identity(); positive(caller)
        if (store._role != f"{actual['purpose']}-executor" or store._principal != actual["principalArn"]
                or caller.get("UserId") != actual["principalId"] or worker._runtime._store.execution_reference != store.execution_reference
                or scope["workerIdentity"] != actual or checksum(actual) != observed._envelope["workerIdentitySha256"]
                or scope["registrationReference"] != observed._envelope["workerRegistrationReference"]
                or scope["leaseEpoch"] != task["leaseEpoch"] or any(actual[k] != task[k] for k in ("instanceId","purpose","executionReference"))):
            raise ValueError("Actual worker/task/boot/role binding mismatch")
        now = self._tasks._clock()
        if not timestamp(task["createdAt"]) <= now < timestamp(task["expiresAt"]): raise ValueError("Worker task dispatch/execution window closed")
        current = store.read().as_dict()
        if current["leaseEpoch"] != task["leaseEpoch"] or current["status"] not in {"PLANNING","PROVISIONING","READY","TESTING"}:
            raise ValueError("Worker task fenced by current session")
        latest = self._tasks.observe(observed._task_ref)
        if latest is None or latest._handle != observed._handle: raise ValueError("Original unique SSM command no longer observed")
        handle = latest._handle
        invocation = self._tasks._ssm.get_command_invocation(CommandId=handle["commandId"],InstanceId=task["instanceId"],PluginName="runTask")
        positive(invocation)
        if (invocation.get("CommandId") != handle["commandId"] or invocation.get("InstanceId") != task["instanceId"]
                or invocation.get("DocumentName") != task["documentName"] or invocation.get("DocumentVersion") != task["documentVersion"]
                or invocation.get("PluginName") != "runTask" or invocation.get("Status") != "InProgress"):
            raise ValueError("Actual in-progress SSM task invocation required")
        started = service_time(invocation.get("ExecutionStartDateTime"))
        if not latest._command["RequestedDateTime"] <= started <= now: raise ValueError("Actual SSM invocation start interval required")
        return actual

    def claim(self,worker,task_id):
        text(task_id,r"[a-f0-9]{32}")
        if not isinstance(worker,LiveWorker): raise ValueError("Actual typed worker required")
        identity = worker.require_current(); purpose = identity["purpose"]
        base = self._tasks._read_lease(purpose)
        if base[0] is None or base[0]["phase"] != "DISPATCHING": raise MutationConsumed("Task slot is not available for first ownership; observe or cleanup")
        envelope = self._tasks._read_task(base[0]["taskReference"])
        if envelope["task"]["taskId"] != task_id: raise ValueError("SSM TaskId differs from current canonical slot")
        binding = self._tasks._registry.registered_worker(purpose)
        observed = self._tasks.observe(base[0]["taskReference"])
        if observed is None: raise ValueError("Actual single SSM command not yet observed")
        self._guard(worker,binding,observed)
        self._tasks._document(envelope["configurationReference"],purpose)
        record = dict(base[0],phase="OWNED",ownerNonce=uuid.uuid4().hex)
        guard = lambda:self._guard(worker,binding,observed)
        accepted = self._tasks._change_existing_lease(base,record,guard,task_id)
        active = ActiveTaskLease(self,worker,binding,observed,accepted,_seal=_SEAL)
        active.require_current()
        return active

    def input_parameters(self,active):
        """Private exact input only; loader must still validate the stage schema."""
        if not isinstance(active,ActiveTaskLease) or active._runtime is not self: raise ValueError("Same-runtime active worker lease required")
        active.require_current()
        envelope = active._observed._envelope; task = envelope["task"]; ref = envelope["inputReference"]
        key = f"{PREFIX}inputs/{task['sessionId']}/{task['operation']}/{task['stage']}/{task['attempt']}.json"
        reference(ref,key=key)
        raw,_,_ = self._tasks._store._object(key,expected=ref)
        inputs = decode_object(raw,ref,key=key)
        fields(inputs,{"schemaVersion","executionReference","operation","stage","attempt","parameters"})
        if (type(inputs["schemaVersion"]) is not int or inputs["schemaVersion"] != 2 or type(inputs["attempt"]) is not int
                or any(inputs[k] != task[k] for k in ("executionReference","operation","stage","attempt")) or not isinstance(inputs["parameters"],dict)):
            raise ValueError("Original typed task input scope mismatch")
        active.require_current()
        return deepcopy(inputs["parameters"])

    def finish(self,active,evidence_reference):
        if not isinstance(active,ActiveTaskLease) or active._runtime is not self: raise ValueError("Same-runtime fresh worker ownership required")
        active.require_current()
        observed = active._observed; task = observed._envelope["task"]; identity = active._worker.as_dict()
        key = f"{PREFIX}evidence/{task['sessionId']}/{task['taskId']}.json"
        reference(evidence_reference,key=key)
        raw,_,_ = self._tasks._store._object(key,expected=evidence_reference)
        evidence = decode_object(raw,evidence_reference,key=key)
        fields(evidence,{"schemaVersion","executionReference","taskId","operation","stage","attempt","payload"})
        if (type(evidence["schemaVersion"]) is not int or evidence["schemaVersion"] != 2 or type(evidence["attempt"]) is not int
                or any(evidence[k] != task[k] for k in ("executionReference","taskId","operation","stage","attempt"))
                or not isinstance(evidence["payload"],dict)):
            raise ValueError("Private operation-specific task evidence binding mismatch")
        active.require_current()
        finished_at = self._tasks._clock().astimezone(timezone.utc).isoformat().replace("+00:00","Z")
        if service_time(finished_at) < service_time(active._started_at): raise ValueError("Worker receipt clock moved backwards")
        result = dict(schemaVersion=2,handle=deepcopy(observed._handle),workerIdentitySha256=checksum(identity),outcome="SUCCEEDED",
                      startedAt=active._started_at,finishedAt=finished_at,evidenceReference=deepcopy(evidence_reference))
        active._finishing = True  # A lost publication never reruns the operation.
        result_key = f"{PREFIX}results/{task['sessionId']}/{task['taskId']}/{observed._handle['commandId']}.json"
        result_ref = self._tasks._immutable(result_key,result,result_tags(observed._envelope,identity),task["taskId"],same=True)
        record = dict(active._lease[0],phase="FINISHED",resultReference=result_ref)
        guard = lambda:self._guard(active._worker,active._binding,observed)
        accepted = self._tasks._change_existing_lease(active._lease,record,guard,task["taskId"])
        active._done = True
        return dict(leaseReference=accepted[1],resultReference=result_ref,businessAcceptance="NOT_ESTABLISHED")
