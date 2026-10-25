import io
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation_inputs import FIXED
from lifecycle_coordinator import initial_record
import run_cleanup


class CleanupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        environment = patch.dict(os.environ, CLOUDFLARE_SECRET_ARN="arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/cloudflare-TestOnly")
        environment.start()
        self.addCleanup(environment.stop)
        self.private = dict(schemaVersion=1, sessionId="staging-123456", dnsMutationAttempted=False, validationDnsPlans=[],
            terraformVars=dict(FIXED, budget_alert_email="owner@example.invalid", public_access_cidrs=["8.8.8.8/32"]))
        class Missing(Exception): pass
        def absent_cluster(**kwargs): raise Missing()
        def credentials(**kwargs):
            self.events.append("shared-secret")
            return dict(SecretString=json.dumps(dict(zoneId="a" * 32, apiToken="fake-never-printed")))
        self.clients = dict(
            codebuild=SimpleNamespace(),
            s3=SimpleNamespace(get_object=lambda **kwargs: dict(Body=io.BytesIO(json.dumps(self.private).encode()))),
            secretsmanager=SimpleNamespace(get_secret_value=credentials, list_secrets=lambda **kwargs: dict(SecretList=[])),
            ec2=SimpleNamespace(describe_instances=lambda **kwargs: dict(Reservations=[])),
            eks=SimpleNamespace(exceptions=SimpleNamespace(ResourceNotFoundException=Missing), describe_cluster=absent_cluster))
        self.inventory = dict(accountId="058114477594", region="us-east-1", environment="staging")
        for key in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): self.inventory[key] = []
        def subprocess_run(command, **kwargs):
            if command[-1].endswith("public-dns.py"): self.events.append("dns-proof"); return SimpleNamespace(returncode=0, stdout="")
            if any("destroy-domain-vpn-environment.ps1" in part for part in command): self.events.append("teardown"); return SimpleNamespace(returncode=0)
            if any("domain-vpn-inventory.ps1" in part for part in command):
                self.events.append("inventory")
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.inventory))
            raise AssertionError(f"Unexpected process in isolated worker test: {command[0]}")
        self.runner = patch("run_cleanup.subprocess.run", side_effect=subprocess_run)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.ip = patch("run_cleanup.urlopen", side_effect=lambda *a, **k: io.BytesIO(b"8.8.8.8\n"))
        self.ip.start()
        self.addCleanup(self.ip.stop)

    def work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            args = SimpleNamespace(bucket="control-bucket")
            run_cleanup.work(args, self.clients, dict(sessionId="staging-123456"), path)
            self.assertTrue((path / "inventory.json").is_file())

    def test_no_cloudflare_credential_dependency_before_any_dns_mutation(self):
        self.work()
        self.assertNotIn("shared-secret", self.events)
        self.assertLess(self.events.index("dns-proof"), self.events.index("teardown"))

    def test_unresolved_certificate_receipt_does_not_block_deleting_workload_but_cannot_complete(self):
        with patch("run_cleanup.reconcile_certificates", return_value=({}, ["client-vpn-reference"])):
            with self.assertRaisesRegex(ValueError, "outcomes remain unproven"):
                self.work()
        self.assertIn("teardown", self.events)

    def test_unresolved_secret_receipt_prevents_completion_after_workload_deletion(self):
        with patch("run_cleanup.reconcile_secrets", return_value=([], ["vpn-pki"])):
            with self.assertRaisesRegex(ValueError, "Secret mutation outcomes remain unproven"):
                self.work()
        self.assertIn("teardown", self.events)

    def test_storefront_removal_precedes_edge_teardown(self):
        self.private["dnsMutationAttempted"] = True
        with patch("run_cleanup.remove_owned_shop", side_effect=lambda *args: self.events.append("remove-shop")):
            self.work()
        self.assertLess(self.events.index("remove-shop"), self.events.index("teardown"))

    def test_validation_dns_is_removed_only_after_certificate_teardown(self):
        name, target = "_abc123.shop.dinhminhkhoa.id.vn", "_abc.def.acm-validations.aws"
        self.private["validationDnsPlans"] = [dict(name=name, target=target)]
        client = SimpleNamespace(records=lambda _: [dict(id="b" * 32, content=target, comment="techx:staging-123456")],
                                 delete=lambda ownership: self.events.append("remove-validation"))
        with patch("run_cleanup.CloudflareDNS", return_value=client): self.work()
        self.assertLess(self.events.index("teardown"), self.events.index("shared-secret"))
        self.assertLess(self.events.index("teardown"), self.events.index("remove-validation"))

    def test_wrong_private_scope_never_starts_teardown(self):
        self.private["terraformVars"]["project"] = "other"
        with self.assertRaises(ValueError): self.work()
        self.assertFalse(self.events)

    def test_cleanup_stops_manager_then_remote_executor_before_fencing(self):
        record = dict(sessionId="staging-123456", lease=dict(owner="manager", operation="deploy"))
        with patch("run_cleanup.stop_executor", side_effect=lambda *a: self.events.append("manager-terminal")), \
             patch("run_cleanup.terminate_deployment_executors", side_effect=lambda *a: self.events.append("ec2-terminal")), \
             patch("run_cleanup.fence_session", side_effect=lambda *a, **kw: self.events.append("fence") or record):
            run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
        self.assertEqual(self.events, ["manager-terminal", "ec2-terminal", "fence"])

    def test_ec2_lease_owner_and_idle_executor_both_require_terminal_proof(self):
        instance = "i-1234567890abcdef0"
        record = dict(sessionId="staging-123456", lease=dict(owner=f"arn:aws:ec2:us-east-1:058114477594:instance/{instance}", operation="deploy"))
        with patch("run_cleanup.stop_executor") as stop, patch("run_cleanup.fence_session") as fence, \
             patch("run_cleanup.terminate_deployment_executors") as terminate:
            run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
            stop.assert_not_called()
            terminate.assert_called_once_with(self.clients["ec2"], "staging-123456", instance)
            fence.assert_called_once_with(record, "cleanup", executor_stopped=True)
        record.pop("lease")
        with patch("run_cleanup.fence_session") as fence, \
             patch("run_cleanup.terminate_deployment_executors", side_effect=TimeoutError("not terminal")) as terminate:
            with self.assertRaises(TimeoutError): run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
            terminate.assert_called_once_with(self.clients["ec2"], "staging-123456", None)
            fence.assert_not_called()

    def test_uncertain_launch_requires_positive_terminal_proof_and_keeps_it_for_retries(self):
        record = initial_record("staging-123456", {key: "a" * 40 for key in ("techx-infra", "techx-chart", "techx-platform")},
            4, "owner@example.invalid", "deploy-window", datetime.now(timezone.utc), approved_budget_usd=60)
        record["executionVersion"] = "execution-v1"
        self.private["deploymentLaunch"] = dict(clientToken="a" * 64)
        proofs, terminated = {}, []
        class Missing(Exception): pass
        def get_object(**kw):
            if kw["Key"].endswith("inputs.json"): return dict(Body=io.BytesIO(json.dumps(self.private).encode()))
            if kw["Key"] not in proofs: raise Missing()
            return dict(Body=io.BytesIO(proofs[kw["Key"]]))
        self.clients["s3"] = SimpleNamespace(get_object=get_object, put_object=lambda **kw: proofs.update({kw["Key"]: kw["Body"]}),
                                              exceptions=SimpleNamespace(NoSuchKey=Missing))
        with patch("run_cleanup.fence_session") as fence:
            with self.assertRaisesRegex(ValueError, "launch outcome is unproven"):
                run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
            fence.assert_not_called()
        self.assertFalse(proofs)
        instance = dict(InstanceId="i-1234567890abcdef0", State=dict(Name="running"), ClientToken="a" * 64,
            Tags=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="staging", Role="deployment-executor", SessionId="staging-123456").items()])
        self.clients["ec2"] = SimpleNamespace(describe_instances=lambda **kw: dict(Reservations=[dict(OwnerId="058114477594", Instances=[instance])]),
            terminate_instances=lambda **kw: terminated.append(kw), get_waiter=lambda _: SimpleNamespace(wait=lambda **kw: None))
        result = run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
        self.assertEqual(result["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(len(terminated), 1)
        self.assertTrue(proofs)
        # After EC2 evicts terminated-instance metadata, the cleanup-owned proof
        # still establishes this launch outcome; another running VM is scanned.
        self.clients["ec2"].describe_instances = lambda **kw: dict(Reservations=[])
        run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
        key = next(iter(proofs))
        changed = json.loads(proofs[key])
        changed["clientToken"] = "b" * 64
        proofs[key] = json.dumps(changed).encode()
        with self.assertRaises(ValueError): run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")

    def takeover_record(self):
        record = initial_record("staging-123456", {key: "a" * 40 for key in ("techx-infra", "techx-chart", "techx-platform")},
            4, "owner@example.invalid", "deploy-window", datetime.now(timezone.utc), approved_budget_usd=60)
        record["executionVersion"] = "execution-v1"
        return record

    def test_prepared_intent_requires_terminal_manager_then_records_no_api_proof(self):
        manager = "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:1234-abcd"
        self.private["deploymentLaunch"] = dict(protocolVersion=1, phase="PREPARED", clientToken="a" * 64, managerArn=manager)
        written = []
        self.clients["s3"].put_object = lambda **kw: written.append(kw)
        with patch("run_cleanup.stop_executor", return_value=True) as stop:
            result = run_cleanup.prepare_takeover(self.clients, self.takeover_record(), "cleanup", "control-bucket")
            stop.assert_called_once_with(self.clients["codebuild"], manager, "cleanup")
        self.assertEqual(result["status"], "FAILED_NEEDS_CLEANUP")
        proof = json.loads(written[0]["Body"])
        self.assertTrue(proof["launchNotInvoked"])
        self.assertEqual(proof["instanceIds"], [])
        self.assertEqual(proof["managerArn"], manager)
        written.clear()
        with patch("run_cleanup.stop_executor", side_effect=TimeoutError("manager not terminal")), patch("run_cleanup.fence_session") as fence:
            with self.assertRaises(TimeoutError):
                run_cleanup.prepare_takeover(self.clients, self.takeover_record(), "cleanup", "control-bucket")
            fence.assert_not_called()
        self.assertFalse(written)

    def test_snapshot_is_refreshed_after_stopping_manager_before_any_no_api_conclusion(self):
        manager = "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:1234-abcd"
        class Missing(Exception): pass
        def get_object(**kw):
            if not kw["Key"].endswith("inputs.json"): raise Missing()
            return dict(Body=io.BytesIO(json.dumps(self.private).encode()))
        self.clients["s3"].get_object = get_object
        self.clients["s3"].exceptions = SimpleNamespace(NoSuchKey=Missing)
        for prepared in (False, True):
            self.private.pop("deploymentLaunch", None)
            if prepared:
                self.private["deploymentLaunch"] = dict(protocolVersion=1, phase="PREPARED", clientToken="a" * 64, managerArn=manager)
            def stop(*args):
                # The API may be invoked between our first read and stopping
                # the process. Its response can be lost and EC2 still empty.
                self.private["deploymentLaunch"] = dict(protocolVersion=1, phase="CALLING", clientToken="a" * 64, managerArn=manager)
                return True
            record = self.takeover_record()
            record["lease"] = dict(owner=manager, operation="deploy", epoch=1)
            with self.subTest(prepared=prepared), patch("run_cleanup.stop_executor", side_effect=stop), patch("run_cleanup.fence_session") as fence:
                with self.assertRaisesRegex(ValueError, "launch outcome is unproven"):
                    run_cleanup.prepare_takeover(self.clients, record, "cleanup", "control-bucket")
                fence.assert_not_called()

    def test_prepared_intent_cannot_bypass_manager_identity_or_conflicting_vm_proof(self):
        self.private["deploymentLaunch"] = dict(protocolVersion=1, phase="PREPARED", clientToken="a" * 64,
            managerArn="arn:aws:codebuild:us-east-1:058114477594:build/other:1234")
        with self.assertRaisesRegex(ValueError, "fixed lifecycle manager"):
            run_cleanup.prepare_takeover(self.clients, self.takeover_record(), "cleanup", "control-bucket")
        self.private["deploymentLaunch"]["managerArn"] = "arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-lifecycle:1234"
        self.private["deploymentExecutor"] = dict(instanceId="i-1234567890abcdef0")
        with patch("run_cleanup.stop_executor", return_value=True):
            with self.assertRaisesRegex(ValueError, "conflicts with an executor"):
                run_cleanup.prepare_takeover(self.clients, self.takeover_record(), "cleanup", "control-bucket")

    def test_snapshot_instance_reference_is_required_even_without_a_legacy_launch_intent(self):
        self.private["deploymentExecutor"] = dict(instanceId="i-1234567890abcdef0")
        with patch("run_cleanup.fence_session") as fence:
            with self.assertRaisesRegex(ValueError, "missing from scoped inventory"):
                run_cleanup.prepare_takeover(self.clients, self.takeover_record(), "cleanup", "control-bucket")
            fence.assert_not_called()


if __name__ == "__main__": unittest.main()
