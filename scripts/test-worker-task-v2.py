from copy import deepcopy
from datetime import timedelta, timezone
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest

from execution_v2 import ACCOUNT, BUCKET, PREFIX
from recovery_v2 import checksum
from session_runtime_v2 import MutationConsumed, encoded
from task_dispatch_v2 import DispatchUnknown, TaskRuntime
from worker_runtime_v2 import WorkerRuntime
from worker_task_v2 import ActiveTaskLease, WorkerTaskRuntime

Helpers = runpy.run_path(str(Path(__file__).with_name("test-task-dispatch-v2.py")))


class WorkerTaskTests(unittest.TestCase):
    def setUp(self):
        self.d = Helpers["DispatchTests"](); self.d.setUp()
        self.prepared = self.d.prepare()
        if getattr(self,"short_deadline",False): self.prepared._envelope["task"]["expiresAt"] = (self.d.aws.now+timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.d.runtime.dispatch(self.prepared)
        self.task_ref = self.d.task_ref(); self.task = self.prepared.as_dict()["task"]
        self.d.commands[0]["Status"] = "InProgress"
        self.boot = "12345678-1234-1234-1234-123456789abc"
        self.worker_principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-deployment-executor/{self.d.f.instance['InstanceId']}"
        self.worker_user = f"{self.d.f.principal_id}:{self.d.f.instance['InstanceId']}"
        outer = self
        class STS:
            meta = outer.d.clients["sts"].meta
            def get_caller_identity(inner):
                return dict(Account=ACCOUNT,Arn=outer.worker_principal,UserId=outer.worker_user,
                            ResponseMetadata=dict(HTTPStatusCode=200,RequestId="worker-caller"))
        base = self.d.clients["ssm"]
        class SSM:
            meta = base.meta
            def __getattr__(inner,name): return getattr(base,name)
            def get_command_invocation(inner,**params):
                result = base.get_command_invocation(**params)
                command = next(c for c in outer.d.commands if c["CommandId"] == params["CommandId"])
                result.update(Status=command["Status"],StatusDetails=command["Status"],ResponseCode=0 if command["Status"] == "Success" else -1,
                              ExecutionStartDateTime=command["RequestedDateTime"].isoformat())
                return result
        self.clients = dict(self.d.clients,sts=STS(),ssm=SSM())
        self.local = WorkerRuntime(self.clients,self.d.aws.ref,self.d.f.intent_ref,
            metadata=SimpleNamespace(instance_id=lambda:self.d.f.instance["InstanceId"]),read_boot_id=lambda:self.boot,clock=lambda:self.d.aws.now)
        self.worker = self.local.verify()
        self.tasks = TaskRuntime(self.clients,self.d.aws.ref,clock=lambda:self.d.aws.now)
        self.runtime = WorkerTaskRuntime(self.tasks)

    def claim(self): return self.runtime.claim(self.worker,self.task["taskId"])
    def evidence(self,**changes):
        value = dict(schemaVersion=2,executionReference=self.d.aws.ref,taskId=self.task["taskId"],operation=self.task["operation"],
                     stage=self.task["stage"],attempt=self.task["attempt"],payload=dict(syntheticStageEvidence=True))
        value.update(changes)
        key = f"{PREFIX}evidence/{self.task['sessionId']}/{self.task['taskId']}.json"
        obj = self.d.aws.publish(key,encoded(value))
        return dict(bucket=BUCKET,key=key,versionId=obj["VersionId"],sha256=checksum(value))
    def lease_writes(self): return [p for p in self.d.puts() if p["Key"] == self.d.lease_key]

    def test_actual_worker_claim_is_once_and_keeps_command_and_boot(self):
        active = self.claim()
        self.assertEqual(active.as_dict()["handle"]["commandId"],self.d.commands[0]["CommandId"])
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")
        self.assertIsNotNone(active.require_current())
        with self.assertRaises(MutationConsumed): self.claim()
        self.assertEqual(len(self.d.send_calls),1)

    def test_complete_worker_receipt_and_service_success_enable_manager_next_task(self):
        active = self.claim(); self.d.aws.now += timedelta(seconds=1)
        result = self.runtime.finish(active,self.evidence())
        self.assertEqual(result["businessAcceptance"],"NOT_ESTABLISHED")
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"FINISHED")
        with self.assertRaises(MutationConsumed): active.require_current()
        next_input = self.d.new_input("plan","foundation",2)
        with self.assertRaises(ValueError): self.d.runtime.prepare(self.d.manager,purpose="deployment",operation="plan",stage="foundation",attempt=2,input_reference=next_input)
        self.d.commands[0]["Status"] = "Success"; self.d.aws.now += timedelta(seconds=1)
        checked = self.d.runtime.successful_result(self.task_ref)
        self.assertEqual(checked["resultReference"],result["resultReference"])
        prepared = self.d.runtime.prepare(self.d.manager,purpose="deployment",operation="plan",stage="foundation",attempt=2,input_reference=next_input)
        self.d.runtime.dispatch(prepared)
        self.assertEqual(len(self.d.send_calls),2)

    def test_expired_owned_task_does_not_transfer_to_another_process(self):
        active = self.claim(); self.d.aws.now += timedelta(minutes=50)
        with self.assertRaises(ValueError): active.require_current()
        with self.assertRaises(MutationConsumed): self.claim()
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")

    def test_fake_sender_or_another_worker_cannot_claim(self):
        with self.assertRaises(ValueError): self.runtime.claim(self.worker.as_dict(),self.task["taskId"])
        self.worker_user = self.worker_user.replace(self.d.f.instance["InstanceId"],"i-"+"9"*17)
        with self.assertRaises(ValueError): self.claim()

    def test_boot_change_invalidates_live_ownership(self):
        active = self.claim(); self.boot = "87654321-1234-1234-1234-123456789abc"
        with self.assertRaises(ValueError): active.require_current()
        restarted = self.local.verify()
        with self.assertRaises(ValueError): self.runtime.claim(restarted,self.task["taskId"])

    def test_cleanup_epoch_and_status_fence_worker_without_github(self):
        active = self.claim()
        self.d.aws.publish_session(dict(self.d.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(ValueError): active.require_current()
        with self.assertRaises(ValueError): self.runtime.finish(active,self.evidence())

    def test_manager_cancellation_does_not_replace_worker_authority(self):
        self.d.f.h.f.run.update(status="completed",conclusion="cancelled")
        active = self.claim()
        self.assertIsNotNone(active.require_current())
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")

    def test_pending_or_terminal_ssm_invocation_cannot_acquire_owner(self):
        for status in ("Pending","Success","Failed","Cancelled"):
            self.d.commands[0]["Status"] = status
            with self.assertRaises(ValueError): self.claim()
        self.assertEqual(len(self.lease_writes()),1)

    def test_wrong_task_id_cannot_claim_current_slot(self):
        with self.assertRaises(ValueError): self.runtime.claim(self.worker,"e"*32)
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"DISPATCHING")

    def test_unknown_owned_cas_does_not_grant_new_process_execution(self):
        self.d.lease_mode = "timeout-after"
        with self.assertRaises(DispatchUnknown): self.claim()
        self.d.lease_mode = "success"
        with self.assertRaises(MutationConsumed): self.claim()
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")

    def test_unknown_before_owned_cas_still_consumes_same_semantic_claim(self):
        self.d.lease_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.claim()
        before = len(self.lease_writes()); self.d.lease_mode = "success"; self.d.aws.now += timedelta(seconds=1)
        another = WorkerTaskRuntime(TaskRuntime(self.clients,self.d.aws.ref,clock=lambda:self.d.aws.now))
        with self.assertRaises(MutationConsumed): another.claim(self.local.verify(),self.task["taskId"])
        self.assertEqual(len(self.lease_writes()),before)
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"DISPATCHING")

    def test_lost_finished_ack_reconciles_only_after_service_and_original_receipt(self):
        active = self.claim(); self.d.aws.now += timedelta(seconds=1); self.d.lease_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.runtime.finish(active,self.evidence())
        self.d.lease_mode = "success"
        with self.assertRaises(MutationConsumed): active.require_current()
        self.assertIsNone(self.d.runtime.reconcile_finished(self.d.manager,"deployment"))
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")
        self.d.commands[0]["Status"] = "Success"; self.d.aws.now += timedelta(seconds=1)
        result = self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.assertEqual(result["businessAcceptance"],"NOT_ESTABLISHED")
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"FINISHED")
        self.assertEqual(len(self.d.send_calls),1)

    def test_manager_cannot_invent_worker_ownership_for_a_successful_command(self):
        self.d.commands[0]["Status"] = "Success"
        with self.assertRaises(ValueError): self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"DISPATCHING")

    def test_evidence_is_exact_private_scope_and_operation_binding(self):
        active = self.claim()
        for change in (dict(operation="apply"),dict(attempt=True),dict(taskId="f"*32),dict(executionReference=dict(self.d.aws.ref,versionId="other"))):
            with self.assertRaises(ValueError): self.runtime.finish(active,self.evidence(**change))
        wrong = self.evidence(); wrong["key"] = "other-project/evidence.json"
        with self.assertRaises(ValueError): self.runtime.finish(active,wrong)
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")

    def test_finished_context_or_sender_json_cannot_be_reused(self):
        with self.assertRaises(ValueError): ActiveTaskLease(None,None,None,None,None)
        active = self.claim()
        with self.assertRaises(ValueError): self.runtime.finish(active.as_dict(),self.evidence())
        self.runtime.finish(active,self.evidence())
        with self.assertRaises(MutationConsumed): self.runtime.finish(active,self.evidence())

    def test_source_sdk_cas_arguments_are_conditional_and_claimed_once(self):
        import botocore.session
        from botocore.validate import validate_parameters
        active = self.claim(); self.runtime.finish(active,self.evidence())
        model = botocore.session.get_session().get_service_model("s3")
        for params in self.d.puts(): validate_parameters(params,model.operation_model("PutObject").input_shape)
        self.assertTrue(all("IfMatch" in p for p in self.lease_writes()[1:]))
        self.assertEqual(len(self.d.send_calls),1)

    def test_completion_metadata_repair_can_reconcile_lost_repair_ack(self):
        active = self.claim(); self.d.aws.now += timedelta(seconds=1); self.d.lease_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.runtime.finish(active,self.evidence())
        self.d.commands[0]["Status"] = "Success"; self.d.aws.now += timedelta(seconds=1)
        with self.assertRaises(DispatchUnknown): self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        before = len(self.d.send_calls); self.d.lease_mode = "success"
        recovered = self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.assertIsNotNone(recovered)
        self.assertEqual(self.d.runtime.reconcile_finished(self.d.manager,"deployment"),recovered)
        self.assertEqual(len(self.d.send_calls),before)
        with self.assertRaises(MutationConsumed): active.require_current()

    def test_terminal_receipt_metadata_repair_survives_original_task_deadline(self):
        self.short_deadline = True; self.setUp()
        active = self.claim(); self.d.aws.now += timedelta(seconds=1); self.d.lease_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.runtime.finish(active,self.evidence())
        self.d.lease_mode = "success"; self.d.commands[0]["Status"] = "Success"; self.d.aws.now += timedelta(seconds=70)
        result = self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.assertIsNotNone(result)
        self.assertEqual(len(self.d.send_calls),1)

    def test_receipt_timestamps_are_canonical_utc_with_non_utc_clock(self):
        self.d.aws.now = self.d.aws.now.astimezone(timezone(timedelta(hours=7)))
        active = self.claim(); self.d.aws.now += timedelta(seconds=1)
        self.runtime.finish(active,self.evidence()); self.d.commands[0]["Status"] = "Success"
        result = self.d.runtime.successful_result(self.task_ref)
        self.assertTrue(result["result"]["startedAt"].endswith("Z"))
        self.assertTrue(result["result"]["finishedAt"].endswith("Z"))

    def test_completion_proof_cannot_bypass_new_cleanup_epoch(self):
        active = self.claim(); self.d.aws.now += timedelta(seconds=1); self.d.lease_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.runtime.finish(active,self.evidence())
        self.d.lease_mode = "success"; self.d.commands[0]["Status"] = "Success"
        self.d.aws.publish_session(dict(self.d.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(ValueError): self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")

    def test_completion_repair_rejects_changed_original_claim(self):
        active = self.claim(); self.d.aws.now += timedelta(seconds=1); self.d.lease_mode = "timeout-before"
        with self.assertRaises(DispatchUnknown): self.runtime.finish(active,self.evidence())
        self.d.commands[0]["Status"] = "Success"
        with self.assertRaises(DispatchUnknown): self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.d.lease_mode = "success"
        for key,objects in self.d.aws.objects.items():
            if "/task-mutations/" not in key: continue
            value = json.loads(objects[-1]["body"])
            if value.get("kind") == "service-finalize":
                value["intendedRecord"]["workerIdentitySha256"] = "f"*64
                self.d.aws.publish(key,encoded(value),objects[-1]["tags"])
                break
        with self.assertRaises(ValueError): self.d.runtime.reconcile_finished(self.d.manager,"deployment")
        self.assertEqual(self.tasks.current_task("deployment")["phase"],"OWNED")
        self.assertEqual(len(self.d.send_calls),1)

    def test_worker_input_uses_original_version_even_after_head_changes(self):
        active = self.claim()
        new = dict(schemaVersion=2,executionReference=self.d.aws.ref,operation="plan",stage="foundation",attempt=1,parameters=dict(unreviewedChange=True))
        self.d.aws.publish(self.d.input_key,encoded(new))
        self.assertEqual(self.runtime.input_parameters(active),{})
        self.assertNotIn("unreviewedChange",self.runtime.input_parameters(active))

    def test_worker_input_cannot_be_read_using_sender_json_or_closed_lease(self):
        active = self.claim()
        with self.assertRaises(ValueError): self.runtime.input_parameters(active.as_dict())
        self.d.aws.publish_session(dict(self.d.aws.record,status="DESTROYING",leaseEpoch=2,runtimeRevision=2,lastMutationId="2"*32))
        with self.assertRaises(ValueError): self.runtime.input_parameters(active)


if __name__ == "__main__": unittest.main()
