from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import hashlib
import json
import runpy
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qsl

from execution_v2 import ACCOUNT, BUCKET, PREFIX, REGION
from manager_bridge_v2 import CLEANUP
from manager_handoff_v2 import ServerManagerHandoff, build_handoff
from session_runtime_v2 import CASConflict, MutationConsumed, encoded
from worker_identity import launch_token, worker_identity
from worker_registry_v2 import (FreshCalling, RegistryObservation, RegistryUncertain, WorkerRegistry,
                                checksum, registry_tags, validate_registry)

HandoffHelpers = runpy.run_path(str(Path(__file__).with_name("test-manager-handoff-v2.py")))
SessionHelpers = runpy.run_path(str(Path(__file__).with_name("test-session-runtime-v2.py")))
ContractHelpers = runpy.run_path(str(Path(__file__).with_name("test-execution-v2.py")))
ServiceError = SessionHelpers["ServiceError"]


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.h = HandoffHelpers["HandoffTests"](); self.h.setUp()
        self.aws = self.h.f.aws
        self.key = f"{PREFIX}workers/{self.aws.execution['sessionId']}/registry.json"
        self.mode = "success"; self.after_claim = None; self.read_failure = None; self.history_override = None
        self.launcher_role = "lifecycle-launcher"
        self.principal_id = "AROA"+"A"*17
        f = ContractHelpers["V2Contracts"](); f.setUp()
        self.intent = dict(deepcopy(f.intent),executionReference=deepcopy(self.aws.ref))
        self.intent["clientToken"] = launch_token(self.intent)
        raw = encoded(self.intent); obj = self.aws.publish(self.h.intent["key"],raw)
        self.intent_ref = dict(bucket=BUCKET,key=obj["key"],versionId=obj["VersionId"],sha256=hashlib.sha256(raw).hexdigest())
        self.h.intent = deepcopy(self.intent_ref)
        self.handoff = self.h.receiver.verify(self.h.event(),self.h.context)
        self.instance = deepcopy(f.instance)
        self.instance["MetadataOptions"].update(HttpEndpoint="enabled",HttpPutResponseHopLimit=1)
        self.instance["ClientToken"] = self.intent["clientToken"]
        self.instance["Tags"] = [dict(Key=k,Value=v) for k,v in dict(Project="TechX",Environment="automation",Role="deployment-executor",
            SessionId=self.aws.execution["sessionId"],ExecutionSha256=self.aws.ref["sha256"],LeaseEpoch="1").items()]
        self.instance.update(BlockDeviceMappings=[dict(DeviceName="/dev/xvda",Ebs=dict(VolumeId="vol-"+"1"*17,DeleteOnTermination=True,Status="attached"))],
                             NetworkInterfaces=[dict(NetworkInterfaceId="eni-"+"2"*17,Status="in-use",Attachment=dict(DeleteOnTermination=True))])
        outer = self
        class STS:
            meta = self.aws.clients["sts"].meta
            def get_caller_identity(inner):
                session = "techx-v2-123456-1" if outer.launcher_role == "manager" else "runtime-session"
                return dict(Account=ACCOUNT,Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-{outer.launcher_role}/{session}",
                            ResponseMetadata=dict(HTTPStatusCode=200,RequestId="launcher-sts"))
        original = self.aws.clients["s3"]
        class S3:
            meta = original.meta
            def get_object(inner,**params):
                if params["Key"] == outer.key and outer.read_failure:
                    raise ServiceError(*outer.read_failure)
                try: return original.get_object(**params)
                except KeyError: raise ServiceError("NoSuchKey",404) from None
            def get_object_tagging(inner,**params): return original.get_object_tagging(**params)
            def list_object_versions(inner,**params):
                outer.aws.calls.append(("s3","list_object_versions",params))
                if outer.history_override is not None: return deepcopy(outer.history_override)
                return dict(IsTruncated=False,Versions=[dict(Key=outer.key,VersionId=obj["VersionId"]) for obj in outer.aws.objects.get(outer.key,[])],
                            ResponseMetadata=dict(HTTPStatusCode=200,RequestId="registry-history"))
            def put_object(inner,**params):
                if params["Key"] != outer.key:
                    response = original.put_object(**params)
                    if outer.after_claim: outer.after_claim()
                    return response
                outer.aws.calls.append(("s3","put_object",params))
                current = outer.aws.objects.get(outer.key,[])
                if params.get("IfNoneMatch") == "*" and current: raise ServiceError("PreconditionFailed",412)
                if params.get("IfMatch") and (not current or current[-1]["ETag"] != params["IfMatch"]): raise ServiceError("PreconditionFailed",412)
                if outer.mode == "conflict409": raise ServiceError("ConditionalRequestConflict",409)
                if outer.mode == "timeout-before": raise TimeoutError()
                obj = outer.aws.publish(outer.key,params["Body"],dict(parse_qsl(params["Tagging"])))
                if outer.mode == "timeout-after": raise TimeoutError()
                return dict(VersionId=obj["VersionId"],ETag=obj["ETag"],ServerSideEncryption="aws:kms",SSEKMSKeyId=SessionHelpers["KMS_KEY"],
                            ChecksumSHA256=params["ChecksumSHA256"],ResponseMetadata=dict(HTTPStatusCode=200,RequestId="registry-put"))
        class EC2:
            meta = SimpleNamespace(endpoint_url=f"https://ec2.{REGION}.amazonaws.com",region_name=REGION,config=SimpleNamespace(retries={"total_max_attempts":1}))
            def describe_instances(inner,**params):
                outer.aws.calls.append(("ec2","describe_instances",params))
                return dict(Reservations=[dict(OwnerId=ACCOUNT,Instances=[deepcopy(outer.instance)])],
                            ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ec2-registration"))
        class IAM:
            meta = SimpleNamespace(endpoint_url="https://iam.amazonaws.com",region_name=REGION,config=SimpleNamespace(retries={"total_max_attempts":1}))
            def get_instance_profile(inner,**params):
                outer.aws.calls.append(("iam","get_instance_profile",params))
                return dict(InstanceProfile=dict(Arn=outer.intent["instanceProfileArn"],Roles=[dict(RoleId=outer.principal_id,
                    Arn=outer.intent["instanceProfileArn"].replace(":instance-profile/",":role/"))]),
                    ResponseMetadata=dict(HTTPStatusCode=200,RequestId="iam-profile"))
        self.clients = dict(s3=S3(),sts=STS(),ec2=EC2(),iam=IAM())
        self.registry = self.new_registry()

    def new_registry(self): return WorkerRegistry(self.clients,self.aws.ref,clock=lambda:self.aws.now)
    def reserve(self): return self.registry.commit(self.registry.reserve(self.registry.read(),self.intent_ref,self.handoff))
    def calling(self):
        self.reserve()
        mutation = self.registry.begin(self.registry.read(),"deployment",self.handoff)
        ack = self.registry.fresh_calling(mutation)
        self.instance["LaunchTime"] = self.aws.now
        return ack
    def puts(self,key=None): return [p for n,op,p in self.aws.calls if op == "put_object" and (key is None or p["Key"] == key)]
    def registration(self):
        caller = dict(Account=ACCOUNT,Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-{self.intent['purpose']}-executor/{self.instance['InstanceId']}")
        identity = dict(worker_identity(self.intent,self.instance,caller,self.aws.execution,self.aws.ref,1),
                        bootId="12345678-1234-1234-1234-123456789abc",principalId=f"{self.principal_id}:{self.instance['InstanceId']}")
        key = f"{PREFIX}workers/{self.aws.execution['sessionId']}/registrations/{identity['principalId']}.json"
        body = encoded(dict(schemaVersion=2,executionReference=self.aws.ref,intentReference=self.intent_ref,workerIdentity=identity))
        obj = self.aws.publish(key,body,{"techx-schema":"2","techx-session-id":self.aws.execution["sessionId"],
                                       "techx-worker-id":identity["instanceId"],"techx-boot-id":identity["bootId"]})
        return dict(bucket=BUCKET,key=key,versionId=obj["VersionId"],sha256=hashlib.sha256(body).hexdigest())

    def test_empty_positive_absence_reserves_exact_private_intent(self):
        self.assertIsNone(self.registry.read().as_dict())
        result = self.reserve()
        row = result.as_dict()["launches"][0]
        self.assertEqual(row["intentReference"],self.intent_ref)
        self.assertEqual(row["phase"],"PREPARED")
        self.assertEqual(len(self.puts(self.key)),1)
        self.assertEqual(self.puts(self.key)[0]["IfNoneMatch"],"*")
        self.assertFalse(any(n == "ec2" for n,op,p in self.aws.calls))

    def test_begin_has_durable_claim_cas_and_one_shot_fresh_ack(self):
        ack = self.calling()
        self.assertEqual(ack.as_dict()["admission"],"NOT_ESTABLISHED")
        self.assertIn("IfMatch",self.puts(self.key)[-1])
        ack.take()
        with self.assertRaises(MutationConsumed): ack.take()
        with self.assertRaises(ValueError): self.registry.begin(self.registry.read(),"deployment",self.handoff)

    def test_unknown_calling_write_cannot_mint_new_claim_after_restart_and_clock_change(self):
        self.reserve(); base = self.registry.read(); self.mode = "timeout-before"
        mutation = self.registry.begin(base,"deployment",self.handoff)
        with self.assertRaises(RegistryUncertain): self.registry.fresh_calling(mutation)
        before = len(self.puts(self.key)); self.mode = "success"; self.aws.now += timedelta(seconds=60)
        replacement = self.new_registry()
        again = replacement.begin(replacement.read(),"deployment",self.handoff)
        self.assertEqual(mutation.as_dict()["lastMutationId"],again.as_dict()["lastMutationId"])
        with self.assertRaises(MutationConsumed): replacement.fresh_calling(again)
        self.assertEqual(len(self.puts(self.key)),before)
        self.assertIsNone(replacement.recover(mutation.as_dict()["lastMutationId"]))

    def test_lost_accepted_write_recovers_read_only_without_fresh_launch_ack(self):
        self.reserve(); mutation = self.registry.begin(self.registry.read(),"deployment",self.handoff); self.mode = "timeout-after"
        with self.assertRaises(RegistryUncertain): self.registry.fresh_calling(mutation)
        reader = self.new_registry(); before = len(self.puts())
        recovered = reader.recover(mutation.as_dict()["lastMutationId"])
        self.assertIsInstance(recovered,RegistryObservation)
        self.assertNotIsInstance(recovered,FreshCalling)
        self.assertEqual(len(self.puts()),before)
        with self.assertRaises(ValueError): reader.begin(reader.read(),"deployment",self.handoff)

    def test_claim_timeout_before_or_after_is_never_replayed(self):
        for mode in ("timeout-before","timeout-after"):
            self.setUp(); self.aws.claim_mode = mode
            prepared = self.registry.reserve(self.registry.read(),self.intent_ref,self.handoff)
            with self.assertRaises(RegistryUncertain): self.registry.commit(prepared)
            with self.assertRaises(ValueError): self.registry.commit(prepared)
            self.assertEqual(self.puts(self.key),[])

    def test_existing_slot_cannot_be_re_reserved_or_change_intent_version(self):
        self.reserve()
        with self.assertRaises(MutationConsumed): self.registry.reserve(self.registry.read(),self.intent_ref,self.handoff)
        changed = dict(self.intent_ref,versionId="wrong-version")
        with self.assertRaises(Exception) as caught: self.registry.reserve(self.registry.read(),changed,self.handoff)
        self.assertEqual(caught.exception.response["Error"]["Code"],"NoSuchVersion")

    def test_role_and_sender_json_are_not_launch_authority(self):
        with self.assertRaises(ValueError): self.registry.reserve(self.registry.read(),self.intent_ref,self.handoff.as_dict())
        self.launcher_role = "manager"
        other = self.new_registry()
        with self.assertRaises(ValueError): other.reserve(other.read(),self.intent_ref,self.handoff)

    def test_snapshot_cannot_be_reconstructed_or_cross_stores(self):
        with self.assertRaises(ValueError): RegistryObservation(None,{},None,None)
        other = self.new_registry()
        with self.assertRaises(ValueError): other.reserve(self.registry.read(),self.intent_ref,self.handoff)

    def test_cancellation_or_cleanup_fence_after_claim_blocks_registry_write(self):
        self.after_claim = lambda:self.h.f.run.update(status="completed",conclusion="cancelled")
        with self.assertRaises(ValueError): self.reserve()
        self.assertEqual(self.puts(self.key),[])

    def test_fresh_ack_rechecks_epoch_and_cannot_survive_new_cleanup_fence(self):
        ack = self.calling()
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(ValueError): ack.take()

    def test_observation_owns_sdk_reads_and_registers_original_assets_and_boot(self):
        self.calling(); ref = self.registration()
        result = self.registry.commit(self.registry.observe(self.registry.read(),"deployment",1,ref))
        row = result.as_dict()["launches"][0]
        self.assertEqual(row["phase"],"OBSERVED")
        self.assertEqual(row["registrationReference"],ref)
        self.assertEqual(row["volumeIds"],["vol-"+"1"*17])
        self.assertEqual(row["networkInterfaceIds"],["eni-"+"2"*17])
        self.assertTrue(any(n == "ec2" for n,op,p in self.aws.calls))
        self.assertTrue(any(n == "iam" for n,op,p in self.aws.calls))

    def test_partial_launch_can_be_observed_after_fence_without_github(self):
        self.calling(); ref = self.registration()
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        self.h.f.run.update(status="completed",conclusion="cancelled")
        result = self.registry.commit(self.registry.observe(self.registry.read(),"deployment",1,ref))
        self.assertEqual(result.as_dict()["leaseEpoch"],2)
        self.assertEqual(result.as_dict()["launches"][0]["launchEpoch"],1)

    def test_observation_cannot_rebind_to_changed_iam_role_or_ip(self):
        self.calling(); ref = self.registration()
        self.principal_id = "AROA"+"B"*17
        with self.assertRaises(ValueError): self.registry.observe(self.registry.read(),"deployment",1,ref)
        self.principal_id = "AROA"+"A"*17; self.instance["PublicIpAddress"] = "8.8.4.4"
        with self.assertRaises(ValueError): self.registry.observe(self.registry.read(),"deployment",1,ref)

    def test_missing_original_assets_cannot_become_observed(self):
        self.calling(); ref = self.registration(); self.instance["NetworkInterfaces"] = []
        with self.assertRaises(ValueError): self.registry.observe(self.registry.read(),"deployment",1,ref)

    def test_finalizer_or_worker_cannot_write_launch_roster(self):
        self.launcher_role = "finalizer"; reader = self.new_registry()
        with self.assertRaises(ValueError): reader.reserve(reader.read(),self.intent_ref,self.handoff)

    def test_real_sdk_registry_put_shapes(self):
        import botocore.session
        from botocore.validate import validate_parameters
        self.calling(); model = botocore.session.get_session().get_service_model("s3")
        for params in self.puts(): validate_parameters(params,model.operation_model("PutObject").input_shape)
        for name,op,params in self.aws.calls:
            if op == "list_object_versions": validate_parameters(params,model.operation_model("ListObjectVersions").input_shape)

    def test_api_error_or_missing_metadata_cannot_mean_absent_registry(self):
        for failure in (("AccessDenied",403),("InternalError",500),("NoSuchKey",403)):
            self.read_failure = failure
            with self.assertRaises(ServiceError): self.registry.read()

    def test_unknown_registry_conflict_is_consumed_not_retried(self):
        self.reserve(); mutation = self.registry.begin(self.registry.read(),"deployment",self.handoff)
        self.mode = "conflict409"
        with self.assertRaises(RegistryUncertain): self.registry.fresh_calling(mutation)
        writes = len(self.puts(self.key)); self.mode = "success"
        with self.assertRaises(MutationConsumed): self.registry.fresh_calling(self.registry.begin(self.registry.read(),"deployment",self.handoff))
        self.assertEqual(len(self.puts(self.key)),writes)

    def test_registry_changed_after_claim_cannot_use_old_etag(self):
        base = self.reserve()
        def change_head():
            record = base.as_dict(); record.update(runtimeRevision=2,lastMutationId="f"*32)
            self.aws.publish(self.key,encoded(record),registry_tags(record,self.aws.ref))
        self.after_claim = change_head
        with self.assertRaises(CASConflict): self.registry.fresh_calling(self.registry.begin(base,"deployment",self.handoff))
        self.assertEqual(len(self.puts(self.key)),1)

    def test_same_intent_bytes_in_another_version_cannot_replace_reserved_binding(self):
        self.reserve(); obj = self.aws.publish(self.intent_ref["key"],encoded(self.intent))
        self.h.intent = dict(self.intent_ref,versionId=obj["VersionId"])
        handoff = self.h.receiver.verify(self.h.event(),self.h.context)
        with self.assertRaises(ValueError): self.registry.begin(self.registry.read(),"deployment",handoff)

    def test_unscoped_intent_or_registration_is_rejected_before_its_sdk_read(self):
        base = self.registry.read(); before = len(self.aws.calls)
        with self.assertRaises(ValueError): self.registry.reserve(base,dict(self.intent_ref,key="other-project/secret.json"),self.handoff)
        self.assertEqual(len(self.aws.calls),before)
        self.calling(); base = self.registry.read(); before = len(self.aws.calls)
        with self.assertRaises(ValueError): self.registry.observe(base,"deployment",1,dict(self.intent_ref,key=f"{PREFIX}workers/{self.aws.execution['sessionId']}/registrations/../secret.json"))
        self.assertFalse(any(op == "get_object" and p["Key"].endswith("../secret.json") for n,op,p in self.aws.calls[before:]))

    def test_registry_tags_and_original_slot_must_still_match(self):
        self.reserve(); self.aws.objects[self.key][-1]["tags"]["techx-runtime-revision"] = "99"
        with self.assertRaises(ValueError): self.registry.read()

    def test_roster_bounds_duplicate_slots_and_future_epoch_are_rejected(self):
        snapshot = self.reserve(); value = snapshot.as_dict()
        duplicate = deepcopy(value); duplicate["launches"].append(deepcopy(duplicate["launches"][0]))
        with self.assertRaises(ValueError): validate_registry(duplicate,self.aws.execution,self.aws.ref)
        future = deepcopy(value); future["launches"][0]["launchEpoch"] = 2
        with self.assertRaises(ValueError): validate_registry(future,self.aws.execution,self.aws.ref)
        bare = {k:v for k,v in value.items() if k not in {"runtimeRevision","lastMutationId"}}
        with self.assertRaises(ValueError): validate_registry(bare,self.aws.execution,self.aws.ref)

    def test_recovery_rejects_a_rewind_even_with_self_consistent_claim_hash(self):
        self.calling(); base = self.registry.read(); record = base.as_dict()
        record["runtimeRevision"] += 1
        row = record["launches"][0]
        row["phase"] = "PREPARED"
        row["checkpoint"].update(phase="PREPARED",callStartedAt=None)
        mid = self.registry._mutation_id(base.reference,record); record["lastMutationId"] = mid
        claim = dict(schemaVersion=2,executionReference=self.aws.ref,baseRegistryReference=base.reference,mutationId=mid,
                     claimNonce="a"*32,principalArn=self.registry._session._principal,intendedRecord=record)
        key = f"{PREFIX}worker-mutations/{self.aws.execution['sessionId']}/{mid}.json"
        self.aws.publish(key,encoded(claim),registry_tags(record,self.aws.ref))
        with self.assertRaises(ValueError): self.registry.recover(mid)

    def test_stale_observation_after_claim_does_not_publish_new_identity(self):
        self.calling(); ref = self.registration()
        mutation = self.registry.observe(self.registry.read(),"deployment",1,ref)
        self.after_claim = lambda:setattr(self,"principal_id","AROA"+"B"*17)
        before = len(self.puts(self.key))
        with self.assertRaises(ValueError): self.registry.commit(mutation)
        self.assertEqual(len(self.puts(self.key)),before)

    def test_read_only_observer_role_cannot_mutate_registration(self):
        self.calling(); ref = self.registration(); self.launcher_role = "readiness"
        with self.assertRaises(ValueError): self.registry.observe(self.registry.read(),"deployment",1,ref)

    def test_cleanup_can_align_only_to_actual_newer_fence_without_github(self):
        self.calling(); before = self.registry.read().as_dict()["launches"]
        self.launcher_role = "cleanup-launcher"; cleanup = self.new_registry()
        with self.assertRaises(ValueError): cleanup.align_fence(cleanup.read())
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        self.h.f.run.update(status="completed",conclusion="cancelled")
        mutation = cleanup.align_fence(cleanup.read()); result = cleanup.commit(mutation)
        self.assertEqual(result.as_dict()["launches"],before)
        self.assertEqual(result.as_dict()["leaseEpoch"],2)
        self.assertIsNotNone(cleanup.recover(mutation.as_dict()["lastMutationId"]))
        with self.assertRaises(ValueError): cleanup.align_fence(cleanup.read())

    def test_lifecycle_cannot_align_roster_to_a_cleanup_fence(self):
        self.reserve()
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(ValueError): self.registry.align_fence(self.registry.read())

    def test_lost_registry_head_cannot_recreate_or_discard_prior_worker_history(self):
        self.reserve(); self.read_failure = ("NoSuchKey",404)
        with self.assertRaises(ValueError): self.registry.read()
        self.assertEqual(len(self.puts(self.key)),1)

    def test_warm_cleanup_first_slot_uses_start_handoff_and_own_role(self):
        self.h.switch_job("staging-lifecycle.yml","start")
        self.intent["purpose"] = "cleanup"
        self.intent["instanceProfileArn"] = self.intent["instanceProfileArn"].replace("deployment-executor","cleanup-executor")
        self.intent["configurationReference"]["key"] = self.intent["configurationReference"]["key"].replace("deployment.json","cleanup.json")
        self.intent["clientToken"] = launch_token(self.intent)
        key = self.intent_ref["key"].replace("/deployment/","/cleanup/")
        obj = self.aws.publish(key,encoded(self.intent))
        self.intent_ref = dict(bucket=BUCKET,key=key,versionId=obj["VersionId"],sha256=checksum(self.intent))
        packet = build_handoff(self.h.manager,self.h.f.token,operation="arm-cleanup",intent_reference=self.intent_ref)
        receiver = ServerManagerHandoff(self.h.f.bridge,HandoffHelpers["ReceiverSTS"](CLEANUP),function_arn=CLEANUP)
        self.handoff = receiver.verify(json.loads(packet._payload),SimpleNamespace(invoked_function_arn=CLEANUP,aws_request_id="warm-cleanup"))
        with self.assertRaises(ValueError): self.registry.reserve(self.registry.read(),self.intent_ref,self.handoff)
        self.launcher_role = "cleanup-launcher"; self.registry = self.new_registry()
        self.registry.commit(self.registry.reserve(self.registry.read(),self.intent_ref,self.handoff))
        ack = self.registry.fresh_calling(self.registry.begin(self.registry.read(),"cleanup",self.handoff))
        self.assertEqual(ack.take()["purpose"],"cleanup")
        self.assertEqual(self.registry.read().as_dict()["launches"][0]["phase"],"CALLING")

    def test_partial_or_malformed_history_is_not_bootstrap_permission(self):
        positive = dict(IsTruncated=False,ResponseMetadata=dict(HTTPStatusCode=200,RequestId="history-request"))
        for changed in (dict(IsTruncated=True),dict(NextKeyMarker="more"),dict(Versions=None),dict(ResponseMetadata={})):
            self.history_override = dict(positive,**changed)
            with self.assertRaises(ValueError): self.registry.read()
        self.assertEqual(self.puts(),[])

    def test_read_binding_keeps_original_row_provenance_across_unrelated_revision(self):
        self.calling(); ref = self.registration()
        observed = self.registry.commit(self.registry.observe(self.registry.read(),"deployment",1,ref))
        reader = self.registry.registered_worker("deployment"); original = reader.as_dict()
        value = observed.as_dict(); value.update(runtimeRevision=value["runtimeRevision"]+1,lastMutationId="f"*32)
        self.aws.publish(self.key,encoded(value),registry_tags(value,self.aws.ref))
        self.assertEqual(reader.require_current(),original)

    def test_terminal_proof_overrides_stale_running_service_observation(self):
        self.calling(); ref = self.registration()
        observed = self.registry.commit(self.registry.observe(self.registry.read(),"deployment",1,ref))
        reader = self.registry.registered_worker("deployment")
        value = observed.as_dict(); value["launches"][0]["terminalReference"] = dict(bucket=BUCKET,
            key=f"{PREFIX}cleanup/{self.aws.execution['sessionId']}/terminals/{self.instance['InstanceId']}.json",versionId="terminal-version",sha256="e"*64)
        value.update(runtimeRevision=value["runtimeRevision"]+1,lastMutationId="f"*32)
        self.aws.publish(self.key,encoded(value),registry_tags(value,self.aws.ref))
        with self.assertRaises(ValueError): reader.require_current()
        with self.assertRaises(ValueError): self.registry.registered_worker("deployment")


if __name__ == "__main__": unittest.main()
