from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from automation_session import claim, release
from chart_selection import INFRA_REPO
from deployment_tasks import finish_task
from plan_review_store import checksum, immutable_json
from public_acceptance import PublicAcceptance, hosted_runner
from public_acceptance_contract import public_context, read_public_receipt
from public_probe import PublicProbe
from workflow_handoff import intent_for, intent_key, receipt_key, target_for


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


edge_support = load("public_edge_fixture", "test-edge-activation.py")
http_support = load("public_http_fixture", "test-public-probe.py")


class PublicAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.edge = edge_support.ActivationTests()
        self.edge.setUp()
        self.addCleanup(self.edge.doCleanups)
        self.edge.perform()
        f = self.f = self.edge.f
        f.record = release(f.record, self.edge.owner, self.edge.epoch)
        finish_task(f.store, self.edge.task, True, f.now)
        self.target = target_for("accept-public", f.record, f.execution)
        intent = intent_for(f.record, "execution-v1", self.target)
        self.dispatch = intent["dispatchId"]
        key = intent_key(f.record["sessionId"], self.dispatch)
        ref = immutable_json(f.clients["s3"], "control-bucket", key, intent)
        ref["sha256"] = checksum(intent)
        receipt = dict(schemaVersion=1, dispatchId=self.dispatch, intentReference=ref, response=dict(workflow_run_id=501,
            run_url=f"https://api.github.com/repos/{INFRA_REPO}/actions/runs/501", html_url=f"https://github.com/{INFRA_REPO}/actions/runs/501"))
        receipt_ref = immutable_json(f.clients["s3"], "control-bucket", receipt_key(f.record["sessionId"], self.dispatch), receipt)
        receipt_ref["sha256"] = checksum(receipt)
        f.record["workflowHandoff"] = dict(id=self.dispatch, phase="OBSERVED", intentReference=ref, receiptReference=receipt_ref)
        get = f.clients["s3"].get_object
        def read(**kw):
            if kw["Key"] == "automation/staging/session.json": return dict(Body=io.BytesIO(json.dumps(f.record).encode()))
            return get(**kw)
        f.clients["s3"].get_object = read
        f.clients["s3"].list_objects_v2 = lambda **kw: dict(IsTruncated=False,
            Contents=[dict(Key=kw["Prefix"])] if kw["Prefix"] in f.objects else [])
        self.worker = PublicAcceptance(f.clients, f.store, "control-bucket", f.execution, "execution-v1", 501,
            self.dispatch, self.target["inputs"]["activation_sha256"], clock=lambda: f.now)
        f.identity.update(Arn=self.worker.owner)
        f.record = claim(f.record, self.worker.owner, "deploy", now=f.now)
        self.run = dict(id=501, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha=f.execution["sourceRevisions"]["techx-infra"], path=".github/workflows/staging-acceptance.yml",
            repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        self.job = dict(id=601, run_id=501, head_sha=self.run["head_sha"], name="public", status="in_progress",
            runner_group_name="GitHub Actions", runner_id=100123, labels=["ubuntu-24.04"])
        def api(endpoint):
            if endpoint == "actions/runs/501": return deepcopy(self.run)
            if endpoint == "actions/runs/501/jobs?filter=latest&per_page=100": return dict(total_count=1, jobs=[deepcopy(self.job)])
            raise AssertionError(endpoint)
        self.github = SimpleNamespace(get=api)
        self.service = http_support.Storefront()
        self.release_reader = patch("public_acceptance.read_release", return_value={})
        self.selection_reader = patch("public_acceptance.read_selection", return_value={})
        self.release_mock, self.selection_mock = self.release_reader.start(), self.selection_reader.start()
        self.addCleanup(self.release_reader.stop)
        self.addCleanup(self.selection_reader.stop)

    def probe(self, guard, session, dispatch):
        return PublicProbe(guard, session, dispatch, transport=self.service,
            dns_verify=lambda target, guard: self.assertEqual(target, self.edge.target))

    def perform(self): return self.worker.perform(self.github, probe_factory=self.probe)

    def test_real_probe_result_is_private_versioned_and_does_not_mark_ready(self):
        result = self.perform()
        self.assertEqual(result["state"], "PUBLIC_PASSED")
        self.assertEqual(self.f.record["status"], "PROVISIONING")
        receipt = read_public_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        self.assertEqual(receipt["runner"]["jobId"], 601)
        self.assertEqual(receipt["result"]["private"], "pending")
        self.assertEqual(receipt["result"]["vpn"], "pending")
        self.assertEqual(self.f.record["stages"]["publicAcceptance"]["contextSha256"], checksum(public_context(self.f.record)))
        self.release_mock.assert_called_once()
        self.selection_mock.assert_called_once()

    def test_duplicate_worker_cannot_probe_again_after_checkpoint_consumption(self):
        self.perform()
        count = len(self.service.calls)
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.service.calls), count)

    def test_fork_rerun_wrong_sha_and_self_hosted_job_are_rejected(self):
        mutations = [(self.run, "head_sha", "0" * 40), (self.run, "run_attempt", 2),
            (self.run, "head_branch", "feature"), (self.job, "runner_group_name", "Private runners"),
            (self.job, "labels", ["self-hosted", "ubuntu-24.04"])]
        for obj, key, changed in mutations:
            old = deepcopy(obj[key])
            obj[key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError): hosted_runner(self.github, 501, self.f.execution)
            obj[key] = old
        self.assertFalse(self.service.calls)

    def test_wrong_workflow_receipt_prevents_network_requests(self):
        self.worker.run_id = "502"
        with patch("public_acceptance.guard_child", side_effect=ValueError("run does not match protected receipt")):
            with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.service.calls)

    def test_replaced_activation_or_stale_window_blocks_probe(self):
        self.f.record["stages"]["exposure"]["receiptReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.service.calls)

    def test_failed_public_check_marks_cleanup_and_preserves_bounded_diagnostics(self):
        self.service.mutate = lambda response, method, path: response.update(status=503) if path == "/" else None
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["stages"]["publicAcceptance"]["state"], "FAILED")
        failures = [json.loads(data) for key, (data, _) in self.f.objects.items() if key.endswith("/failure.json")]
        self.assertEqual(failures[0]["classification"], "http-status")
        self.assertNotIn("body", failures[0])
        self.assertFalse(any(key.endswith("/result.json") for key in self.f.objects))

    def test_cleanup_takeover_between_requests_stops_probe_without_overwriting_cleanup(self):
        self.service.mutate = lambda response, method, path: self.f.record.update(status="DESTROYING") if path == "/api/products" else None
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertFalse(self.service.orders)
        self.assertFalse(any(key.endswith("/result.json") for key in self.f.objects))

    def test_lease_expiry_after_dns_never_sends_order_requests(self):
        self.service.mutate = lambda response, method, path: self.f.record["lease"].update(
            expiresAt=(self.f.now - timedelta(seconds=1)).isoformat()) if path == "/" else None
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.service.orders)

    def test_changed_receipt_or_missing_suite_cannot_be_consumed(self):
        self.perform()
        ref = self.f.record["stages"]["publicAcceptance"]["receiptReference"]
        body, version = self.f.objects[ref["key"]]
        changed = json.loads(body)
        changed["result"]["checks"] = ["dns"]
        self.f.objects[ref["key"]] = json.dumps(changed).encode(), version
        with self.assertRaises(ValueError): read_public_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        ref["sha256"] = checksum(changed)
        with self.assertRaises(ValueError): read_public_receipt(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")

    def test_receipt_write_failure_never_publishes_passed_checkpoint(self):
        original = self.f.clients["s3"].put_object
        def put(**kw):
            if kw["Key"].endswith("/result.json"): raise TimeoutError("evidence storage unavailable")
            return original(**kw)
        self.f.clients["s3"].put_object = put
        with self.assertRaises(TimeoutError): self.perform()
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["stages"]["publicAcceptance"]["state"], "FAILED")

    def test_orchestrator_dispatches_and_consumes_only_exact_public_workflow_result(self):
        from unittest.mock import Mock
        from lifecycle_orchestration import Orchestrator
        # No public worker is active while the parent chooses the handoff.
        self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        dispatcher = SimpleNamespace(current=lambda **kw: self.f.store.read(), clients=self.f.clients,
            store=self.f.store, bucket="control-bucket", execution=self.f.execution, version="execution-v1", clock=lambda: self.f.now)
        engine = Orchestrator(dispatcher, Mock(), {}, Mock())
        self.assertEqual(engine.target(dict(waiting="PUBLIC_ACCEPTANCE")), self.target)
        self.f.record = claim(self.f.record, self.worker.owner, "deploy", now=self.f.now)
        self.perform()
        self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        intent_data = json.loads(self.f.objects[intent_key(self.f.record["sessionId"], self.dispatch)][0])
        engine.handoffs = Mock()
        engine.consume(deepcopy(self.f.record), intent_data, dict(id=501))
        engine.handoffs.phase.assert_called_once_with(self.dispatch, "CONSUMED")
        with self.assertRaises(ValueError): engine.consume(deepcopy(self.f.record), intent_data, dict(id=502))
        from lifecycle_dispatch import next_operation, operation
        self.assertEqual(next_operation(self.f.record, operation("activate", "exposure")), dict(waiting="PRIVATE_ACCEPTANCE"))
        self.assertEqual(self.f.record["status"], "PROVISIONING")

    def test_dispatch_intent_cannot_be_rebound_to_changed_activation(self):
        from workflow_handoff import load_intent
        ref = self.f.record["workflowHandoff"]["intentReference"]
        self.f.record["stages"]["exposure"]["receiptReference"]["sha256"] = "0" * 64
        # The saved target retains its original predecessor. Child admission
        # also checks the current activation, so it cannot probe a replacement.
        load_intent(self.f.clients["s3"], "control-bucket", self.f.record, ref)
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.service.calls)

    def test_failed_public_callback_still_rebuilds_original_protected_predecessor(self):
        from orchestrate_lifecycle import resolve
        self.f.record["status"] = "FAILED_NEEDS_CLEANUP"
        self.f.execution["sourceAdmission"] = dict(workflowRun=f"https://github.com/{INFRA_REPO}/actions/runs/200")
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = json.dumps(self.f.execution).encode(), "execution-v1"
        self.f.objects["control/staging/current-execution.json"] = json.dumps(dict(
            sessionId=self.f.record["sessionId"], executionVersion="execution-v1")).encode(), "pointer-v1"
        root = dict(self.run, id=200, status="completed", conclusion="success", path=".github/workflows/staging-lifecycle.yml")
        own = dict(self.run, id=900, event="workflow_run", path=".github/workflows/staging-orchestrate.yml")
        public = dict(self.run, status="completed", conclusion="failure")
        api = SimpleNamespace(get=lambda endpoint: deepcopy({"actions/runs/200": root, "actions/runs/900": own, "actions/runs/501": public}[endpoint]))
        result = resolve(self.f.clients, self.f.store, "control-bucket", api, run_id=900,
            source_sha=self.run["head_sha"], event_name="workflow_run", event=dict(workflow_run=dict(id=501)))
        self.assertTrue(result["boundFailure"])
        self.assertEqual(result["executionVersion"], "execution-v1")


if __name__ == "__main__": unittest.main()
