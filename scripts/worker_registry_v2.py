"""SDK-backed bounded worker roster and durable launch-call consumption.

This registry is not admission or RunInstances authority. Only a fresh same-
process CALLING acknowledgement can be passed to the future admitted launcher;
recovered records and exported dictionaries are observation-only.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re
import uuid

from execution_v2 import (ACCOUNT, PREFIX, REGION, decode_object, execution_reference, fields, integer, reference, text, timestamp, validate_execution)
from manager_handoff_v2 import VerifiedHandoff
from recovery_v2 import begin_launch, checksum, prepare_launch, service_time, validate_checkpoint, wire_time
from session_runtime_v2 import (CASConflict, MutationConsumed, SessionStore, WriteSuperseded, encoded, positive)
from worker_identity import validate_launch_intent, worker_identity

HEADER = {"schemaVersion", "executionReference", "leaseEpoch", "runtimeRevision", "lastMutationId", "launches"}
ROW = {"purpose", "attempt", "launchEpoch", "phase", "launchToken", "intentReference", "checkpoint",
       "instanceId", "volumeIds", "networkInterfaceIds", "registrationReference", "terminalReference"}
_SEAL = object()
ORDER = {"deployment":0,"cleanup":1,"probe":2}


def slot_order(row): return ORDER[row["purpose"]],row["attempt"]


def validate_registry(value, execution, ref):
    """Strict rich roster; bare R01 four-field snapshots are not runtime state."""
    validate_execution(execution); execution_reference(ref,execution["sessionId"])
    fields(value, HEADER)
    if len(encoded(value)) > 32768: raise ValueError("Bounded registry bytes required")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 2 or value["executionReference"] != ref:
        raise ValueError("Registry execution mismatch")
    integer(value["leaseEpoch"]); integer(value["runtimeRevision"])
    text(value["lastMutationId"], r"[a-f0-9]{32}")
    rows = value["launches"]
    if not isinstance(rows,list) or not 1 <= len(rows) <= 5: raise ValueError("Bounded nonempty complete roster required")
    slots, tokens, instances, volumes, interfaces = set(), set(), set(), set(), set()
    for row in rows:
        fields(row, ROW)
        if row["purpose"] not in {"deployment","cleanup","probe"}: raise ValueError("Unknown worker purpose")
        integer(row["attempt"],maximum=3 if row["purpose"] == "cleanup" else 1)
        integer(row["launchEpoch"],maximum=value["leaseEpoch"])
        slot = (row["purpose"],row["attempt"])
        text(row["launchToken"],r"[a-f0-9]{64}")
        if slot in slots or row["launchToken"] in tokens: raise ValueError("Duplicate worker slot/token")
        slots.add(slot); tokens.add(row["launchToken"])
        reference(row["intentReference"],key=f"{PREFIX}launches/{execution['sessionId']}/{row['purpose']}/{row['attempt']}/intent.json")
        checkpoint = validate_checkpoint(row["checkpoint"],dict(clientToken=row["launchToken"]))
        if row["phase"] != checkpoint["phase"] or row["instanceId"] != checkpoint["instanceId"]:
            raise ValueError("Roster and checkpoint differ")
        if row["phase"] != "OBSERVED":
            if any(row[k] is not None for k in ("instanceId","registrationReference","terminalReference")) or row["volumeIds"] != [] or row["networkInterfaceIds"] != []:
                raise ValueError("Unobserved launch cannot invent compute/evidence")
            continue
        instance = text(row["instanceId"],r"i-[a-f0-9]{17}")
        if instance in instances: raise ValueError("Duplicate registered instance")
        instances.add(instance)
        registration = row["registrationReference"]
        if not isinstance(registration,dict): raise ValueError("Exact private boot registration required")
        key = registration.get("key", "")
        if not re.fullmatch(re.escape(f"{PREFIX}workers/{execution['sessionId']}/registrations/") + rf"AROA[A-Z0-9]{{17}}:{instance}\.json",key):
            raise ValueError("Registration belongs to another instance")
        reference(registration,key=key)
        if row["terminalReference"] is not None:
            reference(row["terminalReference"],key=f"{PREFIX}cleanup/{execution['sessionId']}/terminals/{instance}.json")
        for name,pattern,seen in (("volumeIds",r"vol-[a-f0-9]{17}",volumes),("networkInterfaceIds",r"eni-[a-f0-9]{17}",interfaces)):
            assets = row[name]
            if not isinstance(assets,list) or not 1 <= len(assets) <= 8: raise ValueError("Complete original worker assets required")
            for asset in assets:
                text(asset,pattern)
                if asset in seen: raise ValueError("Duplicate worker asset")
                seen.add(asset)
    if rows != sorted(rows,key=slot_order): raise ValueError("Canonical worker slot order required")
    return deepcopy(value)


def registry_tags(value, ref):
    return {"techx-schema":"2", "techx-backend":"ec2-ssm", "techx-session-id":value["executionReference"]["key"].split("/")[-1][:-5],
            "techx-execution-sha256":ref["sha256"],"techx-lease-epoch":str(value["leaseEpoch"]),
            "techx-runtime-revision":str(value["runtimeRevision"]),"techx-mutation-id":value["lastMutationId"]}


class RegistryUncertain(ValueError):
    def __init__(self, mutation_id):
        self.mutation_id = mutation_id
        super().__init__("REGISTRY_WRITE_UNKNOWN_RECOVER_SAME_MUTATION_NO_REPLAY")


class RegistryObservation:
    __slots__ = ("_store","_record","_ref","_etag")
    def __init__(self, store, record, ref, etag, *, _seal=None):
        if _seal is not _SEAL: raise ValueError("Use registry SDK reader")
        self._store,self._record,self._ref,self._etag = store,deepcopy(record),deepcopy(ref),etag
    def as_dict(self): return deepcopy(self._record)
    @property
    def reference(self): return deepcopy(self._ref)


class RegistryMutation:
    __slots__ = ("_store","_base","_record","_guard","_state","_nonce")
    def __init__(self,store,base,record,guard,*,_seal=None):
        if _seal is not _SEAL: raise ValueError("Use registry preparation")
        self._store,self._base,self._record,self._guard = store,base,deepcopy(record),guard
        self._state,self._nonce = "PREPARED",uuid.uuid4().hex
    def as_dict(self): return deepcopy(self._record)


class FreshCalling:
    """Storage acknowledgement ONLY; admission/config/RunInstances guard is separate."""
    __slots__ = ("_registry","_observation","_slot","_consumed","_guard")
    def __init__(self,registry,observation,slot,guard,*,_seal=None):
        if _seal is not _SEAL: raise ValueError("Fresh CALLING write required")
        self._registry,self._observation,self._slot,self._consumed,self._guard = registry,observation,slot,False,guard
    def as_dict(self):
        return dict(registryReference=self._observation.reference,purpose=self._slot[0],attempt=self._slot[1],admission="NOT_ESTABLISHED")
    def take(self):
        if self._consumed: raise MutationConsumed("Fresh CALLING acknowledgement already consumed")
        self._guard(); self._registry._same_head(self._observation)
        self._consumed = True
        return self.as_dict()  # Still not admission/config authorization.


class WorkerRegistry:
    def __init__(self,clients,execution_ref,*,clock=lambda:datetime.now(timezone.utc)):
        self._session = SessionStore(clients,execution_ref,clock=clock)
        self._clock,self._token = clock,object()
        self.key = f"{PREFIX}workers/{self._session.execution['sessionId']}/registry.json"

    def _intent(self,ref,epoch):
        prefix = f"{PREFIX}launches/{self._session.execution['sessionId']}/"
        key = ref.get("key", "") if isinstance(ref,dict) else ""
        match = re.fullmatch(re.escape(prefix)+r"(deployment|cleanup|probe)/([1-3])/intent.json",key)
        if not match or match[1] != "cleanup" and match[2] != "1": raise ValueError("Exact scoped launch intent required before SDK read")
        reference(ref,key=key)
        raw,_,_ = self._session._object(ref["key"],expected=ref)
        intent = decode_object(raw,ref,key=ref["key"])
        validate_launch_intent(intent,self._session.execution,self._session.execution_reference,epoch)
        reference(ref,key=f"{PREFIX}launches/{self._session.execution['sessionId']}/{intent['purpose']}/{intent['attempt']}/intent.json")
        return intent

    def read(self):
        self._session._caller()
        try: raw,ref,etag = self._session._object(self.key)
        except Exception as error:
            if self._session._service_error(error) == ("NoSuchKey",404):
                history = self._session.clients["s3"].list_object_versions(Bucket=self._session.execution_reference["bucket"],
                    Prefix=self.key,MaxKeys=100,ExpectedBucketOwner=ACCOUNT)
                positive(history)
                if (history.get("IsTruncated") is not False or history.get("NextKeyMarker") or history.get("NextVersionIdMarker")
                        or not isinstance(history.get("Versions",[]),list) or not isinstance(history.get("DeleteMarkers",[]),list)
                        or history.get("Versions",[]) or history.get("DeleteMarkers",[])):
                    raise ValueError("Absent registry has history or incomplete listing; never recreate a lost roster")
                return RegistryObservation(self._token,None,None,None,_seal=_SEAL)
            raise
        value = validate_registry(decode_object(raw,ref,key=self.key),self._session.execution,self._session.execution_reference)
        if value["leaseEpoch"] > self._session.read().as_dict()["leaseEpoch"]: raise ValueError("Registry ahead of current session fence")
        self._session._tags(ref,registry_tags(value,self._session.execution_reference))
        for row in value["launches"]:
            intent = self._intent(row["intentReference"],row["launchEpoch"])
            if (intent["purpose"],intent["attempt"],intent["clientToken"]) != (row["purpose"],row["attempt"],row["launchToken"]):
                raise ValueError("Stored roster intent binding changed")
        return RegistryObservation(self._token,value,ref,etag,_seal=_SEAL)

    def _base(self,base):
        if not isinstance(base,RegistryObservation) or base._store is not self._token: raise ValueError("Same-store SDK observation required")
        return base.as_dict()

    def _launch_guard(self,handoff,intent_ref,intent):
        self._session._caller()
        if not isinstance(handoff,VerifiedHandoff): raise ValueError("Receiver-verified manager handoff required")
        expected = {"deployment":("lifecycle-launcher","launch-deployment"),"probe":("lifecycle-launcher","launch-probe"),
                    "cleanup":("cleanup-launcher","arm-cleanup")}[intent["purpose"]]
        request = handoff.as_dict()["request"]
        if (self._session._role,request["operation"]) != expected or request["intentReference"] != intent_ref or handoff.execution_reference != self._session.execution_reference:
            raise ValueError("Actual launcher/handoff purpose or intent mismatch")
        handoff.require_current()
        current = self._session.read().as_dict()
        now,execution = self._clock(),self._session.execution
        if (current["leaseEpoch"] != intent["leaseEpoch"] or request["leaseEpoch"] != intent["leaseEpoch"]
                or current["status"] not in {"PLANNING","PROVISIONING","READY","TESTING"}
                or not timestamp(execution["startedAt"]) <= now < timestamp(execution["cleanupStartAt"])):
            raise ValueError("Launch fenced or mutation window closed")
        return current["leaseEpoch"]

    @staticmethod
    def _mutation_id(base,record):
        target = {k:deepcopy(v) for k,v in record.items() if k != "lastMutationId"}
        # Re-preparing in a new process/time must consume the original semantic
        # slot, not mint another claim for a lost CALLING-write response.
        for row in target["launches"]:
            row["checkpoint"]["callStartedAt"] = None
            row["checkpoint"]["observedAt"] = None
        return checksum(dict(baseRegistryReference=base,target=target))[:32]

    def _prepare(self,base,record,guard):
        self._base(base); guard()
        record["runtimeRevision"] = 1 if base._record is None else base._record["runtimeRevision"]+1
        record["lastMutationId"] = self._mutation_id(base.reference,record)
        validate_registry(record,self._session.execution,self._session.execution_reference)
        self._transition(base.as_dict(),record)
        return RegistryMutation(self._token,base,record,guard,_seal=_SEAL)

    @staticmethod
    def _transition(old,new):
        previous = [] if old is None else old["launches"]
        if new["runtimeRevision"] != (1 if old is None else old["runtimeRevision"]+1): raise ValueError("Registry revision must advance once")
        if old is not None and (new["executionReference"] != old["executionReference"] or new["leaseEpoch"] < old["leaseEpoch"]):
            raise ValueError("Registry scope/fence cannot rewind")
        before = {(r["purpose"],r["attempt"]):r for r in previous}
        after = {(r["purpose"],r["attempt"]):r for r in new["launches"]}
        if set(before)-set(after): raise ValueError("A worker generation may never be dropped")
        added = set(after)-set(before)
        changed = [slot for slot in before if before[slot] != after[slot]]
        if len(added) == 1 and not changed:
            row = after[next(iter(added))]
            if row["phase"] != "PREPARED" or row["attempt"] != 1 or row["launchEpoch"] != new["leaseEpoch"]:
                raise ValueError("Only a new first-attempt prepared slot may be reserved")
            if old is not None and new["leaseEpoch"] != old["leaseEpoch"]: raise ValueError("Reservation cannot advance a fence")
            return "reserve",row["purpose"]
        if not added and not changed and old is not None and new["leaseEpoch"] > old["leaseEpoch"]:
            return "fence","cleanup"
        if added or len(changed) != 1: raise ValueError("Exactly one monotonic worker transition required")
        slot = changed[0]; a,b = before[slot],after[slot]
        immutable = {"purpose","attempt","launchEpoch","launchToken","intentReference","terminalReference"}
        if any(a[k] != b[k] for k in immutable): raise ValueError("Worker generation binding is immutable")
        if (a["phase"],b["phase"]) == ("PREPARED","CALLING"):
            if old["leaseEpoch"] != new["leaseEpoch"] or a["launchEpoch"] != new["leaseEpoch"]: raise ValueError("CALLING cannot cross a fence")
            return "begin",b["purpose"]
        if (a["phase"],b["phase"]) == ("CALLING","OBSERVED") and a["checkpoint"]["callStartedAt"] == b["checkpoint"]["callStartedAt"]:
            return "observe",b["purpose"]
        raise ValueError("Launch checkpoint may not reset or skip a phase")

    def reserve(self,base,intent_ref,handoff):
        old = self._base(base)
        if not isinstance(handoff,VerifiedHandoff): raise ValueError("Receiver-verified manager handoff required")
        request = handoff.as_dict()["request"]
        intent = self._intent(intent_ref,request.get("leaseEpoch"))
        guard = lambda:self._launch_guard(handoff,intent_ref,intent)
        epoch = guard()
        if intent["attempt"] != 1: raise ValueError("Replacement requires the separate terminal-proof supervisor")
        record = old or dict(schemaVersion=2,executionReference=self._session.execution_reference,leaseEpoch=epoch,
                             runtimeRevision=1,lastMutationId="0"*32,launches=[])
        if record["leaseEpoch"] != epoch: raise ValueError("Registry fence differs from current launch")
        if any(row["purpose"] == intent["purpose"] for row in record["launches"]): raise MutationConsumed("Worker purpose already reserved; observe original slot")
        checkpoint = prepare_launch(intent,self._session.execution,self._session.execution_reference,epoch)
        record["launches"].append(dict(purpose=intent["purpose"],attempt=1,launchEpoch=epoch,phase="PREPARED",launchToken=intent["clientToken"],
            intentReference=deepcopy(intent_ref),checkpoint=checkpoint,instanceId=None,volumeIds=[],networkInterfaceIds=[],registrationReference=None,terminalReference=None))
        record["launches"].sort(key=slot_order)
        return self._prepare(base,record,guard)

    def begin(self,base,purpose,handoff):
        record = self._base(base)
        if record is None: raise ValueError("No reserved worker")
        rows = [row for row in record["launches"] if row["purpose"] == purpose and row["attempt"] == 1]
        if len(rows) != 1: raise ValueError("Exact reserved first-attempt slot required")
        row = rows[0]; intent = self._intent(row["intentReference"],row["launchEpoch"])
        guard = lambda:self._launch_guard(handoff,row["intentReference"],intent)
        epoch = guard()
        row["checkpoint"] = begin_launch(row["checkpoint"],intent,self._session.execution,self._session.execution_reference,current_epoch=epoch,now=self._clock())
        row["phase"] = "CALLING"
        return self._prepare(base,record,guard)

    def align_fence(self,base):
        record = self._base(base)
        if record is None: raise ValueError("No worker roster to fence")
        def guard():
            self._session._caller()
            current = self._session.read().as_dict()
            if (self._session._role != "cleanup-launcher" or current["status"] != "DESTROYING"
                    or current["leaseEpoch"] <= base._record["leaseEpoch"]):
                raise ValueError("Actual cleanup role and strictly newer DESTROYING epoch required")
            return current["leaseEpoch"]
        epoch = guard(); record["leaseEpoch"] = epoch
        def fixed_guard():
            if guard() != epoch: raise ValueError("Cleanup fence advanced during registry write")
        return self._prepare(base,record,fixed_guard)

    def _registered(self,row,registration_ref):
        """Own service reads, never an event's EC2/STS response dictionary."""
        self._session._caller()
        if self._session._role not in {"lifecycle-launcher","cleanup-launcher"} or self._session._role == "lifecycle-launcher" and row["purpose"] == "cleanup":
            raise ValueError("Actual launcher role cannot register this worker")
        for service,endpoint in (("ec2",f"https://ec2.{REGION}.amazonaws.com"),("iam","https://iam.amazonaws.com")):
            client = self._session.clients[service]
            if client.meta.endpoint_url != endpoint or client.meta.region_name != REGION or client.meta.config.retries.get("total_max_attempts") != 1:
                raise ValueError("Fixed no-retry regional service clients required")
        prefix = f"{PREFIX}workers/{self._session.execution['sessionId']}/registrations/"
        if not isinstance(registration_ref,dict) or not isinstance(registration_ref.get("key"),str) or not re.fullmatch(re.escape(prefix)+r"AROA[A-Z0-9]{17}:i-[a-f0-9]{17}\.json",registration_ref["key"]):
            raise ValueError("Exact worker registration reference required")
        reference(registration_ref,key=registration_ref["key"])
        raw,_,_ = self._session._object(registration_ref["key"],expected=registration_ref)
        registration = decode_object(raw,registration_ref,key=registration_ref["key"])
        fields(registration,{"schemaVersion","executionReference","intentReference","workerIdentity"})
        if (type(registration["schemaVersion"]) is not int or registration["schemaVersion"] != 2
                or registration["executionReference"] != self._session.execution_reference or registration["intentReference"] != row["intentReference"]):
            raise ValueError("Registration execution/intent mismatch")
        identity = registration["workerIdentity"]
        if not isinstance(identity,dict): raise ValueError("Private registered identity required")
        instance_id = text(identity.get("instanceId"),r"i-[a-f0-9]{17}")
        purpose = row["purpose"]
        profile = self._session.clients["iam"].get_instance_profile(InstanceProfileName=f"techx-automation-v2-{purpose}-executor")
        positive(profile)
        observed_profile = profile.get("InstanceProfile",{})
        expected_profile = f"arn:aws:iam::{ACCOUNT}:instance-profile/techx-automation-v2-{purpose}-executor"
        roles = observed_profile.get("Roles")
        if (observed_profile.get("Arn") != expected_profile or not isinstance(roles,list) or len(roles) != 1
                or roles[0].get("Arn") != expected_profile.replace(":instance-profile/",":role/")):
            raise ValueError("Actual worker profile role changed")
        role_id = text(roles[0].get("RoleId"),r"AROA[A-Z0-9]{17}")
        principal_id = f"{role_id}:{instance_id}"
        if registration_ref["key"] != f"{prefix}{principal_id}.json" or identity.get("principalId") != principal_id:
            raise ValueError("Registration is not bound to actual role unique ID/instance")
        boot = text(identity.get("bootId"),r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
        self._session._tags(registration_ref,{"techx-schema":"2","techx-session-id":self._session.execution["sessionId"],
                            "techx-worker-id":instance_id,"techx-boot-id":boot})
        response = self._session.clients["ec2"].describe_instances(InstanceIds=[instance_id]); positive(response)
        reservations = response.get("Reservations")
        if (response.get("NextToken") or not isinstance(reservations,list) or len(reservations) != 1
                or reservations[0].get("OwnerId") != ACCOUNT or not isinstance(reservations[0].get("Instances"),list)
                or len(reservations[0]["Instances"]) != 1 or reservations[0]["Instances"][0].get("InstanceId") != instance_id):
            raise ValueError("Positive singleton owned EC2 registration required")
        instance = reservations[0]["Instances"][0]
        options = instance.get("MetadataOptions",{})
        if options.get("HttpEndpoint") != "enabled" or type(options.get("HttpPutResponseHopLimit")) is not int or options["HttpPutResponseHopLimit"] != 1:
            raise ValueError("Actual worker metadata endpoint/hop boundary changed")
        intent = self._intent(row["intentReference"],row["launchEpoch"])
        # The original record is writable only by this exact aws:userid path
        # under R05. Its role/session claim is additionally bound to actual IAM
        # profile membership and current EC2 observations here.
        core = worker_identity(intent,instance,dict(Account=ACCOUNT,Arn=identity.get("principalArn")),
                               self._session.execution,self._session.execution_reference,row["launchEpoch"])
        if identity != dict(core,bootId=boot,principalId=principal_id): raise ValueError("Registered boot/service identity differs")
        if not timestamp(row["checkpoint"]["callStartedAt"]) <= service_time(identity["launchedAt"]) <= self._clock():
            raise ValueError("Registered launch time differs from durable CALLING interval")
        volumes,interfaces = [],[]
        for mapping in instance.get("BlockDeviceMappings",[]):
            ebs = mapping.get("Ebs",{})
            if ebs.get("DeleteOnTermination") is not True or ebs.get("Status") != "attached": raise ValueError("Original EBS lifecycle not verified")
            volumes.append(text(ebs.get("VolumeId"),r"vol-[a-f0-9]{17}"))
        for interface in instance.get("NetworkInterfaces",[]):
            if interface.get("Attachment",{}).get("DeleteOnTermination") is not True or interface.get("Status") != "in-use":
                raise ValueError("Original ENI lifecycle not verified")
            interfaces.append(text(interface.get("NetworkInterfaceId"),r"eni-[a-f0-9]{17}"))
        if not 1 <= len(volumes) <= 8 or not 1 <= len(interfaces) <= 8 or len(set(volumes)) != len(volumes) or len(set(interfaces)) != len(interfaces):
            raise ValueError("Complete unique original worker assets required")
        current = self._session.read().as_dict()
        if current["leaseEpoch"] < row["launchEpoch"]: raise ValueError("Observed launch is ahead of current session fence")
        return identity,sorted(volumes),sorted(interfaces),current["leaseEpoch"]

    def observe(self,base,purpose,attempt,registration_ref):
        record = self._base(base)
        if record is None: raise ValueError("No durable CALLING roster")
        rows = [row for row in record["launches"] if (row["purpose"],row["attempt"]) == (purpose,attempt)]
        if len(rows) != 1 or rows[0]["phase"] != "CALLING": raise ValueError("Only a CALLING launch may acquire positive observation")
        row = rows[0]; original = deepcopy(row)
        identity,volumes,interfaces,epoch = self._registered(original,registration_ref)
        def guard():
            current = self._registered(original,registration_ref)
            if current != (identity,volumes,interfaces,epoch): raise ValueError("Worker observation changed before registry commit")
        row.update(phase="OBSERVED",instanceId=identity["instanceId"],volumeIds=volumes,networkInterfaceIds=interfaces,
                   registrationReference=deepcopy(registration_ref))
        row["checkpoint"].update(phase="OBSERVED",instanceId=identity["instanceId"],observedAt=wire_time(self._clock()),workerIdentitySha256=checksum(identity))
        record["leaseEpoch"] = epoch
        return self._prepare(base,record,guard)

    def _same_head(self,base):
        current = self.read()
        if current.reference != base.reference or current._etag != base._etag: raise CASConflict("Registry changed; reload without replay")

    def commit(self,mutation):
        if not isinstance(mutation,RegistryMutation) or mutation._store is not self._token or mutation._state != "PREPARED":
            raise ValueError("Same-store unconsumed registry mutation required")
        mutation._guard(); self._same_head(mutation._base)
        record,body = mutation._record,encoded(mutation._record)
        mid = record["lastMutationId"]
        claim_key = f"{PREFIX}worker-mutations/{self._session.execution['sessionId']}/{mid}.json"
        claim = dict(schemaVersion=2,executionReference=self._session.execution_reference,baseRegistryReference=mutation._base.reference,
                     mutationId=mid,claimNonce=mutation._nonce,principalArn=self._session._principal,intendedRecord=record)
        claim_body = encoded(claim); tags = registry_tags(record,self._session.execution_reference)
        if len(claim_body) > 32768: raise ValueError("Bounded original registry claim required")
        mutation._state = "CLAIMING"
        try:
            response = self._session._put(claim_key,claim_body,tags,IfNoneMatch="*")
        except Exception as error:
            if self._session._service_error(error) == ("PreconditionFailed",412):
                mutation._state = "CONSUMED"
                raise MutationConsumed("Durable registry mutation consumed; read/recover only") from None
            raise RegistryUncertain(mid) from None
        try: self._session._write_response(response,checksum(claim))
        except Exception: raise RegistryUncertain(mid) from None
        mutation._state = "CLAIMED"
        mutation._guard(); self._same_head(mutation._base)
        mutation._state = "CALLING"
        condition = dict(IfNoneMatch="*") if mutation._base.reference is None else dict(IfMatch=mutation._base._etag)
        try: response = self._session._put(self.key,body,tags,**condition)
        except Exception as error:
            if self._session._service_error(error) == ("PreconditionFailed",412):
                mutation._state = "CONFLICT"
                raise CASConflict("Registry CAS conflict; observe without replay") from None
            raise RegistryUncertain(mid) from None
        try:
            self._session._write_response(response,checksum(record)); latest = self.read()
        except Exception: raise RegistryUncertain(mid) from None
        if latest.reference["versionId"] != response["VersionId"] or latest.reference["sha256"] != checksum(record):
            mutation._state = "SUPERSEDED"
            raise WriteSuperseded("Registry was superseded; no fresh launch acknowledgement")
        if latest._etag != response["ETag"]: raise RegistryUncertain(mid)
        mutation._guard()
        mutation._state = "OBSERVED"
        return latest

    def fresh_calling(self,mutation):
        """Only same-process positive commit can acknowledge a new CALLING row."""
        latest = self.commit(mutation)
        old = mutation._base.as_dict()
        changed = [row for row in latest.as_dict()["launches"] if row["phase"] == "CALLING" and
                   any(r["purpose"] == row["purpose"] and r["attempt"] == row["attempt"] and r["phase"] == "PREPARED" for r in old["launches"])] if old else []
        if len(changed) != 1: raise ValueError("Exactly one new CALLING slot required")
        row = changed[0]
        return FreshCalling(self,latest,(row["purpose"],row["attempt"]),mutation._guard,_seal=_SEAL)

    def recover(self,mutation_id):
        """Read original durable claim and matching head; never returns FreshCalling."""
        self._session._caller()
        text(mutation_id,r"[a-f0-9]{32}")
        key = f"{PREFIX}worker-mutations/{self._session.execution['sessionId']}/{mutation_id}.json"
        raw,ref,_ = self._session._object(key)
        claim = decode_object(raw,ref,key=key)
        fields(claim,{"schemaVersion","executionReference","baseRegistryReference","mutationId","claimNonce","principalArn","intendedRecord"})
        record = validate_registry(claim["intendedRecord"],self._session.execution,self._session.execution_reference)
        if (type(claim["schemaVersion"]) is not int or claim["schemaVersion"] != 2 or claim["executionReference"] != self._session.execution_reference
                or claim["mutationId"] != mutation_id or record["lastMutationId"] != mutation_id
                or self._mutation_id(claim["baseRegistryReference"],record) != mutation_id): raise ValueError("Registry claim mismatch")
        text(claim["claimNonce"],r"[a-f0-9]{32}")
        principal = text(claim["principalArn"],rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-(?:lifecycle-launcher|cleanup-launcher)/[A-Za-z0-9+=,.@_-]{{1,64}}")
        old = None
        if claim["baseRegistryReference"] is not None:
            base_ref = reference(claim["baseRegistryReference"],key=self.key)
            base_raw,_,_ = self._session._object(self.key,expected=base_ref)
            old = validate_registry(decode_object(base_raw,base_ref,key=self.key),self._session.execution,self._session.execution_reference)
            self._session._tags(base_ref,registry_tags(old,self._session.execution_reference))
        kind,purpose = self._transition(old,record)
        required = "cleanup-launcher" if purpose == "cleanup" else "lifecycle-launcher"
        if f"/techx-automation-v2-{required}/" not in principal and not (kind == "observe" and "/techx-automation-v2-cleanup-launcher/" in principal):
            raise ValueError("Durable claim principal cannot perform this transition")
        self._session._tags(ref,registry_tags(record,self._session.execution_reference))
        current = self.read()
        return current if current.reference is not None and current.reference["sha256"] == checksum(record) else None
