"""Sequential real controllers/workers over shared offline service boundaries.

These are integration fixtures, not AWS/GitHub deployment acceptance. Acceptance
receipts and history are produced by the real workers and terminal parents;
the test never copies/re-signs another session's acceptance artifacts.
"""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid

import botocore.session
from botocore.validate import validate_parameters

from accepted_release_history import read_accepted_history
from lifecycle_dispatch import operation
from ready_release_contract import accepted_baseline
from plan_review_store import encoded


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


active = load("mixed_active_fixture", "test-active-private-acceptance.py")
public = active.support
rollout = public.rollout_support
handoff = rollout.support
promotion = handoff.support
publisher = promotion.support
rollback = load("mixed_rollback_fixture", "test-rollback-private-acceptance.py")
rollback_public = rollback.support
rollback_rollout = rollback_public.support
rollback_grant = rollback_rollout.support
rollback_review = rollback_grant.support
rollback_checks = rollback_review.support
rollback_controller = rollback_checks.support
rollback_prepare = rollback_controller.support
rollback_reservation = rollback_prepare.reservation
rollback_admission = rollback_reservation.support


class MixedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.cold = self.chart = self.f = None
        self.rounds = []

    def shared_ssm(self):
        """One SDK-shaped service retains each actual command's own handle.

        A remote command becomes terminal after its real worker/parent has
        committed the task result; older terminal observations are retained.
        No receipt, task result, lease or session state is written here.
        """
        f, sdk = self.f, self.f.clients["ssm"]
        original_send = sdk.send_command
        self.command_ids = []
        self.command_operations, self.command_managers = [], []
        self.invocations = {f.record["deploymentTask"].get("commandId", f.command_id): dict(status="Success", taskId=f.record["deploymentTask"]["taskId"])}
        def send(**kwargs):
            f.command_id = str(uuid.UUID(int=len(self.command_ids) + 1))
            result = original_send(**kwargs)
            self.command_ids.append(f.command_id)
            self.command_operations.append((f.record["deploymentTask"]["mode"], f.record["deploymentTask"]["stage"]))
            self.command_managers.append(f.record["deploymentTask"]["managerArn"])
            self.invocations[f.command_id] = dict(status="InProgress", taskId=kwargs["Parameters"]["TaskId"][0])
            return result
        def invocation(**kwargs):
            model = f.ssm_model.operation_model("GetCommandInvocation")
            validate_parameters(kwargs, model.input_shape)
            self.assertEqual(kwargs["InstanceId"], f.instance_id)
            command = self.invocations.get(kwargs["CommandId"])
            if command is None: raise f.Missing()
            task = f.record.get("deploymentTask", {})
            if task.get("taskId") == command["taskId"]:
                if task.get("state") == "SUCCEEDED": command["status"] = "Success"
                elif task.get("state") == "FAILED": command["status"] = "Failed"
            status = command["status"]
            result = dict(CommandId=kwargs["CommandId"], InstanceId=f.instance_id, DocumentName=f.document["Name"],
                DocumentVersion="1", PluginName="runTask", Status=status, ResponseCode=0 if status == "Success" else -1)
            validate_parameters(result, model.output_shape)
            return result
        sdk.send_command, sdk.get_command_invocation = send, invocation

    def release(self, number):
        """Prepare one new release without resetting session or old evidence."""
        p = publisher.ReadyReleaseTests()
        p.setUp(private=self.cold, source_sha=format(11 + number, "x") * 40,
            run_id=700 + number, opening_action="deploy-window-rollback", chart=self.chart)
        self.addCleanup(p.doCleanups)
        if self.f is None:
            self.cold, self.f = p.private, p.f
            self.context = {key: deepcopy(self.f.record[key]) for key in (
                "sessionId", "executionVersion", "sourceRevisions", "createdAt", "deadline", "cleanupStartAt", "approvedBudgetUsd", "action")}
            self.cold_private = deepcopy(self.f.record["stages"]["private"])
            self.cold_baseline = deepcopy(self.f.record["acceptedRelease"])
            self.shared_ssm()
        else:
            self.assertIs(p.f, self.f)
            self.f.now += timedelta(seconds=5)
        # Each release has a distinct image, tag, source and hosted CI identity.
        digest = "sha256:" + str(number) * 64
        for image in p.manifest["images"]: image["digest"] = digest
        if not hasattr(self, "registry"): self.registry = {}
        for image in p.manifest["images"]:
            repository = "techx/" + image["component"]
            for reference in (p.manifest["tag"], digest):
                key = repository, reference
                self.assertEqual(self.registry.get(key, digest), digest)
                self.registry[key] = digest
        def images(**kwargs):
            selector = kwargs["imageIds"][0]
            reference = selector.get("imageTag", selector.get("imageDigest"))
            actual = self.registry.get((kwargs["repositoryName"], reference))
            return dict(imageDetails=[dict(imageDigest=actual)] if actual else [])
        model = botocore.session.get_session().get_service_model("ecr")
        def api(operation, callback):
            shape = model.operation_model(operation)
            def request(**kwargs):
                validate_parameters(kwargs, shape.input_shape)
                result = callback(**kwargs)
                validate_parameters(result, shape.output_shape)
                return result
            return request
        self.f.clients["ecr"] = SimpleNamespace(
            describe_repositories=api("DescribeRepositories", lambda **kw: dict(repositories=[dict(imageTagMutability="IMMUTABLE")])),
            describe_images=api("DescribeImages", images),
            describe_image_scan_findings=api("DescribeImageScanFindings", lambda **kw: dict(imageScanStatus=dict(status="COMPLETE"),
                imageScanFindings=dict(findingSeverityCounts={}))))
        review = promotion.ActivePromotionTests()
        review.setUp(publisher=p, ci_run=950 + number)
        h = handoff.ActiveHandoffTests()
        h.setUp(promotion=review, review_run=100 + number,
            build_uuid=f"abcdef{number:02x}-1000")
        r = rollout.RolloutTests()
        r.setUp(handoff=h, selected_sha=format(number, "x") * 40, ci_run=960 + number)
        pub = public.ActivePublicTests()
        pub.setUp(rollout=r, run_id=200 + number * 10, build_uuid=f"abcdef{number:02x}-2000")
        if not hasattr(self, "service"):
            self.service = self.cold.public.service
            pub.service = self.service
            self.service_mutate = self.service.mutate
            self.cold_orders = deepcopy(self.service.orders)
        else: pub.service = self.service
        key = f"executions/{self.f.record['sessionId']}.json"
        if not hasattr(self, "execution_bytes"):
            self.assertNotIn(key, self.f.objects)
            self.f.objects[key] = encoded(pub.d.execution), pub.d.version
            self.execution_key, self.execution_bytes = key, self.f.objects[key]
        else:
            self.assertEqual(encoded(pub.d.execution), self.execution_bytes[0])
            self.assertEqual(self.f.objects[key], self.execution_bytes)
        self.chart = review.chart
        return pub

    def accept(self, pub):
        p = active.ActivePrivateTests()
        p.setUp(public=pub)
        self.addCleanup(p.doCleanups)
        p.complete_parent()
        pub.private_build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.assertEqual(p.d.advance()["acceptedRelease"], self.f.record["acceptedRelease"])
        self.assertEqual(pub.r.h.observe(2)["waiting"], "READY")
        accepted = read_accepted_history(self.f.clients["s3"], p.d.bucket, self.f.record, p.d.version)
        self.rounds.append(p)
        for key, value in self.context.items(): self.assertEqual(self.f.record[key], value)
        self.assertEqual(self.f.record["stages"]["private"], self.cold_private)
        self.assertEqual(accepted, self.f.record["acceptedRelease"])
        return accepted

    def restore(self, failed_public):
        admission = rollback_admission.AdmissionTests()
        admission.setUp(public=failed_public)
        self.addCleanup(admission.doCleanups)
        self.assertEqual(self.f.record["acceptedRelease"], self.last_accepted)
        self.assertEqual(self.f.objects[self.execution_key], self.execution_bytes)
        self.service.mutate = self.service_mutate
        reserved = rollback_reservation.ReservationTests()
        reserved.setUp(admission=admission)
        preparation = rollback_prepare.PrivatePrepareTests()
        preparation.setUp(reserved=reserved, chart=self.chart, defer_dispatch=True)
        controller = rollback_controller.ControllerTests()
        controller.setUp(preparation=preparation)
        checks = rollback_checks.ReviewTests()
        checks.setUp(controller=controller)
        review = rollback_review.ProtectedReviewTests()
        review.setUp(review=checks)
        grant = rollback_grant.GrantTests()
        grant.setUp(review=review)
        restored = rollback_rollout.RollbackRolloutTests()
        restored.setUp(grant=grant)
        pub = rollback_public.RollbackPublicTests()
        pub.setUp(restore=restored)
        pub.service = self.service
        p = rollback.RollbackPrivateTests()
        p.setUp(public=pub, build_uuid="abcdef94-1234")
        self.addCleanup(p.doCleanups)
        p.complete_parent()
        self.assertEqual(p.manager.wait(p.pointer)["state"], "SUCCEEDED")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        p.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        result = pub.controller.observe_private()
        self.assertEqual(result["acceptedRelease"], self.f.record["acceptedRelease"])
        self.assertEqual(p.d.advance()["acceptedRelease"], self.f.record["acceptedRelease"])
        self.rollback = p
        for key, value in self.context.items(): self.assertEqual(self.f.record[key], value)
        self.assertEqual(self.f.objects[self.execution_key], self.execution_bytes)
        return read_accepted_history(self.f.clients["s3"], p.d.bucket, self.f.record, p.d.version)

    def test_two_active_releases_keep_the_actual_first_history_and_use_new_baseline(self):
        first = self.accept(self.release(1))
        first_ref = deepcopy(first["acceptanceReference"])
        first_bytes = self.f.objects[first_ref["key"]]
        second = self.accept(self.release(2))
        self.assertNotEqual(first, second)
        self.assertEqual(self.f.record["acceptedActive"]["previousAccepted"], first)
        self.assertEqual(self.f.objects[first_ref["key"]], first_bytes)
        self.assertEqual(accepted_baseline(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1"), second)
        self.assertEqual(len(self.chart.previous_prs), 1)
        self.assertEqual(self.f.record["lifecycleDispatch"]["operation"], operation("verify", "active"))

    def test_cold_active_failed_active_rollback_and_next_active_use_one_history(self):
        first = self.last_accepted = self.accept(self.release(1))
        first_ref = deepcopy(first["acceptanceReference"])
        first_bytes = self.f.objects[first_ref["key"]]
        original_orders = deepcopy(self.service.orders)
        restored = self.restore(self.release(2))
        self.assertEqual(restored["platformSha"], first["platformSha"])
        self.assertEqual(restored["images"], first["images"])
        self.assertNotEqual(restored["chartSha"], first["chartSha"])
        self.assertEqual(self.f.record["acceptedRollback"]["previousAccepted"], first)
        attempt = deepcopy(self.f.record["rollbackAttempt"])
        latest = self.accept(self.release(3))
        self.assertEqual(self.f.record["acceptedActive"]["previousAccepted"], restored)
        self.assertEqual(self.f.record["rollbackAttempt"], attempt)
        self.assertEqual(self.f.objects[first_ref["key"]], first_bytes)
        self.assertNotEqual(latest["platformSha"], restored["platformSha"])
        self.assertTrue(set(original_orders).issubset(self.service.orders))
        for order_id, order in {**self.cold_orders, **original_orders}.items():
            self.assertEqual(self.service.orders[order_id], order)
        self.assertNotIn("releaseCandidate", self.f.record)
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(len(self.command_ids), 8)
        self.assertEqual(len(set(self.command_ids)), 8)
        self.assertEqual(len(set(self.command_managers)), 8)
        self.assertEqual(self.command_operations, [("release", "active"), ("verify", "active"), ("release", "active"),
            ("rollback", "prepare"), ("rollback", "rollout"), ("verify", "rollback"), ("release", "active"), ("verify", "active")])
        self.assertEqual(len(self.chart.previous_prs), 3)

    def mixed(self):
        self.last_accepted = self.accept(self.release(1))
        self.restore(self.release(2))
        return self.accept(self.release(3))

    def test_old_active_and_rollback_history_are_still_required_after_next_acceptance(self):
        latest = self.mixed()
        original = deepcopy(self.f.record)
        # The service stores all three actual finalizer outputs; corrupt each
        # older node independently, keeping its VersionId unchanged.
        keys = [key for key in self.f.objects if key.startswith(f"accepted/staging/{self.f.record['sessionId']}/")]
        self.assertEqual(len(keys), 3)
        for key in keys:
            stored = self.f.objects[key]
            changed = json.loads(stored[0])
            changed["release"]["platformSha"] = "0" * 40
            self.f.objects[key] = encoded(changed), stored[1]
            try:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    read_accepted_history(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
                self.assertEqual(self.f.record, original)
            finally: self.f.objects[key] = stored
        self.assertEqual(read_accepted_history(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1"), latest)

    def test_a_later_real_candidate_failure_cannot_rearm_the_retired_rollback_slot(self):
        latest = self.mixed()
        old_attempt = deepcopy(self.f.record["rollbackAttempt"])
        failed_public = self.release(4)
        admission = rollback_admission.AdmissionTests()
        admission.setUp(public=failed_public)
        self.addCleanup(admission.doCleanups)
        reserved = rollback_reservation.ReservationTests()
        reserved.setUp(admission=admission, run_id=301)
        snapshot = deepcopy(self.f.record)
        writes = len(self.chart.calls)
        with self.assertRaisesRegex(ValueError, "already consumed"): reserved.arm()
        self.assertEqual(self.f.record, snapshot)
        self.assertEqual(self.f.record["rollbackAttempt"], old_attempt)
        self.assertEqual(self.f.record["acceptedRelease"], latest)
        self.assertEqual(len(self.chart.calls), writes)
        self.assertEqual(self.f.objects[self.execution_key], self.execution_bytes)


if __name__ == "__main__": unittest.main()
