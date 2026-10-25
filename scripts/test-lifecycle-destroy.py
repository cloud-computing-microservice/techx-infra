from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from destroy_lifecycle import (ACCOUNT, CLEANUP_ROLE, DispatchUncertain, INFRA_REPO, LAUNCHER,
                               PROJECT, WORKFLOW, request_cleanup)
from lifecycle_coordinator import initial_record
from plan_review_store import encoded


class DestroyTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.session = "staging-gh-123"
        self.version = "-opaque-execution-version"
        self.current_sha = "d" * 40
        self.execution = initial_record(self.session, {"techx-infra": "a" * 40, "techx-platform": "b" * 40,
            "techx-chart": "c" * 40}, 4, "owner@example.invalid", "deploy-window",
            datetime(2026, 1, 1, tzinfo=timezone.utc), approved_budget_usd=60)
        self.current = dict(deepcopy(self.execution), executionVersion=self.version, status="FAILED_NEEDS_CLEANUP")
        self.run = dict(id=456, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha=self.current_sha, path=WORKFLOW, repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        self.main_sha = self.current_sha
        self.github = SimpleNamespace(get=self.get_github)
        self.build_id = PROJECT + ":aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        kms = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.build = dict(id=self.build_id, arn=f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/{self.build_id}",
            projectName=PROJECT, serviceRole=CLEANUP_ROLE, buildStatus="IN_PROGRESS", buildComplete=False, encryptionKey=kms,
            environment=dict(environmentVariables=[dict(name="TECHX_SESSION_ID", value=self.session, type="PLAINTEXT"),
                dict(name="TECHX_EXECUTION_VERSION", value=self.version, type="PLAINTEXT"),
                dict(name="TECHX_STATE_BUCKET", value="techx-test-control", type="PLAINTEXT"),
                dict(name="TECHX_STATE_KMS_KEY_ARN", value=kms, type="PLAINTEXT"),
                dict(name="CONTROL_BUCKET", value="techx-test-control", type="PLAINTEXT"),
                dict(name="CLOUDFLARE_SECRET_ARN", value="not-a-token", type="PLAINTEXT")]))
        self.objects = {}
        self.build_reads = 0
        self.finish = True
        self.complete_on_success = True
        self.response_status = "started"
        self.identity = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-destroy-dispatcher/techx-destroy-456")
        self.clients = dict(sts=SimpleNamespace(get_caller_identity=lambda: self.identity),
            s3=SimpleNamespace(get_object=self.get_object, list_objects_v2=self.list_objects),
            codebuild=SimpleNamespace(batch_get_builds=self.get_build), **{"lambda": SimpleNamespace(invoke=self.invoke)})

    def get_github(self, endpoint):
        if endpoint == "actions/runs/456": return deepcopy(self.run)
        if endpoint == "git/ref/heads/main": return dict(object=dict(sha=self.main_sha))
        self.fail("Unexpected GitHub endpoint")

    def get_object(self, **args):
        self.assertEqual(args["ExpectedBucketOwner"], ACCOUNT)
        self.assertEqual(args["Bucket"], "techx-test-control")
        if args["Key"] == f"executions/{self.session}.json":
            self.assertEqual(args["VersionId"], self.version)
            return dict(Body=io.BytesIO(encoded(self.execution)), VersionId=self.version)
        if args["Key"] == "automation/staging/session.json":
            return dict(Body=io.BytesIO(encoded(self.current)), VersionId="session-version")
        return dict(Body=io.BytesIO(self.objects[args["Key"]]), VersionId="evidence-version")

    def list_objects(self, **args):
        self.assertEqual(args["ExpectedBucketOwner"], ACCOUNT)
        return dict(IsTruncated=False, Contents=[dict(Key=key) for key in self.objects if key.startswith(args["Prefix"])])

    def complete(self):
        inventory = dict(accountId=ACCOUNT, region="us-east-1", environment="staging")
        for field in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()):
            inventory[field] = []
        # Producer formatting differs from encoded(). Check its original bytes.
        payload = json.dumps(inventory, indent=2).encode()
        digest = hashlib.sha256(payload).hexdigest()
        inventory_key = f"cleanup/{self.session}/inventory-{digest}.json"
        self.objects[inventory_key] = payload
        self.objects[f"cleanup/{self.session}/complete.json"] = encoded(dict(executionVersion=self.version,
            inventoryEmpty=True, artifactSha256=digest, inventoryKey=inventory_key, checkedAt="2026-01-01T05:00:00+00:00"))
        self.current.update(status="DESTROYED", cleanupEvidence=dict(inventoryEmpty=True, artifactSha256=digest, key=inventory_key))

    def invoke(self, **args):
        self.calls.append(args)
        self.assertEqual(args["FunctionName"], LAUNCHER)
        self.assertEqual(args["InvocationType"], "RequestResponse")
        self.assertEqual(json.loads(args["Payload"]), dict(sessionId=self.session, executionVersion=self.version, requestedEarly=True))
        return dict(StatusCode=200, Payload=io.BytesIO(encoded(dict(status=self.response_status, sessionId=self.session, buildId=self.build_id))))

    def get_build(self, **args):
        self.assertEqual(args, dict(ids=[self.build_id]))
        self.build_reads += 1
        if self.finish and self.build_reads >= 2:
            self.build.update(buildStatus="SUCCEEDED", buildComplete=True)
            if self.complete_on_success: self.complete()
        return dict(builds=[deepcopy(self.build)])

    def request(self, **kwargs):
        return request_cleanup(self.clients, self.github, bucket="techx-test-control", session_id=self.session,
            execution_version=self.version, run_id="456", source_sha=self.current_sha,
            observations=3, sleep=lambda _: None, **kwargs)

    def test_expired_failed_session_uses_new_main_and_verifies_real_inventory(self):
        result = self.request()
        self.assertEqual(result["status"], "DESTROYED")
        self.assertTrue(result["inventoryEmpty"])
        self.assertEqual(result["buildId"], self.build_id)
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.execution["sourceRevisions"]["techx-infra"], self.current_sha)
        self.assertNotIn("owner@example.invalid", json.dumps(result))

    def test_complete_session_is_read_only_without_lambda_or_build_calls(self):
        self.complete()
        self.assertEqual(self.request()["status"], "DESTROYED")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.build_reads, 0)

    def test_stale_session_or_execution_cannot_request_cleanup(self):
        for key, value in (("sessionId", "staging-other"), ("executionVersion", "other-version"),
                           ("status", "OFF"), ("deadline", "2026-01-02T00:00:00+00:00")):
            old = self.current[key]
            self.current[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.request()
            self.current[key] = old
        self.assertFalse(self.calls)

    def test_untrusted_workflow_or_identity_cannot_invoke(self):
        for key, value in (("head_sha", "f" * 40), ("run_attempt", 2), ("run_attempt", True),
                           ("path", ".github/workflows/verify.yml"), ("head_branch", "feature"),
                           ("event", "pull_request"), ("head_repository", dict(full_name="fork/repo"))):
            old = self.run[key]
            self.run[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.request()
            self.run[key] = old
        self.main_sha = "e" * 40
        with self.assertRaises(ValueError): self.request()
        self.main_sha = self.current_sha
        self.identity["Account"] = "000000000000"
        with self.assertRaises(ValueError): self.request()
        self.assertFalse(self.calls)

    def test_lost_or_function_error_response_is_not_reinvoked(self):
        def lost(**args):
            self.calls.append(args)
            raise TimeoutError("lost response after potential launch")
        self.clients["lambda"].invoke = lost
        with self.assertRaises(DispatchUncertain): self.request()
        self.assertEqual(len(self.calls), 1)
        self.clients["lambda"].invoke = lambda **args: dict(StatusCode=200, FunctionError="Unhandled")
        with self.assertRaises(DispatchUncertain): self.request()
        self.assertEqual(self.build_reads, 0)

    def test_bad_response_and_unproven_already_destroyed_are_not_success(self):
        for status in ("not-due", "off", "already-destroyed"):
            self.response_status = status
            with self.subTest(status=status), self.assertRaises(DispatchUncertain): self.request()
        self.assertEqual(self.build_reads, 0)

    def test_observation_limit_retains_same_handle_and_does_not_claim_inventory(self):
        self.finish = False
        result = self.request()
        self.assertEqual(result["waiting"], "CLEANUP_RUNNING")
        self.assertFalse(result["inventoryVerified"])
        self.assertEqual(result["buildId"], self.build_id)
        self.assertEqual(len(self.calls), 1)

    def test_build_must_be_same_project_role_and_execution(self):
        self.finish = False
        for key, value in (("serviceRole", "arn:aws:iam::058114477594:role/other"),
                           ("projectName", "other"), ("buildStatus", "SUCCEEDED"), ("buildComplete", True)):
            old = self.build[key]
            self.build[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.request()
            self.build[key] = old
        self.build["environment"]["environmentVariables"][1]["value"] = "other-execution"
        with self.assertRaises(ValueError): self.request()

    def test_terminal_build_without_marker_or_failure_never_reports_destroyed(self):
        self.complete_on_success = False
        with self.assertRaisesRegex(ValueError, "without protected inventory"): self.request()
        self.finish = False
        self.build.update(buildStatus="FAILED", buildComplete=True)
        with self.assertRaisesRegex(ValueError, "failed or returned"): self.request()

    def test_tampered_or_wrong_execution_inventory_marker_is_rejected(self):
        self.complete()
        key = f"cleanup/{self.session}/complete.json"
        original = self.objects[key]
        for name, value in (("executionVersion", "other"), ("inventoryEmpty", False), ("artifactSha256", "f" * 64)):
            marker = json.loads(original)
            marker[name] = value
            self.objects[key] = encoded(marker)
            with self.subTest(name=name), self.assertRaises(ValueError): self.request()
        self.objects[key] = original
        inventory_key = json.loads(original)["inventoryKey"]
        self.objects[inventory_key] += b" "
        with self.assertRaisesRegex(ValueError, "inventory bytes"): self.request()
        self.assertFalse(self.calls)

    def test_signed_nonempty_inventory_or_missing_session_checkpoint_is_rejected(self):
        self.complete()
        self.current["status"] = "DESTROYING"
        with self.assertRaisesRegex(ValueError, "DESTROYED"): self.request()
        self.current["status"] = "DESTROYED"
        key = f"cleanup/{self.session}/complete.json"
        marker = json.loads(self.objects[key])
        inventory = json.loads(self.objects[marker["inventoryKey"]])
        # Choose an actual required field from the versioned inventory contract.
        inventory[json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text())[0]] = ["remaining"]
        payload = encoded(inventory)
        digest = hashlib.sha256(payload).hexdigest()
        marker.update(artifactSha256=digest, inventoryKey=f"cleanup/{self.session}/inventory-{digest}.json")
        self.objects[marker["inventoryKey"]] = payload
        self.objects[key] = encoded(marker)
        with self.assertRaisesRegex(ValueError, "nonempty"): self.request()

    def test_incomplete_listing_or_absent_exact_version_fails_closed(self):
        self.clients["s3"].list_objects_v2 = lambda **args: dict(IsTruncated=True, Contents=[])
        with self.assertRaisesRegex(ValueError, "listing"): self.request()
        self.assertFalse(self.calls)
        self.clients["s3"].list_objects_v2 = self.list_objects
        original = self.get_object
        def wrong_version(**args):
            result = original(**args)
            if "VersionId" in args: result["VersionId"] = "other"
            return result
        self.clients["s3"].get_object = wrong_version
        with self.assertRaisesRegex(ValueError, "unexpected version"): self.request()
        self.assertFalse(self.calls)

    def test_missing_marker_after_destroyed_checkpoint_requires_reconciliation_without_launch(self):
        self.complete()
        del self.objects[f"cleanup/{self.session}/complete.json"]
        with self.assertRaisesRegex(ValueError, "reconcile evidence"): self.request()
        self.assertFalse(self.calls)


if __name__ == "__main__": unittest.main()
