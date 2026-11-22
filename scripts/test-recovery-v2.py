from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import hashlib
import json
import runpy
import unittest

from execution_v2 import BUCKET, PREFIX
from recovery_v2 import begin_launch, checksum, observe_launch, prepare_launch, validate_successful_result
from worker_identity import launch_token


class RecoveryContracts(unittest.TestCase):
    def setUp(self):
        # Reuse synthetic wire fixtures without importing/rerunning their suite.
        self.f = runpy.run_path(str(Path(__file__).with_name("test-execution-v2.py")))["V2Contracts"]()
        self.f.setUp()
        f = self.f
        self.prepared = prepare_launch(f.intent, f.execution, f.ref, 1)
        self.calling = begin_launch(self.prepared, f.intent, f.execution, f.ref, current_epoch=1, now=f.now)
        self.worker = f.worker()
        self.command = dict(f.command, Status="Success")
        self.invocation = dict(CommandId=f.handle["commandId"], InstanceId=f.task["instanceId"],
                               DocumentName=f.task["documentName"], DocumentVersion=f.task["documentVersion"],
                               PluginName="runTask", Status="Success", StatusDetails="Success", ResponseCode=0,
                               ExecutionStartDateTime="2026-11-22T01:00:01.000Z", ExecutionEndDateTime="2026-11-22T01:00:05Z")
        self.result = dict(schemaVersion=2, handle=deepcopy(f.handle), workerIdentitySha256=checksum(self.worker), outcome="SUCCEEDED",
                           startedAt="2026-11-22T01:00:02Z", finishedAt="2026-11-22T01:00:04Z",
                           evidenceReference=dict(bucket=BUCKET, key=f"{PREFIX}evidence/{f.task['sessionId']}/{f.task['taskId']}.json",
                                                  versionId="evidence-version", sha256="f" * 64))

    def observe(self, checkpoint=None, instance=None, caller=None, now=None):
        f = self.f
        return observe_launch(checkpoint if checkpoint is not None else self.calling, f.intent, f.execution, f.ref,
                              instance=instance if instance is not None else f.instance,
                              caller=caller if caller is not None else f.worker_caller, now=now or f.now)

    def success(self, result=None, invocation=None, command=None, ref_changes=None):
        f = self.f
        body = json.dumps(result if result is not None else self.result).encode()
        ref = dict(bucket=BUCKET, key=f"{PREFIX}results/{f.task['sessionId']}/{f.task['taskId']}/{f.handle['commandId']}.json",
                   versionId="result-version", sha256=hashlib.sha256(body).hexdigest())
        ref.update(ref_changes or {})
        return validate_successful_result(body, ref, f.handle, f.task, self.worker,
                                           command=command if command is not None else self.command,
                                           invocation=invocation if invocation is not None else self.invocation,
                                           now=f.now + timedelta(minutes=1))

    def test_calling_is_single_consumption_and_does_not_mutate_input(self):
        self.assertEqual(self.prepared["phase"], "PREPARED")
        self.assertEqual(self.calling["phase"], "CALLING")
        for checkpoint in [self.calling, self.observe()]:
            with self.subTest(checkpoint=checkpoint["phase"]), self.assertRaises(ValueError):
                begin_launch(checkpoint, self.f.intent, self.f.execution, self.f.ref,
                             current_epoch=1, now=self.f.now + timedelta(hours=1))

    def test_prepared_cannot_skip_calling_or_invent_response(self):
        with self.assertRaises(ValueError): self.observe(self.prepared)
        for changes in [dict(instanceId=self.f.instance["InstanceId"]), dict(observedAt="2026-11-22T01:00:00Z"),
                        dict(workerIdentitySha256="a" * 64), dict(callStartedAt="2026-11-22T01:00:00Z")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                begin_launch(dict(self.prepared, **changes), self.f.intent, self.f.execution, self.f.ref,
                             current_epoch=1, now=self.f.now)

    def test_old_epoch_unknown_phase_and_modified_intent_refused(self):
        for changes in [dict(phase="UNKNOWN"), dict(launchToken="f" * 64), dict(schemaVersion=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.observe(dict(self.calling, **changes))
        with self.assertRaises(ValueError):
            begin_launch(self.prepared, self.f.intent, self.f.execution, self.f.ref, current_epoch=2, now=self.f.now)
        changed = dict(self.f.intent, configurationReference=dict(self.f.intent["configurationReference"], versionId="other"))
        with self.assertRaises(ValueError): prepare_launch(changed, self.f.execution, self.f.ref, 1)

    def test_scope_pinned_configuration_reference(self):
        for changes in [dict(bucket="another-bucket"), dict(versionId="null"), dict(key="automation/staging/config.json")]:
            intent = dict(self.f.intent, configurationReference=dict(self.f.intent["configurationReference"], **changes))
            intent["clientToken"] = launch_token(intent)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                prepare_launch(intent, self.f.execution, self.f.ref, 1)

    def test_deployment_and_probe_cannot_start_in_cleanup_reserve(self):
        with self.assertRaises(ValueError):
            begin_launch(self.prepared, self.f.intent, self.f.execution, self.f.ref,
                         current_epoch=1, now=self.f.now + timedelta(hours=8))
        with self.assertRaises(ValueError):
            begin_launch(self.prepared, self.f.intent, self.f.execution, self.f.ref,
                         current_epoch=1, now=self.f.now - timedelta(seconds=1))

    def test_only_exact_positive_worker_observation_resolves_calling(self):
        observed = self.observe()
        self.assertEqual(observed["phase"], "OBSERVED")
        self.assertEqual(observed["instanceId"], self.f.instance["InstanceId"])
        self.assertEqual(self.calling["phase"], "CALLING")
        self.assertEqual(self.observe(observed, now=self.f.now + timedelta(minutes=1)), observed)
        for instance in [{}, dict(self.f.instance, ClientToken="f" * 64), dict(self.f.instance, State=dict(Name="terminated"))]:
            with self.subTest(instance=instance), self.assertRaises(ValueError): self.observe(instance=instance)
        with self.assertRaises(ValueError): self.observe(caller=self.f.manager_caller)

    def test_observed_cannot_silently_switch_boot_or_public_ip(self):
        observed = self.observe()
        for changes in [dict(PublicIpAddress="8.8.4.4"), dict(LaunchTime=self.f.now + timedelta(seconds=1))]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.observe(observed, instance=dict(self.f.instance, **changes), now=self.f.now + timedelta(minutes=1))

    def test_instance_must_be_launched_after_the_persisted_call(self):
        later = dict(self.calling, callStartedAt="2026-11-22T01:00:01Z")
        with self.assertRaises(ValueError): self.observe(later, now=self.f.now + timedelta(minutes=1))
        with self.assertRaises(ValueError): self.observe(now=self.f.now - timedelta(seconds=1))

    def test_durable_success_requires_aws_and_private_receipt(self):
        self.assertEqual(self.success(), self.result)
        self.assertEqual(self.success()["handle"]["commandId"], self.f.handle["commandId"])

    def test_receipt_cannot_claim_different_command_worker_or_outcome(self):
        for changes in [dict(handle=dict(self.f.handle, commandId="abcdefab-1234-1234-1234-123456789abc")),
                        dict(handle=dict(self.f.handle, leaseEpoch=True)),
                        dict(workerIdentitySha256="a" * 64), dict(outcome="FAILED"), dict(outcome=True),
                        dict(schemaVersion=True), dict(pretendSuccess=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.success(dict(self.result, **changes))

    def test_result_and_evidence_must_be_exact_private_versions(self):
        for changes in [dict(versionId="null"), dict(bucket="public-bucket"), dict(sha256="a" * 64),
                        dict(key="automation/staging/result.json")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.success(ref_changes=changes)
        for changes in [dict(versionId="null"), dict(key="wrong-evidence")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.success(dict(self.result, evidenceReference=dict(self.result["evidenceReference"], **changes)))

    def test_exit_zero_or_success_string_alone_cannot_commit_stage(self):
        for changes in [dict(Status="InProgress"), dict(Status="Failed"), dict(ResponseCode=1), dict(ResponseCode=False),
                        dict(StatusDetails="Delivery Timed Out"), dict(PluginName="arbitraryStep"), dict(DocumentVersion="4"),
                        dict(InstanceId="i-" + "f" * 17), dict(CommandId="abcdefab-1234-1234-1234-123456789abc")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.success(invocation=dict(self.invocation, **changes))
        with self.assertRaises(ValueError): self.success(invocation={})
        with self.assertRaises(ValueError): self.success(command=dict(self.command, Status="InProgress"))

    def test_receipt_and_service_times_cannot_be_future_or_reordered(self):
        for changes in [dict(startedAt="2026-11-22T01:00:00Z"), dict(finishedAt="2026-11-22T01:00:06Z"),
                        dict(finishedAt="2026-11-22T01:00:01Z")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.success(dict(self.result, **changes))
        for changes in [dict(ExecutionStartDateTime="2026-11-22T01:00:01"),
                        dict(ExecutionEndDateTime="2026-11-22T01:01:01Z"), dict(ExecutionEndDateTime="bad")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.success(invocation=dict(self.invocation, **changes))

    def test_fractional_receipt_times_preserve_service_execution_order(self):
        result = dict(self.result, startedAt="2026-11-22T01:00:01.900123Z", finishedAt="2026-11-22T01:00:04.500Z")
        invocation = dict(self.invocation, ExecutionStartDateTime="2026-11-22T01:00:01.900000Z")
        self.assertEqual(self.success(result, invocation=invocation), result)


if __name__ == "__main__":
    unittest.main()
