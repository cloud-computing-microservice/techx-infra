from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from lifecycle_coordinator import Coordinator, initial_record


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.events, self.objects, self.schedule = [], {}, None
        self.current, self.etag = dict(status="OFF"), "v1"
        self.fail_start = False
        self.now = datetime.now(timezone.utc)
        self.record = initial_record("staging-123456", {name: "a" * 40 for name in ("techx-infra", "techx-chart", "techx-platform")},
                                     4, "owner@example.invalid", "deploy-test-destroy", self.now)
        self.inventory = dict(accountId="058114477594", environment="staging", region="us-east-1")
        for key in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): self.inventory[key] = []
        self.estimate = dict(includesAutomation=True, maximumHours=4, upperBoundUsd=17.7, approvedBudgetUsd=80)
        class Missing(Exception): pass
        def get_object(**kwargs):
            if kwargs["Key"] == "automation/staging/session.json":
                return dict(Body=io.BytesIO(json.dumps(self.current).encode()), ETag=self.etag)
            raise Missing()
        def put_object(**kwargs):
            self.events.append(kwargs["Key"])
            self.objects[kwargs["Key"]] = kwargs["Body"]
            return dict(VersionId="version-" + str(len(self.objects)))
        def write(record, etag=None):
            if etag != self.etag: raise ValueError("CAS conflict")
            self.current = dict(record)
            self.etag += "x"
            self.events.append("session")
        def get_schedule(**kwargs):
            if self.schedule is None: raise Missing()
            return self.schedule
        def create_schedule(**kwargs):
            self.events.append("schedule")
            self.schedule = dict(kwargs, Arn="arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/" + kwargs["Name"])
            return dict(ScheduleArn=self.schedule["Arn"])
        def start_build(**kwargs):
            self.events.append("start-build")
            if self.fail_start: raise RuntimeError("Lost StartBuild response")
            return dict(build=dict(id="lifecycle:123"))
        clients = {
            "sts": SimpleNamespace(get_caller_identity=lambda: dict(Account="058114477594")),
            "s3": SimpleNamespace(get_object=get_object, put_object=put_object, exceptions=SimpleNamespace(NoSuchKey=Missing),
                                   get_bucket_versioning=lambda **kwargs: dict(Status="Enabled")),
            "codebuild": SimpleNamespace(batch_get_projects=lambda **kwargs: dict(projects=[dict(name=name) for name in kwargs["names"]]), start_build=start_build),
            "scheduler": SimpleNamespace(get_schedule=get_schedule, create_schedule=create_schedule,
                                          exceptions=SimpleNamespace(ResourceNotFoundException=Missing)),
            "lambda": SimpleNamespace(invoke=lambda **kwargs: self.events.append("early-cleanup")),
        }
        store = SimpleNamespace(read=lambda: (dict(self.current), self.etag), write=write)
        self.coordinator = Coordinator(clients, store, "control-bucket")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = Path(self.directory.name) / "source.tar.gz"
        self.bundle.write_bytes(b"fixture bundle; loader integrity checks are tested separately")

    def start(self): return self.coordinator.start(self.record, self.bundle, self.inventory, self.estimate, "8.8.8.8", self.now)

    def test_cleanup_is_armed_and_inputs_persisted_before_start_build(self):
        result = self.start()
        self.assertEqual(result["buildId"], "lifecycle:123")
        self.assertLess(self.events.index("schedule"), self.events.index("start-build"))
        self.assertIn("private/staging/staging-123456/inputs.json", self.objects)
        self.assertIn("control/staging/current-execution.json", self.objects)
        self.assertEqual(self.current["executionVersion"], result["executionVersion"])

    def test_uncertain_start_response_triggers_independent_cleanup_not_off(self):
        self.fail_start = True
        with self.assertRaises(RuntimeError): self.start()
        self.assertIn("early-cleanup", self.events)
        self.assertEqual(self.current["status"], "PLANNING")

    def test_existing_session_leftover_resources_or_missing_cost_coverage_block_start(self):
        self.current["status"] = "READY"
        with self.assertRaises(ValueError): self.start()
        self.current["status"] = "OFF"
        self.inventory["ebsVolumes"] = ["vol-leftover"]
        with self.assertRaises(ValueError): self.start()
        self.inventory["ebsVolumes"] = []
        self.estimate["includesAutomation"] = False
        with self.assertRaises(ValueError): self.start()
        self.assertNotIn("start-build", self.events)
        self.assertNotIn("schedule", self.events)

    def test_invalid_cost_numbers_never_reserve_or_start(self):
        for bound in (-1, float("nan"), float("inf"), True, 80):
            self.estimate["upperBoundUsd"] = bound
            with self.subTest(bound=bound), self.assertRaises(ValueError): self.start()
        self.assertFalse(self.events)


if __name__ == "__main__": unittest.main()
