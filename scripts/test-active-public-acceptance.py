"""Real hosted public suite and controller consumption for a private rollout."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from active_public_acceptance import ActivePublicAcceptance
from active_public_contract import active_public_context, read_active_public_receipt
from automation_session import claim, release
from chart_selection import INFRA_REPO
from deployment_tasks import finish_task
from plan_review_store import checksum, encoded
from public_acceptance_contract import read_public_receipt
from public_probe import PublicProbe


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


rollout_support = load("active_public_rollout_fixture", "test-active-rollout.py")
http_support = load("active_public_http_fixture", "test-public-probe.py")


class ActivePublicTests(unittest.TestCase):
    def setUp(self):
        self.r = rollout_support.RolloutTests()
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        r = self.r
        r.perform()
        self.f, self.d = r.f, r.d
        f = self.f
        f.record = release(f.record, r.owner, r.epoch)
        finish_task(f.store, r.task, True, f.now)
        r.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.baseline = deepcopy(f.record["acceptedRelease"])
        self.cold_public = deepcopy(f.record["stages"]["publicAcceptance"])
        self.run, self.job, self.dispatches = {}, {}, []
        original_get = r.h.infra.get
        def get(path):
            if path == "actions/runs/200": return deepcopy(self.run)
            if path == "actions/runs/200/jobs?filter=latest&per_page=100": return dict(total_count=1, jobs=[deepcopy(self.job)])
            return original_get(path)
        def dispatch(target, dispatch_id):
            self.assertEqual(target["operation"], "accept-active-public")
            self.dispatches.append(deepcopy(target))
            self.run.update(id=200, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main", head_sha=target["sourceSha"],
                path=".github/workflows/staging-acceptance.yml", display_title="TechX handoff " + dispatch_id,
                repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO), created_at=f.now.isoformat())
            self.job.update(id=201, run_id=200, head_sha=target["sourceSha"], name="public", status="in_progress", runner_group_name="GitHub Actions", runner_id=202, labels=["ubuntu-24.04"])
            return dict(workflow_run_id=200, run_url=f"https://api.github.com/repos/{INFRA_REPO}/actions/runs/200", html_url=f"https://github.com/{INFRA_REPO}/actions/runs/200")
        r.h.infra.get, r.h.infra.dispatch = get, dispatch
        r.h.observe()
        self.release_id = f.record["releaseCandidate"]["id"]
        self.rollout_sha = f.record["releaseCandidate"]["rollout"]["receiptReference"]["sha256"]
        self.dispatch_id = f.record["workflowHandoff"]["id"]
        self.worker = ActivePublicAcceptance(f.clients, f.store, self.d.bucket, self.d.execution, self.d.version, 200,
            self.dispatch_id, self.rollout_sha, release_id=self.release_id, clock=lambda: f.now)
        f.identity["Arn"] = self.worker.owner
        f.record = claim(f.record, self.worker.owner, "acceptance", now=f.now)
        self.service = http_support.Storefront()
        self.target = r.h.p.p.private.public.edge.target

    def probe(self, guard, session, dispatch):
        return PublicProbe(guard, session, dispatch, transport=self.service,
            dns_verify=lambda target, guard: self.assertEqual(target, self.target))

    def perform(self): return self.worker.perform(self.r.h.infra, probe_factory=self.probe)

    def test_real_public_result_and_parent_consumption_keep_baseline_and_testing(self):
        self.perform()
        receipt = read_active_public_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(receipt["kind"], "active-public-acceptance")
        self.assertEqual(receipt["releaseId"], self.release_id)
        self.assertEqual(receipt["result"]["networkView"], "public")
        self.assertEqual(receipt["result"]["private"], "pending")
        self.assertEqual(self.f.record["stages"]["publicAcceptance"], self.cold_public)
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["status"], "TESTING")
        self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        self.run.update(status="completed", conclusion="success", updated_at=self.f.now.isoformat())
        self.assertEqual(self.r.h.observe(3)["waiting"], "ACTIVE_PRIVATE_ACCEPTANCE")
        self.assertEqual(self.f.record["workflowHandoff"]["phase"], "CONSUMED")
        self.assertEqual(len(self.dispatches), 1)

    def test_duplicate_public_worker_does_not_repeat_order_requests(self):
        self.perform()
        count = len(self.service.calls)
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(len(self.service.calls), count)

    def test_wrong_rollout_or_release_cannot_probe(self):
        self.worker.release_id = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.worker.release_id = self.release_id
        self.f.record["releaseCandidate"]["rollout"]["receiptReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.service.calls)

    def test_actual_hosted_job_and_original_infra_sha_are_required(self):
        for obj, key, value in ((self.run, "run_attempt", 2), (self.run, "head_sha", "0" * 40),
                (self.job, "labels", ["self-hosted"]), (self.job, "runner_group_name", "Private runners")):
            original = deepcopy(obj[key]); obj[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.perform()
            obj[key] = original
        self.assertFalse(self.service.calls)

    def test_replaced_handoff_predecessor_is_rejected_before_http(self):
        self.f.record["releaseCandidate"]["promotionApprovalReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.service.calls)

    def test_http_failure_records_active_diagnostics_and_preserves_cold_evidence(self):
        self.service.mutate = lambda response, method, path: response.update(status=503) if path == "/" else None
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["releaseCandidate"]["publicAcceptance"]["state"], "FAILED")
        self.assertEqual(self.f.record["stages"]["publicAcceptance"], self.cold_public)
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        failures = [json.loads(body) for key, (body, _) in self.f.objects.items() if "/active/" in key and key.endswith("failure.json")]
        self.assertEqual(failures[0]["classification"], "http-status")
        self.assertNotIn("body", failures[0])

    def test_cleanup_during_http_wins_over_probe_checkpoint(self):
        def takeover(response, method, path): self.f.record.update(status="DESTROYING")
        self.service.mutate = takeover
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertNotEqual(self.f.record["releaseCandidate"]["publicAcceptance"]["state"], "PASSED")

    def test_cold_receipt_cannot_be_reused_as_active_public_success(self):
        self.perform()
        self.f.record["releaseCandidate"]["publicAcceptance"]["receiptReference"] = deepcopy(self.cold_public["receiptReference"])
        with self.assertRaises(ValueError): read_active_public_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_testing_public_lease_requires_typed_public_handoff_and_role(self):
        record = deepcopy(self.f.record); record.pop("lease")
        with self.assertRaises(ValueError): claim(record, "publisher:200", "acceptance", now=self.f.now)
        record["workflowHandoff"]["operation"] = "review-promotion"
        with self.assertRaises(ValueError): claim(record, self.worker.owner, "acceptance", now=self.f.now)

    def test_real_cli_parent_child_persist_receipt_and_release_acceptance_lease(self):
        import active_public_acceptance
        from leased_command import execute
        self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = encoded(self.d.execution), self.d.version
        case = self
        class Child:
            def __init__(self, command, **kw): self.command = command
            def wait(self, timeout):
                with patch.object(sys, "argv", self.command[1:]): active_public_acceptance.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kw): return execute(*args, **kw, popen=Child)
        arguments = ["active_public_acceptance.py", "--bucket", self.d.bucket, "--session-id", self.f.record["sessionId"], "--execution-version", self.d.version,
            "--release-id", self.release_id, "--rollout-sha256", self.rollout_sha]
        env = dict(RUNNER_ENVIRONMENT="github-hosted", GITHUB_REPOSITORY=INFRA_REPO, GITHUB_REF="refs/heads/main", GITHUB_RUN_ATTEMPT="1", GITHUB_RUN_ID="200", TECHX_DISPATCH_ID=self.dispatch_id, GITHUB_TOKEN="fixture-read")
        perform = self.worker.perform
        with patch.object(sys, "argv", arguments), patch.dict("os.environ", env), patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch("active_public_acceptance.S3SessionStore", return_value=self.f.store), patch("active_public_acceptance.ActivePublicAcceptance", return_value=self.worker) as factory, \
                patch("active_public_acceptance.GitHub", return_value=self.r.h.infra), patch("active_public_acceptance.execute", side_effect=parent), \
                patch.object(self.worker, "perform", side_effect=lambda github: perform(github, probe_factory=self.probe)), redirect_stdout(io.StringIO()) as output:
            active_public_acceptance.main()
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(all(call.kwargs["release_id"] == self.release_id and call.args[-1] == self.rollout_sha for call in factory.call_args_list))
        self.assertIn('"state": "PUBLIC_PASSED"', output.getvalue())
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(self.f.record["releaseCandidate"]["publicAcceptance"]["state"], "PASSED")
        self.assertEqual(self.f.record["status"], "TESTING")


if __name__ == "__main__": unittest.main()
