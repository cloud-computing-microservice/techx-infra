from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from automation_inputs import FIXED
from deployment_manager import DeploymentManager, PROFILE
from deployment_tasks import ACCOUNT, DOCUMENT, claim_task, create_task, finish_task, read_task
from lifecycle_coordinator import initial_record
from cleanup_schedule import definition


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.record = initial_record("staging-manager123", {key: "a" * 40 for key in ("techx-infra", "techx-chart", "techx-platform")},
            4, "owner@example.invalid", "deploy-window", self.now, approved_budget_usd=60)
        self.record["executionVersion"] = "execution-v1"
        self.config = dict(schemaVersion=1, imageId="ami-1234567890abcdef0", launchTemplateId="lt-1234567890abcdef0",
            launchTemplateVersion="1", subnetId="subnet-1234567890abcdef0", securityGroupId="sg-1234567890abcdef0",
            loaderSha256="a" * 64, imageBuildSha256="c" * 64, documentVersion="1", documentSha256="b" * 64)
        self.execution = dict(self.record, deploymentConfig=deepcopy(self.config))
        self.instance_id = "i-1234567890abcdef0"
        self.private = dict(schemaVersion=1, sessionId=self.record["sessionId"],
            terraformVars=dict(FIXED, budget_alert_email="owner@example.invalid", budget_limit_usd=60, public_access_cidrs=["8.8.8.8/32"]),
            deploymentExecutor=dict(instanceId=self.instance_id, imageId=self.config["imageId"]))
        self.instance = dict(InstanceId=self.instance_id, ImageId=self.config["imageId"], State=dict(Name="running"), InstanceType="t3.small",
            IamInstanceProfile=dict(Arn=PROFILE), SubnetId=self.config["subnetId"], SecurityGroups=[dict(GroupId=self.config["securityGroupId"])],
            MetadataOptions=dict(HttpTokens="required", HttpPutResponseHopLimit=1), PublicIpAddress="8.8.8.8",
            Tags=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="staging", Role="deployment-executor", SessionId=self.record["sessionId"]).items()])
        self.owner = f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/techx-automation-lifecycle:12345678-abcd"
        self.command_id = "12345678-1234-1234-1234-123456789abc"
        self.identity = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-manager/codebuild-test")
        self.document = dict(Name=DOCUMENT, Owner=ACCOUNT, Status="Active", DocumentType="Command", DocumentVersion="1", HashType="Sha256", Hash="b" * 64)
        self.etag, self.objects = 1, {}
        self.sent, self.observed, self.cleanup = [], [], []
        self.send_callback = None
        self.statuses = ["InProgress"]
        self.schedule = definition(self.record, "execution-v1",
            f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
            f"arn:aws:iam::{ACCOUNT}:role/techx-automation-scheduler", f"arn:aws:sqs:us-east-1:{ACCOUNT}:techx-automation-cleanup-dlq", self.now)
        self.schedule["Arn"] = f"arn:aws:scheduler:us-east-1:{ACCOUNT}:schedule/techx-staging/{self.schedule['Name']}"
        self.record["cleanupScheduleArn"] = self.schedule["Arn"]
        class Missing(Exception): pass
        class Conflict(Exception): response = dict(Error=dict(Code="PreconditionFailed"))
        self.Missing, self.Conflict = Missing, Conflict
        def get_object(**kw):
            payload, version = self.objects[kw["Key"]]
            if kw.get("VersionId"): self.assertEqual(kw["VersionId"], version)
            return dict(Body=io.BytesIO(payload), VersionId=version)
        def put_object(**kw):
            self.assertEqual(kw["ExpectedBucketOwner"], ACCOUNT)
            if kw["Key"] in self.objects: raise Conflict()
            self.objects[kw["Key"]] = kw["Body"], f"v{len(self.objects) + 1}"
            return dict(VersionId=self.objects[kw["Key"]][1])
        def write(value, etag):
            if etag != self.etag: raise Conflict()
            self.record, self.etag = deepcopy(value), self.etag + 1
        self.store = SimpleNamespace(read=lambda: (deepcopy(self.record), self.etag), write=write)
        self.ssm_model = botocore.session.get_session().get_service_model("ssm")
        def send_command(**kw):
            validate_parameters(kw, self.ssm_model.operation_model("SendCommand").input_shape)
            self.assertNotIn("lease", self.record)
            self.assertEqual(self.record["deploymentTask"]["state"], "DISPATCHING")
            self.sent.append(deepcopy(kw))
            if self.send_callback: self.send_callback()
            return dict(Command=dict(CommandId=self.command_id, InstanceIds=[self.instance_id], DocumentName=DOCUMENT,
                                     DocumentVersion="1", Parameters=kw["Parameters"]))
        def observe(**kw):
            validate_parameters(kw, self.ssm_model.operation_model("GetCommandInvocation").input_shape)
            self.observed.append(kw)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if status == "missing": raise Missing()
            if status == "Success":
                self.remote_result()
            return dict(CommandId=self.command_id, InstanceId=self.instance_id, DocumentName=DOCUMENT, DocumentVersion="1",
                        PluginName="runTask", Status=status, ResponseCode=0 if status == "Success" else -1)
        self.clients = dict(sts=SimpleNamespace(get_caller_identity=lambda: deepcopy(self.identity)),
            scheduler=SimpleNamespace(get_schedule=lambda **kw: deepcopy(self.schedule)),
            ec2=SimpleNamespace(describe_instances=lambda **kw: dict(Reservations=[dict(OwnerId=ACCOUNT, Instances=[deepcopy(self.instance)])])),
            s3=SimpleNamespace(get_object=get_object, put_object=put_object, exceptions=SimpleNamespace(NoSuchKey=Missing)),
            ssm=SimpleNamespace(describe_document=lambda **kw: dict(Document=deepcopy(self.document)), send_command=send_command,
                                get_command_invocation=observe, exceptions=SimpleNamespace(InvocationDoesNotExist=Missing)),
            **{"lambda": SimpleNamespace(invoke=lambda **kw: self.cleanup.append(kw))})
        self.manager = DeploymentManager(self.clients, self.store, self.execution, "execution-v1", "control-bucket", self.owner,
                                         clock=lambda: self.now, sleep=lambda _: None)

    def dispatch(self): return self.manager.dispatch(self.private, "plan", "foundation", 1)

    def remote_result(self):
        review = dict(key="private/staging/staging-manager123/plans/foundation/1/request.json", versionId="review-v1", sha256="c" * 64,
                      sessionId=self.record["sessionId"], stage="foundation", attempt=1)
        self.record["status"] = "PROVISIONING"
        self.record["stages"] = dict(foundation=dict(state="AWAITING_APPROVAL", attempt=1, reviewReference=review))
        self.record["pendingPlan"] = review
        self.record["deploymentTask"]["state"] = "SUCCEEDED"
        self.record.pop("lease", None)

    def test_fixed_dispatch_has_one_typed_parameter_and_exact_document_version_hash(self):
        pointer = self.dispatch()
        task = read_task(self.clients["s3"], "control-bucket", self.record, pointer["taskId"], self.now)
        self.assertEqual(task["managerArn"], self.owner)
        self.assertEqual(self.sent[0]["Parameters"], dict(TaskId=[pointer["taskId"]]))
        self.assertEqual(self.sent[0]["DocumentVersion"], "1")
        self.assertEqual(self.sent[0]["DocumentHash"], self.config["documentSha256"])
        self.assertEqual(pointer["commandId"], self.command_id)
        self.assertFalse(self.cleanup)

    def test_bootstrap_dispatch_pins_selection_and_refuses_a_second_attempt(self):
        self.record.update(status="PROVISIONING", stages=dict(foundation=dict(state="APPLIED")),
            bootstrapChart=dict(key=f"chart-selections/staging/{self.record['sessionId']}/bootstrap.json", versionId="selection-v1", sha256="c" * 64))
        pointer = self.manager.dispatch(self.private, "bootstrap", "gitops", 1)
        task = read_task(self.clients["s3"], "control-bucket", self.record, pointer["taskId"], self.now)
        self.assertEqual(task["expectedBootstrapChart"], self.record["bootstrapChart"])
        self.assertEqual(pointer["expectedBootstrapChart"], task["expectedBootstrapChart"])
        self.assertEqual(self.sent[0]["Parameters"], dict(TaskId=[pointer["taskId"]]))
        self.record["bootstrapChart"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "selection changed"):
            read_task(self.clients["s3"], "control-bucket", self.record, pointer["taskId"], self.now)

    def test_edge_dispatch_refuses_foundation_without_gitops_readiness(self):
        self.record.update(status="PROVISIONING", stages=dict(foundation=dict(state="APPLIED")))
        with self.assertRaisesRegex(ValueError, "GitOps readiness"):
            self.manager.dispatch(self.private, "plan", "edge", 1)
        self.assertFalse(self.sent)

    def test_single_consumer_cas_blocks_duplicate_delivery_and_wrong_instance(self):
        pointer = self.dispatch()
        with self.assertRaises(ValueError): claim_task(self.store, self.clients["s3"], "control-bucket", pointer["taskId"], "i-00000000", self.now)
        claim_task(self.store, self.clients["s3"], "control-bucket", pointer["taskId"], self.instance_id, self.now)
        with self.assertRaises(ValueError): claim_task(self.store, self.clients["s3"], "control-bucket", pointer["taskId"], self.instance_id, self.now)
        self.assertEqual(self.record["deploymentTask"]["state"], "RUNNING")

    def test_missing_observation_is_polled_on_same_handle_without_resend(self):
        pointer = self.dispatch()
        self.statuses = ["missing", "InProgress", "Success"]
        result = self.manager.wait(pointer, max_observations=3)
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.observed), 3)
        self.assertTrue(all(row["CommandId"] == self.command_id for row in self.observed))

    def test_lost_send_response_is_uncertain_and_requests_cleanup_never_resends(self):
        def lost(): raise TimeoutError("response lost")
        self.send_callback = lost
        with self.assertRaises(TimeoutError): self.dispatch()
        self.assertEqual(self.record["deploymentTask"]["state"], "UNCERTAIN")
        self.assertEqual(self.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(len(self.cleanup), 1)
        with self.assertRaises(ValueError): self.dispatch()
        self.assertEqual(len(self.sent), 1)

    def test_lost_handoff_cas_response_requests_cleanup_before_any_send(self):
        write = self.store.write
        def lost(value, etag):
            write(value, etag)
            if value.get("deploymentTask"): raise TimeoutError("CAS response lost")
        self.store.write = lost
        with self.assertRaises(TimeoutError): self.dispatch()
        self.assertFalse(self.sent)
        self.assertTrue(self.cleanup)

    def test_recording_command_does_not_reset_fast_remote_task_state(self):
        self.send_callback = lambda: claim_task(self.store, self.clients["s3"], "control-bucket",
            self.record["deploymentTask"]["taskId"], self.instance_id, self.now)
        pointer = self.dispatch()
        self.assertEqual(pointer["state"], "RUNNING")
        self.assertEqual(pointer["commandId"], self.command_id)

    def test_command_id_merge_recovers_write_response_loss_and_heartbeat_conflict(self):
        write = self.store.write
        failed = []
        def conflict(value, etag):
            if value.get("deploymentTask", {}).get("commandId") and not failed:
                failed.append(True)
                self.record["deploymentTask"]["state"] = "RUNNING"
                self.etag += 1
                raise self.Conflict()
            write(value, etag)
        self.store.write = conflict
        self.assertEqual(self.dispatch()["state"], "RUNNING")
        self.assertEqual(len(self.sent), 1)
        self.store.write = lambda value, etag: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.manager.record_command(self.record["deploymentTask"]["taskId"], self.command_id)["commandId"], self.command_id)

    def test_unknown_or_in_progress_previous_task_cannot_start_a_second_command(self):
        self.dispatch()
        with self.assertRaises(ValueError): self.dispatch()
        self.record["deploymentTask"]["state"] = "SUCCEEDED"
        self.statuses = ["missing"]
        with self.assertRaises(ValueError): self.dispatch()
        self.assertEqual(len(self.sent), 1)

    def test_ssm_success_without_durable_stage_result_is_not_success(self):
        pointer = self.dispatch()
        self.remote_result = lambda: None
        self.statuses = ["Success"]
        with self.assertRaises(ValueError): self.manager.wait(pointer, max_observations=1)
        self.assertTrue(self.cleanup)

    def test_observation_timeout_or_cancel_requests_cleanup_without_terminal_claim(self):
        pointer = self.dispatch()
        with self.assertRaises(TimeoutError): self.manager.wait(pointer, max_observations=2)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.cleanup)
        self.statuses = ["Cancelled"]
        with self.assertRaises(ValueError): self.manager.wait(pointer, max_observations=1)
        self.assertNotEqual(self.record["deploymentTask"]["state"], "SUCCEEDED")

    def test_bad_instance_document_role_or_schedule_is_rejected_before_dispatch(self):
        for target, key, value in ((self.instance, "SubnetId", "subnet-00000000"), (self.document, "Hash", "c" * 64),
                                  (self.identity, "Arn", "another-role"), (self.schedule, "State", "DISABLED")):
            original = target[key]
            target[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.dispatch()
            self.assertFalse(self.sent)
            self.assertNotIn("lease", self.record)
            target[key] = original

    def test_expired_or_replaced_task_cannot_be_claimed(self):
        pointer = self.dispatch()
        with self.assertRaises(ValueError): read_task(self.clients["s3"], "control-bucket", self.record, pointer["taskId"], self.now + timedelta(hours=3))
        self.record["deploymentTask"]["reference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): claim_task(self.store, self.clients["s3"], "control-bucket", pointer["taskId"], self.instance_id, self.now)

    def test_finish_requires_released_lease_and_cannot_overwrite_cleanup(self):
        pointer = self.dispatch()
        task = claim_task(self.store, self.clients["s3"], "control-bucket", pointer["taskId"], self.instance_id, self.now)
        with self.assertRaises(ValueError): finish_task(self.store, task, True, self.now)
        self.remote_result()
        self.record["deploymentTask"]["state"] = "RUNNING"
        self.record["lease"] = dict(owner="still-running")
        with self.assertRaises(ValueError): finish_task(self.store, task, True, self.now)
        self.record.pop("lease")
        finish_task(self.store, task, True, self.now)
        self.assertEqual(self.record["deploymentTask"]["state"], "SUCCEEDED")
        self.record["status"] = "DESTROYING"
        with self.assertRaises(ValueError): finish_task(self.store, task, False, self.now)
        self.assertEqual(self.record["status"], "DESTROYING")

    def test_missing_review_reference_cannot_be_a_successful_plan_result(self):
        pointer = self.dispatch()
        task = claim_task(self.store, self.clients["s3"], "control-bucket", pointer["taskId"], self.instance_id, self.now)
        self.record["stages"] = dict(foundation=dict(state="AWAITING_APPROVAL", attempt=1))
        with self.assertRaises(ValueError): finish_task(self.store, task, True, self.now)
        self.assertEqual(self.record["deploymentTask"]["state"], "RUNNING")


if __name__ == "__main__": unittest.main()
