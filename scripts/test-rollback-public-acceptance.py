"""Real restored private pipeline, hosted public probe and terminal consumption."""
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import textwrap
import unittest
from unittest.mock import patch

from automation_session import claim, release
from chart_selection import INFRA_REPO
from deployment_tasks import finish_task
from public_probe import PublicProbe
from rollback_public_acceptance import RollbackPublicAcceptance
from rollback_public_contract import read_public_receipt
from rollback_release_controller import RollbackReleaseController
from rollback_task_contract import slot
from workflow_handoff import target_for, title


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


support = load("rollback_public_restore_fixture", "test-private-rollback-rollout.py")
http_support = load("rollback_public_http_fixture", "test-public-probe.py")


class RollbackPublicTests(unittest.TestCase):
    def setUp(self):
        self.r = support.RollbackRolloutTests()
        self.r.setUp(); self.addCleanup(self.r.doCleanups)
        r = self.r
        r.perform()
        self.f, self.d = r.f, r.d
        f = self.f
        f.record = release(f.record, r.owner, r.epoch)
        finish_task(f.store, r.task, True, f.now)
        r.manager.wait(r.pointer)
        r.build.update(buildStatus="SUCCEEDED", buildComplete=True)
        self.baseline, self.failed = deepcopy(f.record["acceptedRelease"]), deepcopy(f.record["releaseCandidate"])
        self.run, self.job, self.dispatches = {}, {}, []
        self.controller = RollbackReleaseController(self.d, r.infra, r.chart.github, r.runtime.source, "300")
        get = r.infra.get
        def read(endpoint):
            if endpoint == "actions/runs/2100": return deepcopy(self.run)
            if endpoint == "actions/runs/2100/jobs?filter=latest&per_page=100": return dict(total_count=1, jobs=[deepcopy(self.job)])
            return get(endpoint)
        def dispatch(target, identity):
            self.assertEqual(target["operation"], "accept-rollback-public")
            self.dispatches.append(deepcopy(target))
            self.run.update(id=2100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
                head_sha=target["sourceSha"], path=".github/workflows/staging-acceptance.yml", display_title=title(identity),
                repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO), created_at=f.now.isoformat())
            self.job.update(id=2101, run_id=2100, head_sha=target["sourceSha"], name="public", status="in_progress",
                runner_group_name="GitHub Actions", runner_id=2102, labels=["ubuntu-24.04"])
            return dict(workflow_run_id=2100, run_url=f"https://api.github.com/repos/{INFRA_REPO}/actions/runs/2100",
                html_url=f"https://github.com/{INFRA_REPO}/actions/runs/2100")
        r.infra.get, r.infra.dispatch = read, dispatch
        f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.assertEqual(self.controller.observe_public()["waiting"], "ROLLBACK_PUBLIC_RUNNING")
        self.dispatch_id = f.record["workflowHandoff"]["id"]
        self.rollback_id = slot(f.record)["intent"]["rollbackId"]
        self.rollout_sha = slot(f.record)["rolloutReference"]["sha256"]
        self.worker = RollbackPublicAcceptance(f.clients, f.store, self.d.bucket, self.d.execution, self.d.version, 2100,
            self.dispatch_id, self.rollout_sha, rollback_id=self.rollback_id, clock=lambda: f.now)
        f.identity["Arn"] = self.worker.owner
        f.record = claim(f.record, self.worker.owner, "rollback", now=f.now)
        self.service = http_support.Storefront()
        self.target = r.original.h.p.p.private.public.edge.target

    def probe(self, guard, session, dispatch):
        return PublicProbe(guard, session, dispatch, transport=self.service, dns_verify=lambda target, guard: self.assertEqual(target, self.target))

    def perform(self): return self.worker.perform(self.r.infra, probe_factory=self.probe)

    def terminal(self, *, job_success=True):
        if self.f.record.get("lease"):
            self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        self.run.update(status="completed", conclusion="success", updated_at=self.f.now.isoformat())
        self.job.update(status="completed", conclusion="success" if job_success else "failure")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"

    def test_restored_public_suite_and_terminal_consumption_retain_old_baseline_and_failed_candidate(self):
        self.perform()
        proof = read_public_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)
        self.assertEqual(proof["kind"], "rollback-public-acceptance")
        self.assertEqual(proof["rollbackId"], self.rollback_id)
        self.assertEqual(proof["rolloutReference"], slot(self.f.record)["rolloutReference"])
        self.terminal()
        result = self.controller.observe_public()
        self.assertEqual(result["waiting"], "ROLLBACK_PRIVATE_ACCEPTANCE")
        self.assertEqual(self.controller.observe_public(), result)
        self.assertEqual(len(self.dispatches), 1)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["releaseCandidate"], self.failed)
        self.assertIn("restoration", slot(self.f.record))
        self.assertFalse(self.f.cleanup)

    def test_duplicate_delivery_or_changed_rollout_cannot_reprobe(self):
        self.perform()
        with self.assertRaises(ValueError): self.perform()
        slot(self.f.record)["rolloutReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.worker.validate_record(self.f.record)

    def test_cold_receipt_cannot_stand_in_for_restored_public_runtime(self):
        self.perform()
        slot(self.f.record)["publicAcceptance"]["receiptReference"] = deepcopy(self.f.record["stages"]["publicAcceptance"]["receiptReference"])
        with self.assertRaises(ValueError): read_public_receipt(self.f.clients["s3"], self.d.bucket, self.f.record, self.d.version)

    def test_known_application_failure_records_failed_checkpoint_without_another_rollback(self):
        self.service.mutate = lambda response, method, path: response.update(status=503) if path == "/" else None
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(slot(self.f.record)["publicAcceptance"]["state"], "FAILED")
        self.terminal(); self.run["conclusion"] = "failure"
        with self.assertRaises(ValueError): self.controller.observe_public()
        self.assertTrue(self.f.cleanup)
        self.assertEqual(slot(self.f.record)["attempt"], 1)
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_self_hosted_job_cannot_produce_restored_public_evidence(self):
        self.job["runner_group_name"] = "Self-hosted"
        with self.assertRaises(ValueError): self.perform()
        self.assertNotIn("publicAcceptance", slot(self.f.record))

    def test_green_workflow_with_failed_actual_public_job_is_not_consumed(self):
        self.perform(); self.terminal(job_success=False)
        with self.assertRaises(ValueError): self.controller.observe_public()
        self.assertNotIn("restoration", slot(self.f.record))

    def test_cleanup_during_probe_prevents_passed_receipt_and_preserves_baseline(self):
        self.service.mutate = lambda response, method, path: self.f.record.update(status="DESTROYING")
        with self.assertRaises(ValueError): self.perform()
        self.assertEqual(self.f.record["status"], "DESTROYING")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_expired_original_rollback_window_cannot_start_public_probe(self):
        self.f.now += timedelta(minutes=31)
        with self.assertRaises(ValueError): self.perform()

    def test_positive_receipt_recovers_calling_while_probe_progress_exists_without_redispatch(self):
        self.perform()
        self.f.record["workflowHandoff"]["phase"] = "CALLING"
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        self.assertEqual(self.controller.observe_public()["waiting"], "ROLLBACK_PUBLIC_RUNNING")
        self.assertEqual(len(self.dispatches), 1)
        self.terminal()
        self.assertEqual(self.controller.observe_public()["waiting"], "ROLLBACK_PRIVATE_ACCEPTANCE")
        target = target_for("accept-rollback-public", self.f.record, self.d.execution)
        self.controller.handoff.start(target)
        self.assertEqual(len(self.dispatches), 1)

    def test_unknown_calling_without_positive_receipt_never_reposts_the_workflow(self):
        self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        self.f.record["workflowHandoff"]["phase"] = "CALLING"
        receipt_key = self.f.record["workflowHandoff"]["receiptReference"]["key"]
        listing = self.f.clients["s3"].list_objects_v2
        self.f.clients["s3"].list_objects_v2 = lambda **kwargs: dict(IsTruncated=False, Contents=[]) if kwargs["Prefix"] == receipt_key else listing(**kwargs)
        get = self.r.infra.get
        self.r.infra.get = lambda endpoint: dict(total_count=0, workflow_runs=[]) if endpoint.startswith("actions/workflows/staging-acceptance.yml/runs?") else get(endpoint)
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-300"
        with self.assertRaises(ValueError): self.controller.observe_public()
        self.assertEqual(len(self.dispatches), 1)
        self.assertTrue(self.f.cleanup)

    def test_real_hosted_parent_worker_cli_keeps_failed_state_and_releases_lease(self):
        import rollback_public_acceptance
        from leased_command import execute
        self.f.record = release(self.f.record, self.worker.owner, self.f.record["lease"]["epoch"])
        original = self.worker.perform
        def perform(github): return original(github, probe_factory=self.probe)
        commands = []
        class Child:
            def __init__(self, command, **kwargs): self.command = command; commands.append(command)
            def wait(self, timeout):
                with patch.object(sys, "argv", self.command[1:]): rollback_public_acceptance.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kwargs): return execute(*args, **kwargs, popen=Child)
        arguments = ["rollback_public_acceptance.py", "--bucket", self.d.bucket, "--session-id", self.f.record["sessionId"],
            "--execution-version", self.d.version, "--rollback-id", self.rollback_id, "--rollout-sha256", self.rollout_sha]
        env = dict(RUNNER_ENVIRONMENT="github-hosted", GITHUB_REPOSITORY=INFRA_REPO, GITHUB_REF="refs/heads/main",
            GITHUB_RUN_ATTEMPT="1", GITHUB_RUN_ID="2100", GITHUB_TOKEN="fixture-read-only", TECHX_DISPATCH_ID=self.dispatch_id)
        with patch.dict(os.environ, env), patch.object(sys, "argv", arguments), \
                patch("boto3.client", side_effect=lambda name, **kwargs: self.f.clients[name]), \
                patch("rollback_public_acceptance.S3SessionStore", return_value=self.f.store), \
                patch("rollback_public_acceptance.RollbackPublicAcceptance", return_value=self.worker), \
                patch("rollback_public_acceptance.GitHub", return_value=self.r.infra), \
                patch("rollback_public_acceptance.execute", side_effect=parent), \
                patch.object(self.worker, "perform", side_effect=perform), redirect_stdout(io.StringIO()) as output:
            rollback_public_acceptance.main()
        self.assertEqual(len(commands), 1)
        self.assertIn('"state": "PUBLIC_PASSED"', output.getvalue())
        self.assertNotIn("lease", self.f.record)
        self.assertEqual(self.f.record["status"], "FAILED_NEEDS_CLEANUP")

    def test_workflow_rejects_mixed_or_incomplete_scope_before_aws_credentials(self):
        text = (Path(__file__).parents[1] / ".github/workflows/staging-acceptance.yml").read_text()
        program = re.search(r"python - <<'PY'\n(.*?)\n          PY", text, re.S).group(1)
        code = compile(textwrap.dedent(program), "staging-acceptance-input-validator", "exec")
        common = dict(SESSION_ID=self.f.record["sessionId"], EXECUTION_VERSION=self.d.version, TECHX_DISPATCH_ID=self.dispatch_id)
        def validate(activation="", release="", rollout="", rollback=""):
            values = dict(common, ACTIVATION_SHA256=activation, RELEASE_ID=release, ROLLOUT_SHA256=rollout, ROLLBACK_ID=rollback)
            with patch.dict(os.environ, values): exec(code, {})
        validate(activation="a" * 64)
        validate(release="b" * 64, rollout="c" * 64)
        validate(rollback="d" * 64, rollout="e" * 64)
        for values in ({}, dict(release="b" * 64), dict(rollback="d" * 64), dict(rollout="e" * 64),
                dict(activation="a" * 64, rollback="d" * 64, rollout="e" * 64),
                dict(release="b" * 64, rollback="d" * 64, rollout="e" * 64), dict(rollback="bad", rollout="e" * 64)):
            with self.subTest(values=values), self.assertRaises(AssertionError): validate(**values)


if __name__ == "__main__": unittest.main()
