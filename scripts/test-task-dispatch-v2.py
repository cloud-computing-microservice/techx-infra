from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qsl

from execution_v2 import ACCOUNT, BUCKET, PREFIX, REGION
from recovery_v2 import checksum, wire_time
from session_runtime_v2 import CASConflict, MutationConsumed, encoded
from task_dispatch_v2 import DispatchUnknown, ObservedDispatch, PreparedDispatch, TaskRuntime, lease_tags, result_tags
from worker_identity import launch_token

Helpers = runpy.run_path(str(Path(__file__).with_name("test-worker-registry-v2.py")))
ServiceError = Helpers["ServiceError"]


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.f = Helpers["RegistryTests"](); self.f.setUp()
        self.aws = self.f.aws
        self.config = dict(document=dict(name="techx-automation-v2-deployment-task",version="3",sha256="a"*64))
        obj = self.aws.publish(self.f.intent["configurationReference"]["key"],encoded(self.config))
        self.config_ref = dict(bucket=BUCKET,key=obj["key"],versionId=obj["VersionId"],sha256=checksum(self.config))
        self.f.intent["configurationReference"] = self.config_ref; self.f.intent["clientToken"] = launch_token(self.f.intent)
        self.f.instance["ClientToken"] = self.f.intent["clientToken"]
        obj = self.aws.publish(self.f.intent_ref["key"],encoded(self.f.intent))
        self.f.intent_ref = dict(bucket=BUCKET,key=obj["key"],versionId=obj["VersionId"],sha256=checksum(self.f.intent)); self.f.h.intent = deepcopy(self.f.intent_ref)
        self.f.handoff = self.f.h.receiver.verify(self.f.h.event(),self.f.h.context)
        self.f.calling(); self.registration_ref = self.f.registration()
        self.f.registry.commit(self.f.registry.observe(self.f.registry.read(),"deployment",1,self.registration_ref))
        bridge = self.f.h.f
        bridge.user_id = "AROA"+"M"*17+":techx-v2-123456-1"
        self.manager = bridge.authenticate()
        self.lease_mode,self.send_mode,self.metadata_mode = "success","success",None
        self.send_calls,self.ssm_calls,self.commands = [],[],[]
        self.document_change,self.offline = {},False
        self.list_override = None
        self.lease_key = f"{PREFIX}leases/{self.aws.execution['sessionId']}/deployment.json"
        base_s3 = self.f.clients["s3"]; outer = self
        class S3:
            meta = base_s3.meta
            def get_object(inner,**params): return base_s3.get_object(**params)
            def get_object_tagging(inner,**params): return base_s3.get_object_tagging(**params)
            def list_object_versions(inner,**params):
                outer.aws.calls.append(("s3","list_object_versions",params))
                versions = [dict(Key=key,VersionId=o["VersionId"]) for key,objects in outer.aws.objects.items() for o in objects if key.startswith(params["Prefix"])]
                return dict(IsTruncated=False,Versions=versions,ResponseMetadata=dict(HTTPStatusCode=200,RequestId="lease-history"))
            def put_object(inner,**params):
                key = params["Key"]
                if key == outer.lease_key:
                    outer.aws.calls.append(("s3","put_object",params))
                    objects = outer.aws.objects.get(key,[])
                    if params.get("IfNoneMatch") == "*" and objects: raise ServiceError("PreconditionFailed",412)
                    if params.get("IfMatch") and (not objects or params["IfMatch"] != objects[-1]["ETag"]): raise ServiceError("PreconditionFailed",412)
                    if outer.lease_mode == "timeout-before": raise TimeoutError()
                    obj = outer.aws.publish(key,params["Body"],dict(parse_qsl(params["Tagging"])))
                    if outer.lease_mode == "timeout-after": raise TimeoutError()
                    return dict(VersionId=obj["VersionId"],ETag=obj["ETag"],ServerSideEncryption="aws:kms",SSEKMSKeyId=Helpers["SessionHelpers"]["KMS_KEY"],
                                ChecksumSHA256=params["ChecksumSHA256"],ResponseMetadata=dict(HTTPStatusCode=200,RequestId="lease-put"))
                if outer.metadata_mode == "lost-handle" and "/dispatches/" in key:
                    base_s3.put_object(**params); raise TimeoutError()
                return base_s3.put_object(**params)
        class SSM:
            meta = SimpleNamespace(endpoint_url=f"https://ssm.{REGION}.amazonaws.com",region_name=REGION,config=SimpleNamespace(retries={"total_max_attempts":1}))
            def describe_document(inner,**params):
                outer.ssm_calls.append(("describe_document",params))
                document = dict(Name=outer.config["document"]["name"],DocumentVersion="3",Hash="a"*64,HashType="Sha256",Status="Active",DocumentType="Command",SchemaVersion="2.2",
                                PlatformTypes=["Linux"],Owner=ACCOUNT,Parameters=[dict(Name="TaskId",Type="String")])
                document.update(outer.document_change)
                return dict(Document=document,ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ssm-document"))
            def describe_instance_information(inner,**params):
                outer.ssm_calls.append(("describe_instance_information",params))
                return dict(InstanceInformationList=[dict(InstanceId=outer.f.instance["InstanceId"],PingStatus="ConnectionLost" if outer.offline else "Online",
                    PlatformType="Linux",ResourceType="EC2Instance",LastPingDateTime=outer.aws.now)],ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ssm-agent"))
            def send_command(inner,**params):
                outer.send_calls.append(deepcopy(params))
                if outer.send_mode == "timeout-before": raise TimeoutError("secret-sdk-message")
                command = dict(CommandId=f"{0x12345678+len(outer.commands):08x}-1234-1234-1234-123456789abc",InstanceIds=params["InstanceIds"],DocumentName=params["DocumentName"],
                    DocumentVersion=params["DocumentVersion"],Parameters=params["Parameters"],Comment=params["Comment"],Status="Pending",RequestedDateTime=outer.aws.now)
                outer.commands.append(command)
                if outer.send_mode == "timeout-after": raise TimeoutError("secret-sdk-message")
                return dict(Command=deepcopy(command),ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ssm-send"))
            def list_commands(inner,**params):
                outer.ssm_calls.append(("list_commands",params))
                if outer.list_override is not None: return deepcopy(outer.list_override)
                return dict(Commands=deepcopy(outer.commands),ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ssm-list"))
            def get_command_invocation(inner,**params):
                outer.ssm_calls.append(("get_command_invocation",params))
                return dict(CommandId=params["CommandId"],InstanceId=params["InstanceId"],DocumentName=outer.config["document"]["name"],DocumentVersion="3",
                    PluginName="runTask",Status="Success",StatusDetails="Success",ResponseCode=0,
                    ExecutionStartDateTime=outer.commands[0]["RequestedDateTime"].isoformat(),ExecutionEndDateTime=outer.aws.now.isoformat(),
                    ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ssm-invocation"))
        self.clients = dict(self.manager._clients,s3=S3(),ssm=SSM(),iam=self.f.clients["iam"],ec2=self.f.clients["ec2"])
        self.runtime = self.new_runtime()
        self.input_key = f"{PREFIX}inputs/{self.aws.execution['sessionId']}/plan/foundation/1.json"
        inputs = dict(schemaVersion=2,executionReference=self.aws.ref,operation="plan",stage="foundation",attempt=1,parameters={})
        obj = self.aws.publish(self.input_key,encoded(inputs))
        self.input_ref = dict(bucket=BUCKET,key=self.input_key,versionId=obj["VersionId"],sha256=checksum(inputs))

    def new_runtime(self): return TaskRuntime(self.clients,self.aws.ref,clock=lambda:self.aws.now)
    def prepare(self): return self.runtime.prepare(self.manager,purpose="deployment",operation="plan",stage="foundation",attempt=1,input_reference=self.input_ref)
    def task_ref(self): return self.runtime.current_task("deployment")["taskReference"]
    def puts(self): return [p for n,op,p in self.aws.calls if op == "put_object"]

    def test_dispatch_fixed_document_hash_version_single_instance_and_task_only(self):
        prepared = self.prepare(); result = self.runtime.dispatch(prepared)
        self.assertIsInstance(result,ObservedDispatch)
        self.assertEqual(result.as_dict()["admission"],"NOT_ESTABLISHED")
        sent = self.send_calls[0]
        self.assertEqual(sent["DocumentHash"],"a"*64)
        self.assertEqual(sent["DocumentVersion"],"3")
        self.assertEqual(sent["InstanceIds"],[self.f.instance["InstanceId"]])
        self.assertEqual(sent["Parameters"],{"TaskId":[prepared.as_dict()["task"]["taskId"]]})
        self.assertFalse(sent["CloudWatchOutputConfig"]["CloudWatchOutputEnabled"])
        with self.assertRaises(ValueError): self.runtime.dispatch(prepared)
        self.assertEqual(len(self.send_calls),1)

    def test_concurrent_preparations_cannot_dispatch_two_tasks(self):
        first,second = self.prepare(),self.prepare()
        self.runtime.dispatch(first)
        with self.assertRaises(CASConflict): self.runtime.dispatch(second)
        self.assertEqual(len(self.send_calls),1)

    def test_unknown_send_has_no_retry_and_reconciles_same_command(self):
        prepared = self.prepare(); self.send_mode = "timeout-after"
        with self.assertRaises(DispatchUnknown) as caught: self.runtime.dispatch(prepared)
        self.assertNotIn("secret-sdk-message",str(caught.exception))
        result = self.runtime.observe(self.task_ref())
        self.assertEqual(result.as_dict()["handle"]["commandId"],self.commands[0]["CommandId"])
        with self.assertRaises(MutationConsumed): self.prepare()
        self.assertEqual(len(self.send_calls),1)

    def test_empty_command_observation_does_not_mean_permission_to_resend(self):
        self.send_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.runtime.dispatch(self.prepare())
        self.assertIsNone(self.runtime.observe(self.task_ref()))
        with self.assertRaises(MutationConsumed): self.prepare()
        self.assertEqual(len(self.send_calls),1)

    def test_lost_lease_write_cannot_send_after_restart_or_clock_change(self):
        self.lease_mode = "timeout-before"; prepared = self.prepare()
        with self.assertRaises(DispatchUnknown): self.runtime.dispatch(prepared)
        found = self.runtime.find_task(prepared.as_dict()["task"]["taskId"])
        self.assertEqual(found["status"],"OBSERVATION_REQUIRED")
        self.assertIsNone(self.runtime.observe(found["taskReference"]))
        self.aws.now += timedelta(seconds=10); self.lease_mode = "success"
        runtime = self.new_runtime()
        again = runtime.prepare(self.manager,purpose="deployment",operation="plan",stage="foundation",attempt=1,input_reference=self.input_ref)
        self.assertEqual(again.as_dict()["task"]["taskId"],prepared.as_dict()["task"]["taskId"])
        with self.assertRaises(MutationConsumed): runtime.dispatch(again)
        self.assertEqual(self.send_calls,[])

    def test_lost_handle_publication_only_observes_original_command(self):
        self.metadata_mode = "lost-handle"
        with self.assertRaises(DispatchUnknown): self.runtime.dispatch(self.prepare())
        observed = self.runtime.observe(self.task_ref())
        self.assertEqual(observed.as_dict()["status"],"Pending")
        self.assertEqual(len(self.send_calls),1)

    def test_closed_manager_job_before_send_produces_no_new_command(self):
        prepared = self.prepare(); self.f.h.f.run.update(status="completed",conclusion="cancelled")
        with self.assertRaises(ValueError): self.runtime.dispatch(prepared)
        self.assertEqual(self.send_calls,[])

    def test_cleanup_fence_before_send_produces_no_new_command(self):
        prepared = self.prepare()
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(ValueError): self.runtime.dispatch(prepared)
        self.assertEqual(self.send_calls,[])

    def test_document_drift_or_offline_agent_is_not_ready(self):
        self.document_change = dict(Hash="b"*64)
        with self.assertRaises(ValueError): self.prepare()
        self.document_change = {}; self.offline = True
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.send_calls,[])

    def test_worker_ip_drift_invalidates_prepared_dispatch(self):
        prepared = self.prepare(); self.f.instance["PublicIpAddress"] = "8.8.4.4"
        with self.assertRaises(ValueError): self.runtime.dispatch(prepared)
        self.assertEqual(self.send_calls,[])

    def test_sender_json_cannot_create_authenticated_prepared_dispatch(self):
        with self.assertRaises(ValueError): PreparedDispatch(None,None,None,{},None)
        with self.assertRaises(ValueError): self.runtime.prepare(self.manager.as_dict(),purpose="deployment",operation="plan",stage="foundation",attempt=1,input_reference=self.input_ref)

    def test_initial_job_cannot_dispatch_apply_or_arbitrary_shell(self):
        with self.assertRaises(ValueError): self.runtime.prepare(self.manager,purpose="deployment",operation="run-shell",stage="command",attempt=1,input_reference=self.input_ref)
        ref = self.new_input("apply","foundation")
        with self.assertRaisesRegex(ValueError,"Initial plan job"):
            self.runtime.prepare(self.manager,purpose="deployment",operation="apply",stage="foundation",attempt=1,input_reference=ref)
        self.assertEqual(self.send_calls,[])

    def test_duplicate_or_mismatched_command_identity_is_not_success(self):
        self.runtime.dispatch(self.prepare()); ref = self.task_ref()
        self.commands.append(deepcopy(self.commands[0])); self.commands[1]["CommandId"] = "abcdefab-1234-1234-1234-123456789abc"
        with self.assertRaises(ValueError): self.runtime.observe(ref)
        self.commands = self.commands[:1]; self.commands[0]["InstanceIds"] = ["i-"+"9"*17]
        with self.assertRaises(ValueError): self.runtime.observe(ref)

    def test_observation_after_manager_cancellation_is_read_only(self):
        self.runtime.dispatch(self.prepare()); ref = self.task_ref(); before = len(self.puts())
        self.f.h.f.run.update(status="completed",conclusion="cancelled")
        self.assertIsNotNone(self.runtime.observe(ref))
        self.assertEqual(len(self.puts()),before)

    def test_sdk_send_and_observe_argument_shapes(self):
        import botocore.session
        from botocore.validate import validate_parameters
        self.runtime.dispatch(self.prepare()); self.runtime.observe(self.task_ref())
        model = botocore.session.get_session().get_service_model("ssm")
        operations = dict(describe_document="DescribeDocument",describe_instance_information="DescribeInstanceInformation",list_commands="ListCommands")
        for name,params in self.ssm_calls:
            if name in operations: validate_parameters(params,model.operation_model(operations[name]).input_shape)
        for params in self.send_calls: validate_parameters(params,model.operation_model("SendCommand").input_shape)

    def new_input(self,operation,stage,attempt=1):
        key = f"{PREFIX}inputs/{self.aws.execution['sessionId']}/{operation}/{stage}/{attempt}.json"
        inputs = dict(schemaVersion=2,executionReference=self.aws.ref,operation=operation,stage=stage,attempt=attempt,parameters={})
        obj = self.aws.publish(key,encoded(inputs))
        return dict(bucket=BUCKET,key=key,versionId=obj["VersionId"],sha256=checksum(inputs))

    def test_service_success_without_worker_receipt_is_not_completion(self):
        self.runtime.dispatch(self.prepare()); ref = self.task_ref()
        self.assertIsNone(self.runtime.successful_result(ref))
        self.commands[0]["Status"] = "Success"
        with self.assertRaises(Exception) as caught: self.runtime.successful_result(ref)
        self.assertEqual(caught.exception.response["Error"]["Code"],"NoSuchKey")

    def test_terminal_service_and_exact_worker_receipt_allow_next_task_only(self):
        self.runtime.dispatch(self.prepare()); ref = self.task_ref(); observed = self.runtime.observe(ref)
        self.aws.now += timedelta(seconds=2)
        evidence_ref = dict(bucket=BUCKET,key=f"{PREFIX}evidence/{self.aws.execution['sessionId']}/{observed._envelope['task']['taskId']}.json",versionId="evidence-version",sha256="a"*64)
        receipt = dict(schemaVersion=2,handle=observed._handle,workerIdentitySha256=checksum(observed._worker),outcome="SUCCEEDED",
                       startedAt=wire_time(self.commands[0]["RequestedDateTime"]),finishedAt=wire_time(self.aws.now),evidenceReference=evidence_ref)
        key = f"{PREFIX}results/{self.aws.execution['sessionId']}/{observed._envelope['task']['taskId']}/{observed._handle['commandId']}.json"
        obj = self.aws.publish(key,encoded(receipt),result_tags(observed._envelope,observed._worker))
        receipt_ref = dict(bucket=BUCKET,key=key,versionId=obj["VersionId"],sha256=checksum(receipt))
        record = self.runtime._read_lease("deployment")[0]
        record.update(phase="FINISHED",ownerNonce="b"*32,resultReference=receipt_ref,revision=3,mutationId="c"*32)
        self.aws.publish(self.lease_key,encoded(record),lease_tags(record))
        next_input = self.new_input("plan","foundation",2)
        with self.assertRaises(ValueError): self.runtime.prepare(self.manager,purpose="deployment",operation="plan",stage="foundation",attempt=2,input_reference=next_input)
        self.commands[0]["Status"] = "Success"
        self.assertEqual(self.runtime.successful_result(ref)["businessAcceptance"],"NOT_ESTABLISHED")
        prepared = self.runtime.prepare(self.manager,purpose="deployment",operation="plan",stage="foundation",attempt=2,input_reference=next_input)
        self.runtime.dispatch(prepared)
        self.assertEqual(len(self.send_calls),2)

    def test_current_ssm_service_pages_must_be_complete(self):
        self.runtime.dispatch(self.prepare()); ref = self.task_ref()
        for page in (dict(Commands=[],NextToken="repeat",ResponseMetadata=dict(HTTPStatusCode=200,RequestId="list-request")),
                     dict(ResponseMetadata=dict(HTTPStatusCode=200,RequestId="list-request")),
                     dict(Commands=[dict(self.commands[0],Parameters=None)],ResponseMetadata=dict(HTTPStatusCode=200,RequestId="list-request"))):
            self.list_override = page
            with self.assertRaises(ValueError): self.runtime.observe(ref)

    def test_unscoped_task_reference_is_rejected_before_sdk_read(self):
        before = len(self.aws.calls)
        with self.assertRaises(ValueError): self.runtime.observe(dict(bucket=BUCKET,key="other-project/task.json",versionId="version",sha256="a"*64))
        # Actual STS role is rechecked, but no unscoped S3 read is issued.
        self.assertFalse(any(op == "get_object" and p["Key"] == "other-project/task.json" for n,op,p in self.aws.calls[before:]))

    def test_dispatch_deadline_expiry_never_frees_active_slot(self):
        prepared = self.prepare()
        # A shorter admitted deadline exercises task expiry while the original
        # manager job and credential remain valid for observation/new requests.
        prepared._envelope["task"]["expiresAt"] = wire_time(self.aws.now+timedelta(seconds=60))
        self.runtime.dispatch(prepared); self.aws.now += timedelta(seconds=70)
        with self.assertRaises(MutationConsumed): self.prepare()
        self.assertEqual(self.runtime._read_lease("deployment")[0]["phase"],"DISPATCHING")
        self.assertEqual(len(self.send_calls),1)

    def test_unreviewed_ssm_endpoint_or_sdk_retry_policy_is_rejected(self):
        self.clients["ssm"].meta.endpoint_url = "http://localhost:4566"
        with self.assertRaises(ValueError): self.new_runtime()
        self.clients["ssm"].meta.endpoint_url = f"https://ssm.{REGION}.amazonaws.com"
        self.clients["ssm"].meta.config.retries["total_max_attempts"] = 2
        with self.assertRaises(ValueError): self.new_runtime()
        self.assertEqual(self.send_calls,[])

    def test_document_owner_and_parameter_surface_are_fixed(self):
        for changed in (dict(Owner="other-account"),dict(Parameters=[dict(Name="commands",Type="StringList")]),dict(PlatformTypes=["Windows"])):
            self.document_change = changed
            with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(self.send_calls,[])

    def test_linux_compatibility_does_not_depend_on_platform_list_order(self):
        self.document_change = dict(PlatformTypes=["MacOS","Linux"])
        self.assertIsInstance(self.prepare(),PreparedDispatch)


if __name__ == "__main__": unittest.main()
