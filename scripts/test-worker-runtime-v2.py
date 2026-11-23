from copy import deepcopy
from datetime import timedelta
import hashlib
import io
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest

from execution_v2 import ACCOUNT, BUCKET, PREFIX, REGION
from session_runtime_v2 import MutationConsumed, encoded
from worker_identity import launch_token
from worker_runtime_v2 import InstanceMetadata, LiveWorker, RegistrationUncertain, WorkerRejected, WorkerRuntime

SessionHelpers = runpy.run_path(str(Path(__file__).with_name("test-session-runtime-v2.py")))
ContractHelpers = runpy.run_path(str(Path(__file__).with_name("test-execution-v2.py")))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.aws = SessionHelpers["SessionRuntimeTests"](); self.aws.setUp()
        f = ContractHelpers["V2Contracts"](); f.setUp()
        self.intent, self.instance = deepcopy(f.intent), deepcopy(f.instance)
        self.instance["MetadataOptions"].update(HttpEndpoint="enabled",HttpPutResponseHopLimit=1)
        self.intent["executionReference"] = deepcopy(self.aws.ref)
        self.intent["clientToken"] = launch_token(self.intent)
        self.instance["ClientToken"] = self.intent["clientToken"]
        self.instance["Tags"] = [dict(Key=k,Value=v) for k,v in dict(Project="TechX",Environment="automation",Role="deployment-executor",
            SessionId=self.aws.execution["sessionId"],ExecutionSha256=self.aws.ref["sha256"],LeaseEpoch="1").items()]
        self.aws.principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-deployment-executor/{self.instance['InstanceId']}"
        self.principal_id = "AROA" + "A"*17 + ":" + self.instance["InstanceId"]
        self.aws.override = lambda op,p: dict(Account=ACCOUNT,Arn=self.aws.principal,UserId=self.principal_id) if op == "get_caller_identity" else None
        self.identity_boot = "12345678-1234-1234-1234-123456789abc"
        self.metadata = SimpleNamespace(instance_id=lambda:self.instance["InstanceId"])
        self.ec2_calls = []
        class EC2:
            meta = SimpleNamespace(endpoint_url=f"https://ec2.{REGION}.amazonaws.com",region_name=REGION,
                                   config=SimpleNamespace(retries={"total_max_attempts":1}))
            def describe_instances(inner, **params):
                self.ec2_calls.append(params)
                return deepcopy(self.ec2_response)
        self.aws.clients["ec2"] = EC2()
        self.ec2_response = dict(Reservations=[dict(OwnerId=ACCOUNT,Instances=[self.instance])],
                                 ResponseMetadata=dict(HTTPStatusCode=200,RequestId="ec2-request"))
        self.publish_intent()

    def publish_intent(self):
        sid = self.aws.execution["sessionId"]
        key = f"{PREFIX}launches/{sid}/{self.intent['purpose']}/{self.intent['attempt']}/intent.json"
        raw = encoded(self.intent); obj = self.aws.publish(key,raw)
        self.intent_ref = dict(bucket=BUCKET,key=key,versionId=obj["VersionId"],sha256=hashlib.sha256(raw).hexdigest())

    def runtime(self):
        return WorkerRuntime(self.aws.clients,self.aws.ref,self.intent_ref,metadata=self.metadata,
                             read_boot_id=lambda:self.identity_boot,clock=lambda:self.aws.now)

    def test_owned_service_identity_and_create_once_registration(self):
        runtime = self.runtime(); worker = runtime.verify()
        identity = worker.require_current()
        self.assertEqual(identity["bootId"],self.identity_boot)
        self.assertEqual(identity["instanceId"],self.instance["InstanceId"])
        result = runtime.register(worker)
        self.assertEqual(result["workerIdentity"],identity)
        self.assertIn("/registrations/", result["registrationReference"]["key"])
        self.assertTrue(all(p == dict(InstanceIds=[identity["instanceId"]]) for p in self.ec2_calls))

    def test_registration_reconciles_same_boot_without_overwrite(self):
        runtime = self.runtime(); first = runtime.register(runtime.verify())
        second_runtime = self.runtime(); second = second_runtime.register(second_runtime.verify())
        self.assertEqual(first,second)
        self.assertEqual(len(self.aws.objects[first["registrationReference"]["key"]]),1)

    def test_reboot_cannot_reuse_registration_or_live_guard(self):
        runtime = self.runtime(); worker = runtime.verify(); runtime.register(worker)
        self.identity_boot = "87654321-1234-1234-1234-123456789abc"
        with self.assertRaises(WorkerRejected): worker.require_current()
        second = self.runtime()
        with self.assertRaises(MutationConsumed): second.register(second.verify())

    def test_ip_or_instance_launch_time_drift_invalidates_live_identity(self):
        runtime = self.runtime(); worker = runtime.verify()
        self.instance["PublicIpAddress"] = "8.8.4.4"
        with self.assertRaises(WorkerRejected): worker.require_current()
        self.instance["PublicIpAddress"] = "8.8.8.8"
        self.instance["LaunchTime"] += timedelta(seconds=1)
        with self.assertRaises(WorkerRejected): worker.require_current()

    def test_fake_worker_or_another_runtime_cannot_publish(self):
        runtime = self.runtime()
        with self.assertRaises(WorkerRejected): LiveWorker(None,{})
        with self.assertRaises(WorkerRejected): runtime.register({"kind":"ec2-worker"})
        other = self.runtime()
        with self.assertRaises(WorkerRejected): other.register(runtime.verify())

    def test_imds_instance_and_actual_sts_principal_must_match(self):
        self.aws.principal = self.aws.principal.replace(self.instance["InstanceId"], "i-" + "6"*17)
        with self.assertRaises(WorkerRejected): self.runtime().verify()

    def test_registration_path_uses_actual_instance_sts_user_id(self):
        runtime = self.runtime(); result = runtime.register(runtime.verify())
        self.assertTrue(result["registrationReference"]["key"].endswith(f"/registrations/{self.principal_id}.json"))
        self.principal_id = self.principal_id.replace(self.instance["InstanceId"],"i-"+"6"*17)
        with self.assertRaises(WorkerRejected): runtime.verify()

    def test_manager_credentials_cannot_construct_worker_runtime(self):
        self.aws.store("manager")
        with self.assertRaises(WorkerRejected): self.runtime()

    def test_unknown_or_multiple_ec2_results_are_rejected(self):
        runtime = self.runtime()
        for change in (dict(Reservations=[]),dict(NextToken="more"),dict(ResponseMetadata={}),
                       dict(Reservations=[dict(OwnerId=ACCOUNT,Instances=[self.instance,self.instance])])):
            original = deepcopy(self.ec2_response); self.ec2_response.update(change)
            with self.assertRaises(WorkerRejected): runtime.verify()
            self.ec2_response = original

    def test_instance_metadata_endpoint_and_hop_boundary_are_required(self):
        runtime = self.runtime()
        for hop in (None,True,2):
            self.instance["MetadataOptions"]["HttpPutResponseHopLimit"] = hop
            with self.assertRaises(WorkerRejected): runtime.verify()
        self.instance["MetadataOptions"]["HttpPutResponseHopLimit"] = 1
        self.instance["MetadataOptions"]["HttpEndpoint"] = "disabled"
        with self.assertRaises(WorkerRejected): runtime.verify()

    def test_stale_fence_or_cleanup_reserve_blocks_deployment_registration(self):
        runtime = self.runtime(); worker = runtime.verify()
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(WorkerRejected): runtime.register(worker)
        self.aws.publish_session(self.aws.record); self.aws.now += timedelta(hours=9)
        with self.assertRaises(WorkerRejected): runtime.verify()

    def test_closed_failure_status_blocks_deployment(self):
        self.aws.publish_session(dict(self.aws.record,status="FAILED_NEEDS_CLEANUP"))
        with self.assertRaises(WorkerRejected): self.runtime().verify()

    def test_same_warm_cleanup_boot_remains_observable_after_fence_and_deadline(self):
        self.intent["purpose"] = "cleanup"
        self.intent["instanceProfileArn"] = self.intent["instanceProfileArn"].replace("deployment-executor","cleanup-executor")
        self.intent["configurationReference"]["key"] = self.intent["configurationReference"]["key"].replace("deployment.json","cleanup.json")
        self.intent["clientToken"] = launch_token(self.intent)
        self.instance["ClientToken"] = self.intent["clientToken"]
        self.instance["IamInstanceProfile"]["Arn"] = self.intent["instanceProfileArn"]
        next(t for t in self.instance["Tags"] if t["Key"] == "Role")["Value"] = "cleanup-executor"
        self.aws.principal = self.aws.principal.replace("deployment-executor","cleanup-executor")
        self.publish_intent()
        runtime = self.runtime(); worker = runtime.verify(); first = runtime.register(worker)
        self.aws.publish_session(dict(self.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        self.aws.now += timedelta(hours=11)
        self.assertEqual(worker.require_current()["launchEpoch"],1)
        self.assertEqual(runtime.register(worker),first)

    def test_wrong_purpose_reference_is_not_read_as_worker_config(self):
        self.intent_ref["key"] = self.intent_ref["key"].replace("/deployment/", "/cleanup/")
        with self.assertRaises(WorkerRejected): self.runtime()

    def test_boot_changes_during_initial_service_reads_are_rejected(self):
        runtime = self.runtime(); count = []
        def read_boot():
            count.append(1)
            return self.identity_boot if len(count) == 1 else "87654321-1234-1234-1234-123456789abc"
        runtime._boot = read_boot
        with self.assertRaises(WorkerRejected): runtime.verify()

    def test_unknown_registration_write_has_no_sdk_retry_and_keeps_original(self):
        runtime = self.runtime(); worker = runtime.verify(); self.aws.claim_mode = "timeout-after"
        with self.assertRaises(RegistrationUncertain): runtime.register(worker)
        puts = [(op,p) for name,op,p in self.aws.calls if op == "put_object"]
        self.assertEqual(len(puts),1)
        self.assertEqual(len(self.aws.objects[puts[0][1]["Key"]]),1)
        self.assertEqual(puts[0][1]["IfNoneMatch"],"*")
        self.aws.claim_mode = "success"
        self.assertEqual(runtime.register(worker)["workerIdentity"],worker.as_dict())

    def test_actual_ec2_and_s3_sdk_shapes(self):
        import botocore.session
        from botocore.validate import validate_parameters
        runtime = self.runtime(); runtime.register(runtime.verify())
        session = botocore.session.get_session()
        shape = session.get_service_model("ec2").operation_model("DescribeInstances").input_shape
        for params in self.ec2_calls: validate_parameters(params,shape)
        shape = session.get_service_model("s3").operation_model("PutObject").input_shape
        for name,op,params in self.aws.calls:
            if op == "put_object": validate_parameters(params,shape)


class MetadataTests(unittest.TestCase):
    def test_fixed_v2_put_token_and_get_no_credential_metadata(self):
        calls = []
        def opener(request,timeout):
            calls.append(request)
            self.assertEqual(timeout,2)
            response = io.BytesIO(b"private-imds-token" if request.method == "PUT" else b"i-55555555555555555")
            response.status = 200; response.geturl = lambda:request.full_url
            return response
        self.assertEqual(InstanceMetadata(open_url=opener).instance_id(),"i-55555555555555555")
        self.assertEqual([r.method for r in calls],["PUT","GET"])
        self.assertEqual(calls[0].full_url,"http://169.254.169.254/latest/api/token")
        self.assertEqual(calls[0].get_header("X-aws-ec2-metadata-token-ttl-seconds"),"60")
        self.assertEqual(calls[1].get_header("X-aws-ec2-metadata-token"),"private-imds-token")

    def test_metadata_failure_never_falls_back_to_v1_or_leaks_transport_errors(self):
        calls = []
        def opener(*args,**kwargs): calls.append(1); raise TimeoutError("private-imds-token")
        with self.assertRaises(WorkerRejected) as caught: InstanceMetadata(open_url=opener).instance_id()
        self.assertEqual(len(calls),1)
        self.assertNotIn("private-imds-token",str(caught.exception))


if __name__ == "__main__": unittest.main()
