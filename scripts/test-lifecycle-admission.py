from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest

import botocore.session
from botocore.validate import validate_parameters

from lifecycle_admission import ENVIRONMENT, REPOS, WORKFLOW, StartWorkflow, typed_inputs
from plan_review_store import checksum
from automation_session import utc

spec = importlib.util.spec_from_file_location("admission_coordinator_support", Path(__file__).with_name("test-lifecycle-coordinator.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f = support.CoordinatorTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.sources = {"techx-infra": "a" * 40, "techx-platform": "b" * 40, "techx-chart": "c" * 40}
        self.promotion = dict(schemaVersion=1, repository=REPOS["techx-chart"], appId=12345,
            appSlug="techx-promotion", installationId=67890, policySha256="e" * 64)
        self.lifecycle = dict(appId=23456, appSlug="techx-lifecycle", installationId=78901)
        self.inputs = dict(action="deploy-test-destroy", platform_sha="b" * 40, chart_sha="c" * 40,
            duration_hours="4", approved_budget_usd="60", budget_alert_email="owner@example.invalid")
        self.run_data = dict(id=100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main", head_sha="a" * 40,
            path=WORKFLOW, created_at=(self.f.now - timedelta(minutes=1)).isoformat(),
            repository=dict(full_name=REPOS["techx-infra"]), head_repository=dict(full_name=REPOS["techx-infra"]))
        self.approvals = [dict(state="approved", environments=[dict(name=ENVIRONMENT, id=1)], user=dict(type="User", id=1, login="reviewer"))]
        self.main = deepcopy(self.sources)
        self.ci = {name: [dict(id=101, status="completed", conclusion="success", head_branch="main", head_sha=self.sources[name], event="push",
            path=".github/workflows/verify.yml", repository=dict(full_name=repo), head_repository=dict(full_name=repo))] for name, repo in REPOS.items()}
        def github(name, endpoint):
            if endpoint == "actions/runs/100": return deepcopy(self.run_data)
            if endpoint.endswith("/approvals"): return deepcopy(self.approvals)
            if endpoint == "git/ref/heads/main": return dict(object=dict(sha=self.main[name]))
            if endpoint.startswith("actions/workflows/verify.yml/runs?"): return dict(total_count=len(self.ci[name]), workflow_runs=deepcopy(self.ci[name]))
            raise AssertionError(endpoint)
        self.githubs = {name: SimpleNamespace(get=lambda endpoint, name=name: github(name, endpoint)) for name in REPOS}
        self.kms = "arn:aws:kms:us-east-1:058114477594:key/12345678-1234-1234-1234-123456789abc"
        self.encryption = dict(ServerSideEncryptionConfiguration=dict(Rules=[dict(BucketKeyEnabled=True,
            ApplyServerSideEncryptionByDefault=dict(SSEAlgorithm="aws:kms", KMSMasterKeyID=self.kms))]))
        self.key = dict(Arn=self.kms, AWSAccountId="058114477594", KeyState="Enabled", KeyManager="CUSTOMER", KeyUsage="ENCRYPT_DECRYPT")
        self.clients = self.f.coordinator.clients
        self.clients["sts"].get_caller_identity = lambda: dict(Account="058114477594",
            Arn="arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-100")
        self.clients["s3"].get_bucket_encryption = lambda **kw: deepcopy(self.encryption)
        self.used = dict(Contents=[], IsTruncated=False)
        self.clients["s3"].list_objects_v2 = lambda **kw: deepcopy(self.used)
        self.clients["kms"] = SimpleNamespace(describe_key=lambda **kw: dict(KeyMetadata=deepcopy(self.key)))
        self.origins = dict(IsTruncated=False, Quantity=0)
        self.functions = dict(Quantity=0)
        self.logs = dict(logGroups=[])
        cf = botocore.session.get_session().get_service_model("cloudfront")
        def origins(**kw):
            validate_parameters(kw, cf.operation_model("ListVpcOrigins").input_shape)
            return dict(VpcOriginList=deepcopy(self.origins))
        def functions(**kw):
            validate_parameters(kw, cf.operation_model("ListFunctions").input_shape)
            return dict(FunctionList=deepcopy(self.functions))
        self.clients["cloudfront"] = SimpleNamespace(list_vpc_origins=origins, list_functions=functions)
        self.clients["logs"] = SimpleNamespace(describe_log_groups=lambda **kw: deepcopy(self.logs))
        self.calls, self.checkouts, self.packed = [], [], []
        self.f.inventory["checkedAt"] = self.f.now.isoformat()
        def process(command, **kw):
            self.calls.append(command)
            if command[0] == "python" and command[1].endswith("public-dns.py"): return SimpleNamespace(stdout=b"DNS confirmed")
            if "-File" in command and command[command.index("-File") + 1].endswith("cost-estimate.ps1"):
                return SimpleNamespace(stdout=json.dumps(self.f.estimate).encode())
            if "-File" in command and command[command.index("-File") + 1].endswith("domain-vpn-inventory.ps1"):
                return SimpleNamespace(stdout=json.dumps(self.f.inventory).encode())
            raise AssertionError(command)
        def pack(repositories, path):
            self.packed.append(repositories)
            path.write_bytes(b"source; archive extraction is independently tested")
            return "b" * 64
        self.flow = StartWorkflow(self.githubs, 100, "a" * 40, self.f.directory.name, clients=self.clients,
            promotion_check=lambda: deepcopy(self.promotion),
            lifecycle_check=lambda: deepcopy(self.lifecycle),
            store=self.f.coordinator.store, bucket="control-bucket", kms_key=self.kms, clock=lambda: self.f.now, run=process,
            checkout=lambda *args: self.checkouts.append(args), bundle_pack=pack,
            bundle_extract=lambda *args: deepcopy(self.sources), open_url=lambda *args, **kw: io.BytesIO(b"8.8.8.8\n"))

    def start(self, request=None, inputs=None, digest=None):
        request = self.flow.preview(self.inputs) if request is None else request
        return self.flow.start(inputs or self.inputs, request, digest or checksum(request))

    def test_preview_is_cloud_free_and_omits_email_while_start_arms_cleanup_before_paid_dispatch(self):
        clients = self.flow.clients
        self.flow.clients = None
        request = self.flow.preview(self.inputs)
        self.flow.clients = clients
        self.assertNotIn("budgetAlertEmail", request["record"])
        self.assertNotIn(self.inputs["budget_alert_email"], json.dumps(request))
        self.assertFalse(self.f.events)
        self.assertEqual(utc(request["record"]["cleanupStartAt"]).microsecond, 0)
        result = self.start(request)
        self.assertLess(self.f.events.index("schedule"), self.f.events.index("start-build"))
        self.assertEqual(len(self.checkouts), 3)
        self.assertEqual(set(self.packed[0]), set(REPOS))
        self.assertEqual({name: value[1] for name, value in self.packed[0].items()}, self.sources)
        self.assertEqual(self.f.current["sourceAdmission"]["reviewer"], "reviewer")
        self.assertEqual(self.f.current["sourceAdmission"]["promotionAuthority"], request["promotionAuthority"])
        self.assertEqual(self.f.current["sourceAdmission"]["lifecycleIdentity"], request["lifecycleIdentity"])
        self.assertEqual(self.f.current["deadline"], request["record"]["deadline"])
        self.assertEqual(result["deadline"], request["record"]["deadline"])

    def test_missing_human_approval_bot_or_rerun_cannot_reserve_or_launch(self):
        request = self.flow.preview(self.inputs)
        original = deepcopy(self.approvals)
        for approvals in ([], [dict(original[0], state="rejected")], [dict(original[0], user=dict(type="Bot", id=1, login="bot"))], original * 2):
            self.approvals = approvals
            with self.subTest(approvals=approvals), self.assertRaises(ValueError): self.start(request)
        self.approvals = original
        self.run_data["run_attempt"] = 2
        with self.assertRaises(ValueError): self.start(request)
        self.assertFalse(self.f.events)
        self.assertFalse(self.packed)

    def test_inputs_changed_after_preview_cannot_increase_budget_or_extend_window(self):
        request = self.flow.preview(self.inputs)
        for key, value in (("approved_budget_usd", "80"), ("duration_hours", "5"), ("platform_sha", "f" * 40), ("action", "deploy-window")):
            with self.subTest(key=key), self.assertRaises(ValueError): self.start(request, dict(self.inputs, **{key: value}))
        bad = deepcopy(request)
        bad["record"]["approvedBudgetUsd"] = 80
        with self.assertRaises(ValueError): self.start(bad, digest=checksum(request))
        self.assertFalse(self.f.events)

    def test_review_wait_uses_original_deadline_and_expired_window_cannot_be_reopened(self):
        request = self.flow.preview(self.inputs)
        deadline = request["record"]["deadline"]
        self.f.now += timedelta(minutes=20)
        self.f.inventory["checkedAt"] = self.f.now.isoformat()
        self.start(request)
        self.assertEqual(self.f.current["deadline"], deadline)
        self.f.events.clear()
        self.f.now += timedelta(hours=4)
        with self.assertRaises(ValueError): self.start(request)
        self.assertFalse(self.f.events)

    def test_main_movement_or_new_failed_ci_blocks_start_instead_of_using_old_green(self):
        request = self.flow.preview(self.inputs)
        self.main["techx-platform"] = "f" * 40
        with self.assertRaises(ValueError): self.start(request)
        self.main["techx-platform"] = self.sources["techx-platform"]
        self.ci["techx-chart"].append(dict(self.ci["techx-chart"][0], id=102, conclusion="failure"))
        with self.assertRaises(ValueError): self.start(request)
        self.assertFalse(self.f.events)

    def test_wrong_bucket_kms_or_disabled_key_cannot_write_or_start(self):
        request = self.flow.preview(self.inputs)
        self.flow.bucket = "other-bucket"
        with self.assertRaises(ValueError): self.start(request)
        self.flow.bucket = "control-bucket"
        self.key["KeyState"] = "PendingDeletion"
        with self.assertRaises(ValueError): self.start(request)
        self.key["KeyState"] = "Enabled"
        self.encryption["ServerSideEncryptionConfiguration"]["Rules"][0]["ApplyServerSideEncryptionByDefault"]["KMSMasterKeyID"] = "wrong-key"
        with self.assertRaises(ValueError): self.start(request)
        self.assertFalse(self.f.events)

    def test_old_execution_or_nonempty_incomplete_inventory_cannot_start(self):
        request = self.flow.preview(self.inputs)
        self.used = dict(Contents=[dict(Key="executions/staging-gh-100.json")], IsTruncated=False)
        with self.assertRaises(ValueError): self.start(request)
        self.used = dict(Contents=[], IsTruncated=False)
        self.f.inventory["ebsVolumes"] = ["vol-leftover"]
        with self.assertRaises(ValueError): self.start(request)
        self.f.inventory["ebsVolumes"] = []
        self.f.inventory["checkedAt"] = (self.f.now - timedelta(minutes=6)).isoformat()
        with self.assertRaises(ValueError): self.start(request)
        self.assertFalse(self.f.events)

    def test_orphan_vpc_origin_functions_or_logs_block_even_with_zero_tf_resources(self):
        for target, field, value in ((self.origins, "Items", [dict(Name="techx-staging-private")]),
                (self.functions, "Items", [dict(Name="techx-staging-route")]), (self.logs, "logGroups", [dict(logGroupName="/aws/eks/techx-staging/cluster")]),
                (self.functions, "NextMarker", "next-page")):
            target[field] = value
            if field == "Items": target["Quantity"] = 1
            with self.subTest(field=field), self.assertRaises(ValueError): self.start()
            target.pop(field)
            if field == "Items": target["Quantity"] = 0
            if field == "logGroups": target[field] = []
        self.assertFalse(self.f.events)

    def test_bad_dns_or_source_bundle_does_not_dispatch_and_leaves_no_session_reservation(self):
        process = self.flow.run
        def dns_error(command, **kw):
            if command[0] == "python": raise subprocess.CalledProcessError(1, command)
            return process(command, **kw)
        self.flow.run = dns_error
        with self.assertRaises(subprocess.CalledProcessError): self.start()
        self.flow.run = process
        self.flow.extract = lambda *args: {name: "f" * 40 for name in REPOS}
        with self.assertRaises(ValueError): self.start()
        self.assertFalse(self.f.events)

    def test_invalid_numbers_and_extra_inputs_fail_before_source_or_aws_actions(self):
        for key, value in (("duration_hours", "25"), ("duration_hours", "3.5"), ("approved_budget_usd", "NaN"),
                ("approved_budget_usd", "81"), ("approved_budget_usd", True), ("platform_sha", "main")):
            with self.subTest(key=key), self.assertRaises(ValueError): typed_inputs(dict(self.inputs, **{key: value}))
        with self.assertRaises(ValueError): typed_inputs(dict(self.inputs, arbitrary_command="whoami"))
        self.assertFalse(self.f.events)

    def test_estimator_rejection_explains_the_budget_error_without_any_cloud_mutation(self):
        self.flow.run = lambda command, **kw: (_ for _ in ()).throw(subprocess.CalledProcessError(1, command,
            stderr=b"Estimated upper bound exceeds the approved budget."))
        with self.assertRaisesRegex(ValueError, "upper bound exceeds"): self.flow.preview(self.inputs)
        self.assertFalse(self.f.events)

    def test_opening_review_binds_public_app_authority_and_rejects_a_changed_app_before_aws_reads(self):
        request = self.flow.preview(self.inputs)
        self.assertEqual(request["promotionAuthority"], self.promotion)
        self.promotion["installationId"] += 1
        def forbidden(): raise AssertionError("No AWS identity read may precede rejected App authority")
        self.clients["sts"].get_caller_identity = forbidden
        with self.assertRaisesRegex(ValueError, "changed after opening preview"): self.start(request)
        self.assertFalse(self.f.events)
        self.assertFalse(self.packed)

    def test_app_policy_changed_during_preparation_blocks_reservation_and_build(self):
        request = self.flow.preview(self.inputs)
        count = 0
        def changed():
            nonlocal count
            count += 1
            return deepcopy(self.promotion) if count == 1 else dict(self.promotion, policySha256="f" * 64)
        self.flow.promotion_check = changed
        with self.assertRaisesRegex(ValueError, "changed before reservation"): self.start(request)
        self.assertEqual(count, 2)
        self.assertFalse(self.f.events)

    def test_missing_or_private_bearing_preflight_proof_cannot_publish_an_opening_preview(self):
        self.flow.promotion_check = lambda: dict(self.promotion, privateKey="never-public")
        with self.assertRaisesRegex(ValueError, "public promotion authority"): self.flow.preview(self.inputs)
        self.assertFalse(self.f.events)

    def test_lifecycle_identity_change_cannot_open_or_replace_the_promotion_app(self):
        request = self.flow.preview(self.inputs)
        self.lifecycle["installationId"] += 1
        with self.assertRaisesRegex(ValueError, "Lifecycle App changed after"): self.start(request)
        self.assertFalse(self.f.events)
        self.lifecycle["appId"] = self.promotion["appId"]
        with self.assertRaisesRegex(ValueError, "separate dedicated Apps"): self.flow.preview(self.inputs)

    def test_lifecycle_change_before_reservation_has_no_paid_successor(self):
        request = self.flow.preview(self.inputs)
        calls = 0
        def changed():
            nonlocal calls
            calls += 1
            return deepcopy(self.lifecycle) if calls == 1 else dict(self.lifecycle, installationId=999)
        self.flow.lifecycle_check = changed
        with self.assertRaisesRegex(ValueError, "Lifecycle App changed before"): self.start(request)
        self.assertFalse(self.f.events)


if __name__ == "__main__": unittest.main()
