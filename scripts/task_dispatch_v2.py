"""Owned manager-side typed SSM dispatch and durable per-purpose task slot.

This is transport/physical serialization, not operation approval or business
acceptance. R06/R07 must authorize the stage/input before reaching this module.
Worker lease ownership/result publication is a separate in-process worker path;
this manager never steals an expired lease or synthesizes worker completion.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
import uuid

from execution_v2 import ACCOUNT, BUCKET, PREFIX, REGION, decode_object, fields, integer, reference, text, timestamp
from manager_bridge_v2 import AuthenticatedManager, SERVICE_ENDPOINTS
from recovery_v2 import checksum, validate_successful_result, wire_time
from session_runtime_v2 import CASConflict, MutationConsumed, SessionStore, encoded, positive
from worker_identity import OPERATIONS, validate_handle
from worker_registry_v2 import RegisteredWorker, WorkerRegistry

_SEAL = object()
TASK_FIELDS = {"schemaVersion","taskId","sessionId","executionReference","leaseEpoch","instanceId","purpose","operation","stage","attempt",
               "documentName","documentVersion","documentSha256","createdAt","expiresAt"}
ENVELOPE_FIELDS = {"schemaVersion","task","inputReference","configurationReference","workerRegistrationReference","workerIdentitySha256","managerIdentity","managerPrincipalId"}
LEASE_FIELDS = {"schemaVersion","executionReference","leaseEpoch","purpose","revision","mutationId","phase","taskReference","workerRegistrationReference",
                "workerIdentitySha256","ownerNonce","resultReference"}


class DispatchUnknown(ValueError):
    def __init__(self,task_id):
        self.task_id = task_id
        super().__init__("TASK_DISPATCH_UNKNOWN_OBSERVE_SAME_TASK_NEVER_RESEND")


def task_id(envelope):
    core = envelope["task"]
    return checksum(dict(task={k:v for k,v in core.items() if k not in {"taskId","createdAt","expiresAt"}},
        inputReference=envelope["inputReference"],configurationReference=envelope["configurationReference"],
        workerRegistrationReference=envelope["workerRegistrationReference"],workerIdentitySha256=envelope["workerIdentitySha256"]))[:32]


def task_tags(envelope):
    task = envelope["task"]
    return {"techx-schema":"2","techx-session-id":task["sessionId"],"techx-task-id":task["taskId"],
            "techx-execution-sha256":task["executionReference"]["sha256"],"techx-manager-id":envelope["managerPrincipalId"]}


def lease_tags(value):
    return {"techx-schema":"2","techx-execution-sha256":value["executionReference"]["sha256"],"techx-lease-epoch":str(value["leaseEpoch"]),
            "techx-lease-phase":value["phase"],"techx-lease-revision":str(value["revision"]),"techx-mutation-id":value["mutationId"]}


def result_tags(envelope,worker):
    task = envelope["task"]
    return {"techx-schema":"2","techx-task-id":task["taskId"],"techx-execution-sha256":task["executionReference"]["sha256"],
            "techx-worker-id":worker["instanceId"],"techx-boot-id":worker["bootId"],"techx-worker-principal":worker["principalId"]}


class PreparedDispatch:
    __slots__ = ("_runtime","_manager","_worker","_envelope","_base","_used")
    def __init__(self,runtime,manager,worker,envelope,base,*,_seal=None):
        if _seal is not _SEAL: raise ValueError("Use authenticated task preparation")
        self._runtime,self._manager,self._worker,self._envelope,self._base,self._used = runtime,manager,worker,deepcopy(envelope),base,False
    def as_dict(self): return deepcopy(self._envelope)


class ObservedDispatch:
    __slots__ = ("_runtime","_task_ref","_envelope","_worker","_command","_handle")
    def __init__(self,runtime,task_ref,envelope,worker,command,handle,*,_seal=None):
        if _seal is not _SEAL: raise ValueError("Use owned SSM observation")
        self._runtime,self._task_ref,self._envelope,self._worker,self._command,self._handle = runtime,deepcopy(task_ref),deepcopy(envelope),deepcopy(worker),deepcopy(command),deepcopy(handle)
    def as_dict(self): return dict(taskReference=deepcopy(self._task_ref),handle=deepcopy(self._handle),status=self._command["Status"],admission="NOT_ESTABLISHED")


class TaskRuntime:
    def __init__(self,clients,execution_ref,*,clock=lambda:datetime.now(timezone.utc)):
        self._store,self._clock = SessionStore(clients,execution_ref,clock=clock),clock
        self._registry = WorkerRegistry(clients,execution_ref,clock=clock)
        self._token = object()
        self._ssm = clients["ssm"]
        if (self._ssm.meta.endpoint_url != SERVICE_ENDPOINTS["ssm"] or self._ssm.meta.region_name != REGION
                or self._ssm.meta.config.retries.get("total_max_attempts") != 1): raise ValueError("Fixed no-retry regional SSM client required")

    def _manager(self,manager,task=None):
        if not isinstance(manager,AuthenticatedManager): raise ValueError("Actual authenticated manager required")
        manager.require_current(mutation=True); self._store._caller()
        identity = manager.as_dict()["identity"]
        if self._store._role != "manager" or self._store._principal != identity["principalArn"] or manager.execution_reference != self._store.execution_reference:
            raise ValueError("Task runtime must use the same actual manager credentials/execution")
        source = identity["workflow"],identity["job"]
        if task is not None:
            allowed = {("staging-acceptance.yml","public")} if task["purpose"] == "probe" else {("staging-lifecycle.yml","foundation-plan"),("staging-continue.yml","continue")}
            if source not in allowed: raise ValueError("Manager job cannot dispatch this worker purpose")
            if source == ("staging-lifecycle.yml","foundation-plan") and (task["operation"],task["stage"]) not in {("prepare","foundation"),("plan","foundation")}:
                raise ValueError("Initial plan job cannot dispatch apply or later stages")
            current = self._store.read().as_dict()
            if current["leaseEpoch"] != task["leaseEpoch"] or not timestamp(task["createdAt"]) <= self._clock() < timestamp(task["expiresAt"]):
                raise ValueError("Task expired or fenced")
        return identity

    def _task_key(self,tid):
        text(tid,r"[a-f0-9]{32}")
        return f"{PREFIX}tasks/{self._store.execution['sessionId']}/{tid}.json"

    def _lease_key(self,purpose):
        if purpose not in {"deployment","probe"}: raise ValueError("Autonomous cleanup uses its independent worker path")
        return f"{PREFIX}leases/{self._store.execution['sessionId']}/{purpose}.json"

    def _read_lease(self,purpose):
        self._store._caller(); key = self._lease_key(purpose)
        try: raw,ref,etag = self._store._object(key)
        except Exception as error:
            if self._store._service_error(error) != ("NoSuchKey",404): raise
            history = self._store.clients["s3"].list_object_versions(Bucket=BUCKET,Prefix=key,MaxKeys=100,ExpectedBucketOwner=ACCOUNT); positive(history)
            if (history.get("IsTruncated") is not False or history.get("NextKeyMarker") or history.get("NextVersionIdMarker")
                    or not isinstance(history.get("Versions",[]),list) or not isinstance(history.get("DeleteMarkers",[]),list)
                    or history.get("Versions",[]) or history.get("DeleteMarkers",[])):
                raise ValueError("Lost/partial lease history cannot become an idle worker")
            return None,None,None
        value = decode_object(raw,ref,key=key); fields(value,LEASE_FIELDS)
        if (type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["executionReference"] != self._store.execution_reference
                or value["purpose"] != purpose or value["phase"] not in {"DISPATCHING","OWNED","FINISHED"}): raise ValueError("Exact typed worker lease required")
        integer(value["leaseEpoch"]); integer(value["revision"]); text(value["mutationId"],r"[a-f0-9]{32}")
        text(value["workerIdentitySha256"],r"[a-f0-9]{64}")
        task_ref = value["taskReference"]
        if not isinstance(task_ref,dict) or not re.fullmatch(re.escape(f"{PREFIX}tasks/{self._store.execution['sessionId']}/")+r"[a-f0-9]{32}\.json",task_ref.get("key","")):
            raise ValueError("Canonical task lease reference required")
        reference(task_ref,key=task_ref["key"])
        envelope = self._read_task(task_ref); task = envelope["task"]
        if (task["purpose"] != purpose or task["leaseEpoch"] != value["leaseEpoch"]
                or value["workerRegistrationReference"] != envelope["workerRegistrationReference"]
                or value["workerIdentitySha256"] != envelope["workerIdentitySha256"]): raise ValueError("Lease differs from exact private task/worker binding")
        if value["phase"] == "DISPATCHING":
            if value["ownerNonce"] is not None or value["resultReference"] is not None: raise ValueError("Manager cannot invent worker ownership/result")
        else:
            text(value["ownerNonce"],r"[a-f0-9]{32}")
            if (value["phase"] == "OWNED") != (value["resultReference"] is None): raise ValueError("Lease result phase mismatch")
            if value["phase"] == "FINISHED":
                result = value["resultReference"]
                if not isinstance(result,dict) or not re.fullmatch(re.escape(f"{PREFIX}results/{task['sessionId']}/{task['taskId']}/")+r"[a-f0-9-]{36}\.json",result.get("key","")):
                    raise ValueError("Exact task result reference required")
                reference(result,key=result["key"])
        self._store._tags(ref,lease_tags(value))
        return value,ref,etag

    def _read_task(self,ref):
        if not isinstance(ref,dict): raise ValueError("Exact immutable task reference required")
        match = re.fullmatch(re.escape(f"{PREFIX}tasks/{self._store.execution['sessionId']}/")+r"([a-f0-9]{32})\.json",ref.get("key",""))
        if not match: raise ValueError("Task outside canonical session scope")
        reference(ref,key=self._task_key(match[1]))
        raw,_,_ = self._store._object(ref["key"],expected=ref)
        envelope = decode_object(raw,ref,key=ref["key"]); fields(envelope,ENVELOPE_FIELDS)
        task = envelope["task"]; fields(task,TASK_FIELDS)
        if (type(envelope["schemaVersion"]) is not int or envelope["schemaVersion"] != 2 or type(task["schemaVersion"]) is not int or task["schemaVersion"] != 2
                or task["executionReference"] != self._store.execution_reference or task["sessionId"] != self._store.execution["sessionId"]
                or task["taskId"] != match[1] or task_id(envelope) != task["taskId"]): raise ValueError("Immutable task identity mismatch")
        self._validate_core(task)
        text(envelope["workerIdentitySha256"],r"[a-f0-9]{64}")
        reference(envelope["configurationReference"],key=f"{PREFIX}config/{task['purpose']}.json")
        reference(envelope["inputReference"],key=f"{PREFIX}inputs/{task['sessionId']}/{task['operation']}/{task['stage']}/{task['attempt']}.json")
        identity = envelope["managerIdentity"]
        if (not isinstance(identity,dict) or identity.get("kind") != "github-manager" or identity.get("headSha") != self._store.execution["sourceRevisions"]["techx-infra"]):
            raise ValueError("Published manager source mismatch")
        run = text(identity.get("runId"),r"[1-9][0-9]{0,19}")
        text(envelope["managerPrincipalId"],rf"AROA[A-Z0-9]{{17}}:techx-v2-{run}-1")
        self._store._tags(ref,task_tags(envelope))
        return envelope

    def current_task(self,purpose):
        value,ref,_ = self._read_lease(purpose)
        if value is None: return None  # Unknown/idle observation, never a send grant.
        return dict(leaseReference=ref,phase=value["phase"],taskReference=deepcopy(value["taskReference"]))

    def find_task(self,tid):
        self._store._caller(); key = self._task_key(tid)
        try: _,ref,_ = self._store._object(key)
        except Exception as error:
            if self._store._service_error(error) == ("NoSuchKey",404): return None
            raise
        self._read_task(ref)
        return dict(taskReference=ref,taskId=tid,status="OBSERVATION_REQUIRED")

    @staticmethod
    def _validate_core(task):
        integer(task["leaseEpoch"]); integer(task["attempt"],maximum=999999 if task["operation"] in {"plan","apply"} else 1)
        if task["purpose"] not in {"deployment","probe"} or (task["operation"],task["stage"]) not in OPERATIONS:
            raise ValueError("Unknown typed deployment operation")
        if (task["purpose"] == "probe") != ((task["operation"],task["stage"]) == ("verify","public")) or task["operation"] == "cleanup":
            raise ValueError("Operation outside worker purpose")
        text(task["instanceId"],r"i-[a-f0-9]{17}")
        if task["documentName"] != f"techx-automation-v2-{task['purpose']}-task": raise ValueError("Fixed task document required")
        text(task["documentVersion"],r"[1-9][0-9]{0,8}"); text(task["documentSha256"],r"[a-f0-9]{64}")
        created,expires = timestamp(task["createdAt"]),timestamp(task["expiresAt"])
        if not created < expires or expires-created > timedelta(minutes=45): raise ValueError("Bounded typed task lifetime required")

    def _document(self,configuration_ref,purpose):
        reference(configuration_ref,key=f"{PREFIX}config/{purpose}.json")
        raw,_,_ = self._store._object(configuration_ref["key"],expected=configuration_ref)
        config = decode_object(raw,configuration_ref,key=configuration_ref["key"])
        spec = config.get("document"); fields(spec,{"name","version","sha256"})
        if spec["name"] != f"techx-automation-v2-{purpose}-task": raise ValueError("Fixed configured document required")
        text(spec["version"],r"[1-9][0-9]{0,8}"); text(spec["sha256"],r"[a-f0-9]{64}")
        result = self._ssm.describe_document(Name=spec["name"],DocumentVersion=spec["version"]); positive(result)
        document = result.get("Document",{})
        if (document.get("Name"),document.get("DocumentVersion"),document.get("Hash"),document.get("HashType"),document.get("Status"),
            document.get("DocumentType"),document.get("SchemaVersion"),document.get("Owner")) != (spec["name"],spec["version"],spec["sha256"],"Sha256","Active","Command","2.2",ACCOUNT):
            raise ValueError("Actual SSM document differs from reviewed pinned configuration")
        platforms,parameters = document.get("PlatformTypes"),document.get("Parameters")
        if (not isinstance(platforms,list) or not 1 <= len(platforms) <= 3 or any(p not in {"Linux","Windows","MacOS"} for p in platforms)
                or len(set(platforms)) != len(platforms) or "Linux" not in platforms or not isinstance(parameters,list) or len(parameters) != 1
                or parameters[0].get("Name") != "TaskId" or parameters[0].get("Type") != "String"):
            raise ValueError("Reviewed document must support Linux and only the typed TaskId parameter")
        return spec

    def _ready(self,instance):
        result = self._ssm.describe_instance_information(Filters=[dict(Key="InstanceIds",Values=[instance])]); positive(result)
        rows = result.get("InstanceInformationList")
        if result.get("NextToken") or not isinstance(rows,list) or len(rows) != 1: raise ValueError("Singleton SSM agent observation required")
        row = rows[0]; ping = row.get("LastPingDateTime")
        if (row.get("InstanceId") != instance or row.get("PingStatus") != "Online" or row.get("PlatformType") != "Linux"
                or row.get("ResourceType") != "EC2Instance" or not isinstance(ping,datetime) or ping.tzinfo is None
                or not self._clock()-timedelta(minutes=10) <= ping <= self._clock()+timedelta(seconds=30)):
            raise ValueError("Current online EC2 SSM agent required")

    def _previous_success(self,base):
        value = base[0]
        if value is None: return
        if value["phase"] != "FINISHED": raise MutationConsumed("Worker task slot active/uncertain; expiry never permits takeover")
        result = self.successful_result(value["taskReference"])
        if result is None or result["resultReference"] != value["resultReference"]: raise ValueError("Previous service terminal plus durable worker result required")

    def prepare(self,manager,*,purpose,operation,stage,attempt,input_reference):
        identity = self._manager(manager)
        self._lease_key(purpose)
        integer(attempt,maximum=999999 if operation in {"plan","apply"} else 1)
        if (operation,stage) not in OPERATIONS or (purpose == "probe") != ((operation,stage) == ("verify","public")) or operation == "cleanup":
            raise ValueError("Typed worker operation required before private input read")
        worker = self._registry.registered_worker(purpose)
        binding = worker.require_current(); actual = binding["workerIdentity"]
        spec = self._document(binding["configurationReference"],purpose)
        input_key = f"{PREFIX}inputs/{self._store.execution['sessionId']}/{operation}/{stage}/{attempt}.json"
        reference(input_reference,key=input_key)
        raw,_,_ = self._store._object(input_key,expected=input_reference)
        inputs = decode_object(raw,input_reference,key=input_key); fields(inputs,{"schemaVersion","executionReference","operation","stage","attempt","parameters"})
        if (type(inputs["schemaVersion"]) is not int or inputs["schemaVersion"] != 2 or inputs["executionReference"] != self._store.execution_reference
                or (inputs["operation"],inputs["stage"],inputs["attempt"]) != (operation,stage,attempt) or not isinstance(inputs["parameters"],dict)):
            raise ValueError("Private typed stage input mismatch")
        now = self._clock(); expires = min(now+timedelta(minutes=45),manager._job_deadline,timestamp(self._store.execution["cleanupStartAt"]))
        if expires-now < timedelta(seconds=60): raise ValueError("Insufficient bounded job/window time for dispatch")
        task = dict(schemaVersion=2,taskId="0"*32,sessionId=self._store.execution["sessionId"],executionReference=self._store.execution_reference,
            leaseEpoch=binding["leaseEpoch"],instanceId=actual["instanceId"],purpose=purpose,operation=operation,stage=stage,attempt=attempt,
            documentName=spec["name"],documentVersion=spec["version"],documentSha256=spec["sha256"],createdAt=wire_time(now),expiresAt=wire_time(expires))
        self._validate_core(task); self._manager(manager,task); self._ready(actual["instanceId"])
        caller = self._store.clients["sts"].get_caller_identity(); positive(caller)
        principal_id = text(caller.get("UserId"),rf"AROA[A-Z0-9]{{17}}:techx-v2-{identity['runId']}-1")
        envelope = dict(schemaVersion=2,task=task,inputReference=deepcopy(input_reference),configurationReference=binding["configurationReference"],
            workerRegistrationReference=binding["registrationReference"],workerIdentitySha256=checksum(actual),managerIdentity=identity,managerPrincipalId=principal_id)
        task["taskId"] = task_id(envelope)
        base = self._read_lease(purpose); self._previous_success(base)
        return PreparedDispatch(self,manager,worker,envelope,base,_seal=_SEAL)

    def _immutable(self,key,value,tags,tid,*,same=False):
        body = encoded(value)
        if len(body) > 32768: raise ValueError("Bounded immutable task metadata required")
        try: response = self._store._put(key,body,tags,IfNoneMatch="*")
        except Exception as error:
            if self._store._service_error(error) == ("PreconditionFailed",412):
                if not same: raise MutationConsumed("Task publication already consumed; observe original task") from None
                raw,ref,_ = self._store._object(key)
                if raw != body: raise ValueError("Immutable dispatch binding changed") from None
                self._store._tags(ref,tags); return ref
            raise DispatchUnknown(tid) from None
        try:
            self._store._write_response(response,checksum(value)); raw,ref,etag = self._store._object(key)
            if raw != body or ref["versionId"] != response["VersionId"] or etag != response["ETag"]: raise ValueError("Metadata write superseded")
            self._store._tags(ref,tags); return ref
        except Exception: raise DispatchUnknown(tid) from None

    def dispatch(self,prepared):
        if not isinstance(prepared,PreparedDispatch) or prepared._runtime is not self or prepared._used: raise ValueError("Same-runtime fresh prepared task required")
        envelope = prepared.as_dict(); task = envelope["task"]; tid = task["taskId"]
        self._manager(prepared._manager,task); prepared._worker.require_current()
        current = self._read_lease(task["purpose"])
        if current[1:] != prepared._base[1:]: raise CASConflict("Worker task slot changed")
        self._previous_success(current)
        prepared._used = True
        task_ref = self._immutable(self._task_key(tid),envelope,task_tags(envelope),tid)
        record = dict(schemaVersion=2,executionReference=self._store.execution_reference,leaseEpoch=task["leaseEpoch"],purpose=task["purpose"],
            revision=1 if current[0] is None else current[0]["revision"]+1,mutationId="0"*32,phase="DISPATCHING",taskReference=task_ref,
            workerRegistrationReference=envelope["workerRegistrationReference"],workerIdentitySha256=envelope["workerIdentitySha256"],ownerNonce=None,resultReference=None)
        record["mutationId"] = checksum(dict(base=current[1],task=task_ref,phase="DISPATCHING"))[:32]
        claim = dict(schemaVersion=2,baseLeaseReference=current[1],intendedRecord=record,claimNonce=uuid.uuid4().hex,principalArn=self._store._principal)
        claim_key = f"{PREFIX}task-mutations/{task['sessionId']}/{record['mutationId']}.json"
        self._immutable(claim_key,claim,lease_tags(record),tid)
        self._manager(prepared._manager,task); prepared._worker.require_current()
        if self._read_lease(task["purpose"])[1:] != current[1:]: raise CASConflict("Task slot changed after durable claim")
        try:
            response = self._store._put(self._lease_key(task["purpose"]),encoded(record),lease_tags(record),
                **(dict(IfNoneMatch="*") if current[1] is None else dict(IfMatch=current[2])))
            self._store._write_response(response,checksum(record)); accepted = self._read_lease(task["purpose"])
            if accepted[1]["versionId"] != response["VersionId"] or accepted[0] != record or accepted[2] != response["ETag"]: raise ValueError("Task lease write superseded")
        except Exception: raise DispatchUnknown(tid) from None
        self._manager(prepared._manager,task); prepared._worker.require_current(); self._document(envelope["configurationReference"],task["purpose"]); self._ready(task["instanceId"])
        if self._read_lease(task["purpose"])[1] != accepted[1]: raise CASConflict("Dispatch lease no longer current")
        remaining = int((timestamp(task["expiresAt"])-self._clock()).total_seconds())
        if remaining < 30: raise ValueError("Task has no remaining SSM dispatch window")
        self._manager(prepared._manager,task)
        try:
            response = self._ssm.send_command(InstanceIds=[task["instanceId"]],DocumentName=task["documentName"],DocumentVersion=task["documentVersion"],
                DocumentHash=task["documentSha256"],DocumentHashType="Sha256",Parameters={"TaskId":[tid]},Comment=f"techx-v2-task:{tid}",
                TimeoutSeconds=min(600,remaining),CloudWatchOutputConfig=dict(CloudWatchOutputEnabled=False))
            positive(response); command = response.get("Command",{})
            handle = self._handle(envelope,prepared._worker.as_dict()["workerIdentity"],command)
            self._immutable(f"{PREFIX}dispatches/{task['sessionId']}/{tid}/handle.json",dict(schemaVersion=2,taskReference=task_ref,handle=handle),task_tags(envelope),tid,same=True)
            return ObservedDispatch(self,task_ref,envelope,prepared._worker.as_dict()["workerIdentity"],command,handle,_seal=_SEAL)
        except Exception: raise DispatchUnknown(tid) from None

    def _handle(self,envelope,worker,command):
        task = envelope["task"]
        handle = {k:deepcopy(task[k]) for k in TASK_FIELDS-{"sessionId","purpose","createdAt","expiresAt"}}
        handle["commandId"] = command.get("CommandId")
        validate_handle(handle,task,worker,command=command,now=self._clock())
        if command.get("Comment") != f"techx-v2-task:{task['taskId']}": raise ValueError("Command does not match typed task correlation")
        return handle

    def _original_worker(self,envelope):
        ref = envelope["workerRegistrationReference"]; task = envelope["task"]
        prefix = f"{PREFIX}workers/{task['sessionId']}/registrations/"
        if not isinstance(ref,dict) or not re.fullmatch(re.escape(prefix)+rf"AROA[A-Z0-9]{{17}}:{task['instanceId']}\.json",ref.get("key","")):
            raise ValueError("Original private worker registration required")
        reference(ref,key=ref["key"]); raw,_,_ = self._store._object(ref["key"],expected=ref)
        registration = decode_object(raw,ref,key=ref["key"])
        fields(registration,{"schemaVersion","executionReference","intentReference","workerIdentity"})
        worker = registration["workerIdentity"]
        if (type(registration["schemaVersion"]) is not int or registration["schemaVersion"] != 2 or not isinstance(worker,dict)
                or registration["executionReference"] != self._store.execution_reference or checksum(worker) != envelope["workerIdentitySha256"]
                or worker.get("configurationReference") != envelope["configurationReference"]
                or any(worker.get(k) != task[k] for k in ("instanceId","purpose","executionReference"))): raise ValueError("Original worker/task mismatch")
        if ref["key"] != f"{prefix}{worker.get('principalId')}.json": raise ValueError("Original worker principal-key binding mismatch")
        text(worker.get("bootId"),r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
        self._store._tags(ref,{"techx-schema":"2","techx-session-id":task["sessionId"],"techx-worker-id":task["instanceId"],"techx-boot-id":worker["bootId"]})
        return worker

    def observe(self,task_reference):
        self._store._caller(); envelope = self._read_task(task_reference); task = envelope["task"]; worker = self._original_worker(envelope)
        seen,tokens,found = set(),set(),[]; token = None
        for _ in range(100):
            args = dict(InstanceId=task["instanceId"],MaxResults=50,Filters=[dict(key="DocumentName",value=task["documentName"]),
                dict(key="InvokedAfter",value=task["createdAt"]),dict(key="InvokedBefore",value=task["expiresAt"])])
            if token: args["NextToken"] = token
            response = self._ssm.list_commands(**args); positive(response)
            commands = response.get("Commands")
            if not isinstance(commands,list): raise ValueError("Complete SSM command page required")
            for command in commands:
                cid = command.get("CommandId") if isinstance(command,dict) else None
                if (not isinstance(cid,str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}",cid) or cid in seen
                        or not isinstance(command.get("Parameters"),dict) or not isinstance(command.get("Comment"),str)):
                    raise ValueError("Repeated/incomplete SSM command observation")
                seen.add(cid)
                if command.get("Parameters") == {"TaskId":[task["taskId"]]}:
                    handle = self._handle(envelope,worker,command); found.append((command,handle))
            token = response.get("NextToken")
            if not token: break
            if not isinstance(token,str) or token in tokens: raise ValueError("SSM command pagination cycle")
            tokens.add(token)
        else: raise ValueError("SSM command inventory incomplete")
        if len(found)>1: raise ValueError("Duplicate task commands are not a recoverable single dispatch")
        if not found: return None  # Unknown, NEVER permission to resend.
        command,handle = found[0]
        return ObservedDispatch(self,task_reference,envelope,worker,command,handle,_seal=_SEAL)

    def successful_result(self,task_reference):
        observed = self.observe(task_reference)
        if observed is None or observed._command["Status"] != "Success": return None
        task,handle = observed._envelope["task"],observed._handle
        invocation = self._ssm.get_command_invocation(CommandId=handle["commandId"],InstanceId=task["instanceId"],PluginName="runTask"); positive(invocation)
        key = f"{PREFIX}results/{task['sessionId']}/{task['taskId']}/{handle['commandId']}.json"
        raw,ref,_ = self._store._object(key)
        self._store._tags(ref,result_tags(observed._envelope,observed._worker))
        result = validate_successful_result(raw,ref,handle,task,observed._worker,command=observed._command,invocation=invocation,now=self._clock())
        return dict(resultReference=ref,result=result,businessAcceptance="NOT_ESTABLISHED")
