from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cleanup_launcher", Path(__file__).parents[1] / "bootstrap/automation/cleanup_launcher.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.event = dict(sessionId="staging-123456", executionVersion="immutable-version-1")
        self.record = dict(account="058114477594", region="us-east-1", environment="staging", sessionId="staging-123456",
                           cleanupStartAt=(self.now - timedelta(minutes=1)).isoformat(), deadline=(self.now + timedelta(hours=1)).isoformat(),
                           cleanupBundle=dict(key="bundles/" + "a" * 64 + ".tar.gz", sha256="a" * 64, versionId="bundle-v1"))
        self.started, self.builds, self.completion = [], [], None
        class NoSuchKey(Exception): pass
        def get_object(**kwargs):
            self.assertEqual(kwargs["ExpectedBucketOwner"], "058114477594")
            if kwargs["Key"].startswith("cleanup/"):
                if self.completion is None: raise NoSuchKey()
                return dict(Body=io.BytesIO(json.dumps(self.completion).encode()))
            if kwargs["Key"] == 'control/staging/current-execution.json':
                return dict(Body=io.BytesIO(b'{"status":"OFF"}'))
            self.assertEqual(kwargs["VersionId"], self.event["executionVersion"])
            return dict(Body=io.BytesIO(json.dumps(self.record).encode()))
        self.s3 = SimpleNamespace(get_object=get_object, exceptions=SimpleNamespace(NoSuchKey=NoSuchKey),
                                  list_objects_v2=lambda **kwargs: dict(Contents=[dict(Key=kwargs["Prefix"])] if self.completion else []))
        self.codebuild = SimpleNamespace(
            list_builds_for_project=lambda **kwargs: dict(ids=[b["id"] for b in self.builds]),
            batch_get_builds=lambda **kwargs: dict(builds=self.builds),
            start_build=lambda **kwargs: self.started.append(kwargs) or dict(build=dict(id="cleanup:123")),
        )
        self.environment = patch.dict(os.environ, CONTROL_BUCKET="techx-private-control", CLEANUP_PROJECT="techx-cleanup")
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_starts_only_fixed_project_and_immutable_execution(self):
        self.assertEqual(module.launch(self.event, self.s3, self.codebuild, self.now)["status"], "started")
        request = self.started[0]
        self.assertEqual(request["projectName"], "techx-cleanup")
        self.assertEqual(set(request), {"projectName", "idempotencyToken", "environmentVariablesOverride"})

    def test_not_due_or_wrong_scope_never_starts(self):
        self.record["cleanupStartAt"] = (self.now + timedelta(minutes=10)).isoformat()
        self.assertEqual(module.launch(self.event, self.s3, self.codebuild, self.now)["status"], "not-due")
        self.record["account"] = "other"
        with self.assertRaises(ValueError): module.launch(self.event, self.s3, self.codebuild, self.now)
        self.assertFalse(self.started)

    def test_reuses_running_cleanup_without_restart(self):
        self.builds = [dict(id="cleanup:123", buildStatus="IN_PROGRESS", environment=dict(environmentVariables=[
            dict(name="TECHX_SESSION_ID", value=self.event["sessionId"]),
            dict(name="TECHX_EXECUTION_VERSION", value=self.event["executionVersion"]) ]))]
        self.assertEqual(module.launch(self.event, self.s3, self.codebuild, self.now)["status"], "running")
        self.assertFalse(self.started)

    def test_api_failure_propagates(self):
        self.codebuild.list_builds_for_project = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("AWS unavailable"))
        with self.assertRaises(RuntimeError): module.launch(self.event, self.s3, self.codebuild, self.now)
        self.assertFalse(self.started)

    def test_off_or_verified_completion_does_not_start_compute(self):
        self.assertEqual(module.launch(dict(watchdog=True), self.s3, self.codebuild, self.now)["status"], "off")
        self.completion = dict(executionVersion=self.event["executionVersion"], inventoryEmpty=True, artifactSha256="a" * 64)
        self.assertEqual(module.launch(self.event, self.s3, self.codebuild, self.now)["status"], "already-destroyed")
        self.assertFalse(self.started)


if __name__ == "__main__": unittest.main()
