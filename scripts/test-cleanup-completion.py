"""Cleanup producer, observer and CLI recovery against versioned S3 fixtures."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import botocore.session
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

from cleanup_completion import completion, publish_completion
from lifecycle_coordinator import initial_record
from plan_review_store import encoded
import run_cleanup


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.version, self.bucket = "execution-v1", "control-bucket"
        self.execution = initial_record("staging-complete123", {name: "a" * 40 for name in ("techx-infra", "techx-platform", "techx-chart")},
            4, "owner@example.invalid", "deploy-window", self.now - timedelta(hours=6), approved_budget_usd=60)
        self.execution["cleanupBundle"] = dict(key="bundles/" + "b" * 64 + ".tar.gz", sha256="b" * 64, versionId="bundle-v1")
        self.inventory = dict(accountId="058114477594", region="us-east-1", environment="staging")
        for field in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): self.inventory[field] = []
        self.payload = json.dumps(self.inventory, indent=2).encode()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.key = f"cleanup/{self.execution['sessionId']}/inventory-{self.digest}.json"
        self.marker_key = f"cleanup/{self.execution['sessionId']}/complete.json"
        self.current = dict(deepcopy(self.execution), status="DESTROYED", executionVersion=self.version,
            cleanupEvidence=dict(inventoryEmpty=True, artifactSha256=self.digest, key=self.key))
        self.etag, self.state_writes, self.puts = 1, [], []
        self.objects = {(self.key, "inventory-v1"): self.payload}
        self.latest = {self.key: "inventory-v1"}
        self.modified = self.now - timedelta(hours=1)
        self.lost_write, self.wrong_version, self.race = False, False, None
        self.sdk = botocore.session.get_session().get_service_model("s3")
        self.s3 = SimpleNamespace(get_object=self.get, list_objects_v2=self.list, put_object=self.put)
        self.store = SimpleNamespace(read=lambda: (deepcopy(self.current), str(self.etag)), write=self.write)

    def get(self, **args):
        validate_parameters(args, self.sdk.operation_model("GetObject").input_shape)
        self.assertEqual(args["Bucket"], self.bucket); self.assertEqual(args["ExpectedBucketOwner"], "058114477594")
        object_version = args.get("VersionId", self.latest[args["Key"]])
        return dict(Body=io.BytesIO(self.objects[(args["Key"], object_version)]),
            VersionId="wrong-version" if self.wrong_version else object_version, LastModified=self.modified)

    def list(self, **args):
        validate_parameters(args, self.sdk.operation_model("ListObjectsV2").input_shape)
        return dict(IsTruncated=False, Contents=[dict(Key=key) for key in self.latest if key.startswith(args["Prefix"])])

    def put(self, **args):
        validate_parameters(args, self.sdk.operation_model("PutObject").input_shape)
        self.puts.append(deepcopy(args))
        key = args["Key"]
        if self.race:
            self.race(args); self.race = None
        if args.get("IfNoneMatch") == "*" and key in self.latest:
            raise ClientError(dict(Error=dict(Code="PreconditionFailed")), "PutObject")
        object_version = "produced-v" + str(len(self.puts))
        self.objects[(key, object_version)] = args["Body"]
        self.latest[key] = object_version
        if self.lost_write: raise TimeoutError("Object stored, response lost")
        return dict(VersionId=object_version)

    def write(self, record, etag):
        if str(self.etag) != etag: raise ValueError("CAS conflict")
        self.current = deepcopy(record); self.etag += 1
        self.state_writes.append(deepcopy(record))

    def repair(self): return publish_completion(self.s3, self.bucket, self.execution, self.version, self.store)

    def test_missing_marker_recovers_exact_original_inventory_without_state_mutation(self):
        original = deepcopy(self.current)
        result = self.repair()
        self.assertEqual(result["inventoryVersionId"], "inventory-v1")
        self.assertEqual(result["checkedAt"], self.modified.isoformat())
        self.assertEqual(result["status"], "DESTROYED")
        self.assertEqual(self.current, original); self.assertFalse(self.state_writes)
        self.assertEqual(len(self.puts), 1); self.assertEqual(self.puts[0]["Key"], self.marker_key)
        self.assertEqual(self.puts[0]["IfNoneMatch"], "*")

    def test_lost_marker_response_is_reconciled_by_read_without_another_write(self):
        self.lost_write = True
        with self.assertRaises(TimeoutError): self.repair()
        self.lost_write = False
        self.assertEqual(self.repair()["status"], "DESTROYED")
        self.assertEqual(len(self.puts), 1)

    def test_existing_legacy_marker_remains_readable_and_is_never_overwritten(self):
        marker = dict(executionVersion=self.version, inventoryEmpty=True, artifactSha256=self.digest,
            inventoryKey=self.key, checkedAt=self.modified.isoformat())
        self.objects[(self.marker_key, "legacy-v1")] = encoded(marker); self.latest[self.marker_key] = "legacy-v1"
        self.assertEqual(self.repair()["markerVersion"], "legacy-v1")
        self.assertFalse(self.puts)

    def test_existing_marker_pins_inventory_version_even_if_latest_changes(self):
        self.repair()
        self.latest[self.key] = "bad-latest"
        self.objects[(self.key, "bad-latest")] = b"tampered"
        self.assertEqual(self.repair()["inventoryVersionId"], "inventory-v1")
        self.wrong_version = True
        with self.assertRaises(ValueError): self.repair()
        self.assertEqual(len(self.puts), 1)

    def test_invalid_or_nonempty_protected_inventory_cannot_mint_marker(self):
        self.objects[(self.key, "inventory-v1")] += b" "
        with self.assertRaisesRegex(ValueError, "inventory bytes"): self.repair()
        inventory = dict(self.inventory, clusters=["remaining-cluster"])
        payload = encoded(inventory); digest = hashlib.sha256(payload).hexdigest()
        key = f"cleanup/{self.execution['sessionId']}/inventory-{digest}.json"
        self.current["cleanupEvidence"].update(artifactSha256=digest, key=key)
        self.objects[(key, "inventory-v2")] = payload; self.latest[key] = "inventory-v2"
        with self.assertRaisesRegex(ValueError, "nonempty"): self.repair()
        self.assertFalse(self.puts)

    def test_new_session_wrong_execution_open_status_or_lease_cannot_repair(self):
        original = deepcopy(self.current)
        for field, value in (("sessionId", "staging-new-session"), ("executionVersion", "other"), ("status", "DESTROYING"),
                             ("lease", dict(owner="other")), ("sourceRevisions", dict(self.execution["sourceRevisions"], **{"techx-infra": "0" * 40}))):
            self.current = dict(deepcopy(original), **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError): self.repair()
        self.assertFalse(self.puts); self.assertFalse(self.state_writes)

    def test_original_inventory_service_time_is_required_not_current_clock(self):
        for value in (None, datetime.now(), self.now + timedelta(hours=1), self.now - timedelta(days=2)):
            self.modified = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "service timestamp"): self.repair()
        self.assertFalse(self.puts)

    def test_observer_rejects_fresh_time_claim_for_an_older_pinned_inventory(self):
        self.repair()
        version = self.latest[self.marker_key]
        marker = json.loads(self.objects[(self.marker_key, version)])
        marker["checkedAt"] = self.now.isoformat()
        self.objects[(self.marker_key, version)] = encoded(marker)
        with self.assertRaisesRegex(ValueError, "original inventory"): self.repair()

    def test_cli_repair_rejects_an_account_or_role_outside_the_cleanup_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            args, clients = self.cli(directory)
            clients["sts"].get_caller_identity = lambda: dict(Account="058114477594", Arn="arn:aws:sts::058114477594:assumed-role/techx-automation-publisher/run")
            with patch.object(sys, "argv", args), patch("run_cleanup.aws_clients", return_value=clients), self.assertRaisesRegex(ValueError, "dedicated cleanup"):
                run_cleanup.main()
        self.assertFalse(self.puts)

    def test_cli_cannot_start_workload_cleanup_for_a_changed_execution_version(self):
        self.current.update(status="FAILED_NEEDS_CLEANUP", executionVersion="newer-execution")
        with tempfile.TemporaryDirectory() as directory:
            args, clients = self.cli(directory)
            with patch.object(sys, "argv", args), patch.dict(os.environ, CODEBUILD_BUILD_ARN="arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-cleanup:aaaa-aaaa"), \
                    patch("run_cleanup.aws_clients", return_value=clients), patch("run_cleanup.S3SessionStore", return_value=self.store), \
                    patch("run_cleanup.prepare_takeover", side_effect=AssertionError("No stale takeover")), self.assertRaisesRegex(ValueError, "different staging execution"):
                run_cleanup.main()
        self.assertFalse(self.puts)

    def test_put_response_without_positive_marker_readback_cannot_report_completion(self):
        with patch.object(self.s3, "list_objects_v2", return_value=dict(IsTruncated=False, Contents=[])), \
                self.assertRaisesRegex(ValueError, "positive readback"): self.repair()

    def test_unknown_s3_and_invalid_existing_marker_are_not_absence(self):
        with patch.object(self.s3, "get_object", side_effect=PermissionError("denied")), self.assertRaises(PermissionError): self.repair()
        self.latest[self.marker_key] = "bad-marker"
        self.objects[(self.marker_key, "bad-marker")] = encoded(dict(executionVersion="wrong"))
        with self.assertRaises(ValueError): self.repair()
        self.assertFalse(self.puts)

    def test_concurrent_same_marker_producer_is_reconciled_without_overwrite(self):
        def race(args):
            self.objects[(self.marker_key, "concurrent-v1")] = args["Body"]
            self.latest[self.marker_key] = "concurrent-v1"
        self.race = race
        self.assertEqual(self.repair()["markerVersion"], "concurrent-v1")
        self.assertEqual(len(self.puts), 1)

    def test_new_session_race_before_or_during_marker_write_never_changes_it(self):
        original = deepcopy(self.current)
        reads = 0
        def read():
            nonlocal reads
            reads += 1
            if reads == 2: self.current.update(sessionId="staging-new-session", status="PLANNING"); self.etag += 1
            return deepcopy(self.current), str(self.etag)
        with patch.object(self.store, "read", side_effect=read), self.assertRaises(ValueError): self.repair()
        self.assertFalse(self.puts)
        self.current = original
        def race(args): self.current.update(sessionId="staging-new-session", status="PLANNING"); self.etag += 1
        self.race = race
        with self.assertRaises(ValueError): self.repair()
        self.assertEqual(self.current["sessionId"], "staging-new-session")
        self.assertFalse(self.state_writes)

    def cli(self, directory):
        execution_file = Path(directory) / "execution.json"
        execution_file.write_bytes(encoded(self.execution))
        clients = dict(s3=self.s3, sts=SimpleNamespace(get_caller_identity=lambda: dict(Account="058114477594",
            Arn="arn:aws:sts::058114477594:assumed-role/techx-automation-cleanup-executor/CodeBuild-fixture")))
        return ["run_cleanup.py", "--execution", str(execution_file), "--execution-version", self.version, "--bucket", self.bucket], clients

    def test_real_cli_destroyed_repair_cannot_call_teardown_takeover_or_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            args, clients = self.cli(directory)
            with patch.object(sys, "argv", args), patch.dict(os.environ, CODEBUILD_BUILD_ARN="arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-cleanup:aaaa-aaaa"), \
                    patch("run_cleanup.aws_clients", return_value=clients), patch("run_cleanup.S3SessionStore", return_value=self.store), \
                    patch("run_cleanup.prepare_takeover", side_effect=AssertionError("No takeover during repair")), \
                    patch("run_cleanup.execute", side_effect=AssertionError("No worker during repair")), redirect_stdout(io.StringIO()) as output:
                run_cleanup.main()
        self.assertIn("no workload operations", output.getvalue())
        self.assertEqual(len(self.puts), 1)

    def test_normal_cli_and_repair_share_versioned_completion_protocol(self):
        self.current.pop("cleanupEvidence"); self.current["status"] = "FAILED_NEEDS_CLEANUP"
        with tempfile.TemporaryDirectory() as directory:
            args, clients = self.cli(directory)
            def child(*a, **kw): Path(directory, "inventory.json").write_bytes(self.payload)
            with patch.object(sys, "argv", args), patch.dict(os.environ, CODEBUILD_BUILD_ARN="arn:aws:codebuild:us-east-1:058114477594:build/techx-automation-cleanup:aaaa-aaaa"), \
                    patch("run_cleanup.aws_clients", return_value=clients), patch("run_cleanup.S3SessionStore", return_value=self.store), \
                    patch("run_cleanup.prepare_takeover", side_effect=lambda clients, record, *a: record), \
                    patch("run_cleanup.execute", side_effect=child), redirect_stdout(io.StringIO()):
                run_cleanup.main()
        self.assertEqual(completion(self.s3, self.bucket, self.execution, self.version, self.current)["status"], "DESTROYED")
        self.assertEqual(len(self.state_writes), 2)
        self.assertEqual([item["Key"] for item in self.puts], [self.key, self.marker_key])


if __name__ == "__main__": unittest.main()
