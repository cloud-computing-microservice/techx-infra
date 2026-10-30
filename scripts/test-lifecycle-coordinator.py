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
        self.fail_schedule = False
        self.objects["control/staging/executor-config.json"] = json.dumps(dict(schemaVersion=1,
            imageId="ami-1234567890abcdef0", launchTemplateId="lt-1234567890abcdef0", launchTemplateVersion="1",
            subnetId="subnet-1234567890abcdef0", securityGroupId="sg-1234567890abcdef0", loaderSha256="a" * 64, imageBuildSha256="c" * 64,
            documentVersion="1", documentSha256="b" * 64)).encode()
        self.now = datetime.now(timezone.utc)
        self.record = initial_record("staging-123456", {name: "a" * 40 for name in ("techx-infra", "techx-chart", "techx-platform")},
                                     4, "owner@example.invalid", "deploy-test-destroy", self.now, approved_budget_usd=60)
        self.inventory = dict(accountId="058114477594", environment="staging", region="us-east-1")
        for key in json.loads(Path(__file__).parents[1].joinpath("contracts/inventory-fields.json").read_text()): self.inventory[key] = []
        self.estimate = dict(includesAutomation=True, maximumHours=4, upperBoundUsd=17.7, approvedBudgetUsd=60)
        class Missing(Exception): pass
        def get_object(**kwargs):
            if kwargs["Key"] == "automation/staging/session.json":
                return dict(Body=io.BytesIO(json.dumps(self.current).encode()), ETag=self.etag)
            if kwargs["Key"] in self.objects:
                return dict(Body=io.BytesIO(self.objects[kwargs["Key"]]), ETag="object-etag", VersionId="object-version")
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
            if self.fail_schedule: raise TimeoutError("CreateSchedule response lost")
            return dict(ScheduleArn=self.schedule["Arn"])
        def start_build(**kwargs):
            self.events.append("start-build")
            if self.fail_start: raise RuntimeError("Lost StartBuild response")
            build_id = "techx-automation-lifecycle:123abc"
            return dict(build=dict(id=build_id, arn=f"arn:aws:codebuild:us-east-1:058114477594:build/{build_id}", projectName="techx-automation-lifecycle"))
        clients = {
            "sts": SimpleNamespace(get_caller_identity=lambda: dict(Account="058114477594")),
            "s3": SimpleNamespace(get_object=get_object, put_object=put_object, delete_object=lambda **kwargs: self.objects.pop(kwargs["Key"]), exceptions=SimpleNamespace(NoSuchKey=Missing),
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
        self.assertEqual(result["buildId"], "techx-automation-lifecycle:123abc")
        self.assertEqual(self.current["lifecycleDispatch"]["phase"], "OBSERVED")
        self.assertLess(self.events.index("schedule"), self.events.index("start-build"))
        self.assertIn("private/staging/staging-123456/inputs.json", self.objects)
        self.assertIn("control/staging/current-execution.json", self.objects)
        self.assertEqual(self.current["executionVersion"], result["executionVersion"])
        private = json.loads(self.objects["private/staging/staging-123456/inputs.json"])
        execution = json.loads(self.objects["executions/staging-123456.json"])
        self.assertEqual(private["terraformVars"]["budget_limit_usd"], 60)
        self.assertEqual(execution["approvedBudgetUsd"], 60)
        self.assertEqual(execution["costEstimate"]["approvedBudgetUsd"], 60)
        self.assertEqual(self.current["approvedBudgetUsd"], 60)

    def test_uncertain_start_response_triggers_independent_cleanup_not_off(self):
        self.fail_start = True
        with self.assertRaises(RuntimeError): self.start()
        self.assertIn("early-cleanup", self.events)
        self.assertEqual(self.current["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.current["lifecycleDispatch"]["phase"], "CALLING")

    def test_unknown_schedule_result_retains_recoverable_execution_and_never_reopens_off(self):
        self.fail_schedule = True
        with self.assertRaises(TimeoutError): self.start()
        self.assertNotIn("start-build", self.events)
        self.assertIn("early-cleanup", self.events)
        self.assertEqual(self.current["status"], "FAILED_NEEDS_CLEANUP")
        pointer = json.loads(self.objects["control/staging/current-execution.json"])
        self.assertEqual(pointer["executionVersion"], self.current["executionVersion"])
        self.assertIn(f"private/staging/{self.record['sessionId']}/inputs.json", self.objects)

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

    def test_budget_mismatch_or_missing_budget_never_reserves(self):
        for value in (None, 80, 0, True, float("nan"), float("inf")):
            self.estimate["approvedBudgetUsd"] = value
            with self.subTest(value=value), self.assertRaises(ValueError): self.start()
        self.estimate["approvedBudgetUsd"] = 60
        self.record.pop("approvedBudgetUsd")
        with self.assertRaises(ValueError): self.start()
        self.assertFalse(self.events)

    def test_lower_budget_limits_estimate_instead_of_using_policy_ceiling(self):
        self.record["approvedBudgetUsd"] = self.estimate["approvedBudgetUsd"] = 10
        self.estimate["upperBoundUsd"] = 10.01
        with self.assertRaises(ValueError): self.start()
        self.assertFalse(self.events)
        self.estimate["upperBoundUsd"] = 10
        self.start()
        private = json.loads(self.objects["private/staging/staging-123456/inputs.json"])
        self.assertEqual(private["terraformVars"]["budget_limit_usd"], 10)

    def test_invalid_budget_rejected_when_creating_session(self):
        args = ("staging-123456", self.record["sourceRevisions"], 4, "owner@example.invalid", "deploy-window", self.now)
        for budget in (None, True, 0, 81, float("nan"), float("inf"), "60"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                initial_record(*args, approved_budget_usd=budget)
        with self.assertRaises(TypeError): initial_record(*args)

    def test_closed_session_identity_cannot_be_reused_for_a_new_environment(self):
        for state in ("OFF", "DESTROYED"):
            self.current = dict(status=state, sessionId=self.record["sessionId"])
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "fresh session"): self.start()
        self.assertFalse(self.events)

    def test_estimate_window_cannot_change_on_resume(self):
        for hours in (True, 3, 4.0, None):
            self.estimate["maximumHours"] = hours
            with self.subTest(hours=hours), self.assertRaises(ValueError): self.start()
        self.assertFalse(self.events)


if __name__ == "__main__": unittest.main()
