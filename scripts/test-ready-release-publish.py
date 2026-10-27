"""Active release publication against durable cold acceptance and Git fixtures."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from automation_session import claim
from ready_release_contract import read_candidate, read_manifest, read_proposal, source_proof
from ready_release_publish import ReadyProposal, ReadyPublisher


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


private_support = load("active_private_fixture", "test-private-acceptance.py")
proposal_support = load("active_chart_fixture", "test-bootstrap-proposal.py")


class ReadyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.private = private_support.PrivateAcceptanceTests()
        self.private.setUp(); self.addCleanup(self.private.doCleanups)
        self.private.complete_parent()
        f = self.f = self.private.f
        self.source_sha, self.owner = "c" * 40, "cloud-computing-microservice/techx-platform:701:1"
        self.baseline = deepcopy(f.record["acceptedRelease"])
        self.main, self.descendant = self.source_sha, True
        self.run = dict(id=701, run_attempt=1, status="in_progress", event="push", head_branch="main", head_sha=self.source_sha,
            path=".github/workflows/verify.yml", repository=dict(full_name="cloud-computing-microservice/techx-platform"),
            head_repository=dict(full_name="cloud-computing-microservice/techx-platform"))
        self.jobs = [dict(id=801, name="acceptance / acceptance", run_id=701, head_sha=self.source_sha, status="completed", conclusion="success"),
            dict(id=802, name="release / publish", run_id=701, head_sha=self.source_sha, status="in_progress")]
        def get(endpoint):
            if endpoint == "actions/runs/701": return deepcopy(self.run)
            if endpoint == "actions/runs/701/jobs?filter=latest&per_page=100": return dict(total_count=len(self.jobs), jobs=deepcopy(self.jobs))
            if endpoint == "git/ref/heads/main": return dict(object=dict(sha=self.main))
            if endpoint.startswith("compare/"): return dict(status="ahead" if self.descendant else "diverged", merge_base_commit=dict(sha=self.baseline["platformSha"]))
            raise AssertionError(endpoint)
        self.github = SimpleNamespace(get=get)
        f.identity.update(Arn="arn:aws:sts::058114477594:assumed-role/techx-automation-publisher/techx-publish-701")
        self.source = self.private.g.runtime.source
        self.platform = self.source / "techx-platform"
        self.commands, self.on_build = [], None
        self.manifest = dict(commit=self.source_sha, tag="staging-" + self.source_sha[:12], images=[dict(component=name,
            image=f"058114477594.dkr.ecr.us-east-1.amazonaws.com/techx/{name}:staging-{self.source_sha[:12]}", digest="sha256:" + "e" * 64)
            for name in ("frontend", "catalog", "order")])
        def run(command, **kw):
            self.assertEqual(f.record["releaseCandidate"]["state"], "PUBLISHING")
            self.assertEqual(read_candidate(f.clients["s3"], "control-bucket", f.record, "execution-v1")["sourceSha"], self.source_sha)
            self.commands.append(command)
            if self.on_build: self.on_build()
            Path(command[-1]).write_text(json.dumps(self.manifest))
        self.publisher = ReadyPublisher(f.clients, f.store, "control-bucket", f.execution, "execution-v1", self.source_sha, self.owner,
            github=self.github, clock=lambda: f.now, run=run)

    def publish(self):
        self.f.record = claim(self.f.record, self.owner, "release", now=self.f.now)
        self.publisher.epoch = self.f.record["lease"]["epoch"]
        return self.publisher.publish(self.platform, self.source.parent)

    def chart(self):
        chart = proposal_support.ProposalTests()
        chart.setUp(); self.addCleanup(chart.doCleanups)
        old = chart.base
        chart.base = chart.main = self.baseline["chartSha"]
        chart.commits[chart.base] = chart.commits.pop(old)
        chart.commits[chart.base]["sha"] = chart.base
        chart.proposal = ReadyProposal(self.publisher, chart.github)
        return chart

    def test_published_candidate_has_exact_original_baseline_source_proof_and_scan(self):
        self.publish()
        intent = read_candidate(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        manifest = read_manifest(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        self.assertEqual(intent["baseline"], self.baseline)
        self.assertEqual(intent["sourceProof"]["ciJobId"], 801)
        self.assertEqual(manifest["manifest"]["commit"], self.source_sha)
        self.assertEqual(self.f.record["sourceRevisions"], self.f.execution["sourceRevisions"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertEqual(self.f.record["status"], "READY")
        self.assertEqual(self.f.record["releaseCandidate"]["state"], "PUBLISHED")

    def test_real_entry_point_parent_worker_and_proposal_share_one_release_lease(self):
        import run_ready_release
        from leased_command import execute
        self.f.objects[f"executions/{self.f.record['sessionId']}.json"] = json.dumps(self.f.execution).encode(), "execution-v1"
        case = self
        class Child:
            def __init__(self, command, **kw): self.command = command
            def wait(self, timeout):
                case.assertEqual(case.f.record["lease"]["operation"], "release")
                with patch.object(sys, "argv", self.command[1:]): run_ready_release.main()
                return 0
            def poll(self): return 0
        def parent(*args, **kw): return execute(*args, **kw, popen=Child)
        arguments = ["run_ready_release.py", "--bucket", "control-bucket", "--session-id", self.f.record["sessionId"],
            "--source-sha", self.source_sha, "--owner", self.owner, "--platform", str(self.platform)]
        with patch.object(sys, "argv", arguments), patch.dict("os.environ", {"GITHUB_TOKEN": "fixture-read", "GH_TOKEN": "fixture-write"}), \
                patch("boto3.client", side_effect=lambda name, **kw: self.f.clients[name]), \
                patch("run_ready_release.S3SessionStore", return_value=self.f.store), \
                patch("run_ready_release.verified_checkout") as checkout, \
                patch("run_ready_release.GitHub", return_value=self.github), \
                patch("run_ready_release.ReadyPublisher", return_value=self.publisher), \
                patch("run_ready_release.ChartWriter", side_effect=lambda token: self.chart().github), \
                patch("run_ready_release.execute", side_effect=parent), redirect_stdout(io.StringIO()) as output:
            run_ready_release.main()
        self.assertEqual(self.f.record["releaseCandidate"]["state"], "PROPOSED")
        self.assertEqual(self.f.record["status"], "READY")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertNotIn("lease", self.f.record)
        self.assertIn('"state": "RELEASE_PROPOSED"', output.getvalue())
        self.assertEqual(checkout.call_count, 4)
        self.assertEqual([call.args[1] for call in checkout.call_args_list], [self.f.execution["sourceRevisions"]["techx-infra"], self.source_sha] * 2)

    def test_same_run_ci_fork_rerun_and_superseded_source_cannot_publish(self):
        for obj, key, changed in ((self.run, "run_attempt", 2), (self.run, "event", "pull_request"), (self.run, "head_branch", "feature"),
                (self.jobs[0], "conclusion", "failure"), (self.jobs[0], "status", "in_progress"), (self.jobs[0], "head_sha", "f" * 40)):
            original = obj[key]; obj[key] = changed
            with self.subTest(key=key), self.assertRaises(ValueError): source_proof(self.github, self.source_sha, self.owner)
            obj[key] = original
        self.main = "d" * 40
        with self.assertRaises(ValueError): self.publisher.admission(self.f.record)
        self.assertFalse(self.commands)
        self.assertNotIn("releaseCandidate", self.f.record)

    def test_diverged_source_unaccepted_baseline_and_changed_budget_are_rejected(self):
        self.descendant = False
        with self.assertRaises(ValueError): self.publisher.admission(self.f.record)
        self.descendant = True
        self.f.record["acceptedRelease"]["platformSha"] = "f" * 40
        with self.assertRaises(ValueError): self.publisher.admission(self.f.record)
        self.f.record["acceptedRelease"] = deepcopy(self.baseline)
        self.f.record["approvedBudgetUsd"] = 59
        with self.assertRaises(ValueError): self.publisher.admission(self.f.record)

    def test_pending_scan_never_publishes_a_manifest_or_changes_accepted_release(self):
        self.private.scan = "IN_PROGRESS"
        with self.assertRaises(ValueError): self.publish()
        self.assertNotIn("manifestReference", self.f.record["releaseCandidate"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_wrong_published_source_is_rejected_before_chart(self):
        self.manifest["commit"] = "f" * 40
        with self.assertRaises(ValueError): self.publish()
        self.assertNotIn("manifestReference", self.f.record["releaseCandidate"])

    def test_lost_intent_response_is_recovered_without_replacing_original_proof(self):
        put = self.f.clients["s3"].put_object
        lost = []
        def put_object(**kw):
            response = put(**kw)
            if kw["Key"].endswith("/intent.json") and not lost:
                lost.append(kw["Key"])
                raise TimeoutError("Lost stored intent response")
            return response
        self.f.clients["s3"].put_object = put_object
        with self.assertRaises(TimeoutError): self.publish()
        self.assertNotIn("releaseCandidate", self.f.record)
        self.assertFalse(self.commands)
        self.f.now += timedelta(seconds=5)
        self.publish()
        pointer = self.f.record["releaseCandidate"]
        self.assertEqual(pointer["intentReference"]["key"], lost[0])
        self.assertEqual(read_candidate(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")["sourceProof"]["workflowRunId"], 701)

    def test_no_receipt_after_release_lease_is_fenced_or_minimum_window_is_lost(self):
        for expired in (False, True):
            self.f.record = claim(self.f.record, self.owner, "release", now=self.f.now)
            self.publisher.epoch = self.f.record["lease"]["epoch"]
            original = deepcopy(self.f.record)
            if expired: self.f.now += timedelta(hours=4)
            else: self.f.record["lease"]["epoch"] += 1
            with self.subTest(expired=expired), self.assertRaises(ValueError): self.publisher.prepare()
            self.f.record = original
        self.assertFalse(self.commands)

    def test_cleanup_takeover_or_expiring_window_blocks_post_build_receipts(self):
        self.on_build = lambda: self.f.record.update(status="DESTROYING")
        with self.assertRaises(ValueError): self.publish()
        self.assertNotIn("manifestReference", self.f.record["releaseCandidate"])
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)

    def test_competing_candidate_is_rejected_without_a_second_build(self):
        self.publish()
        self.f.record["releaseCandidate"]["id"] = "f" * 64
        with self.assertRaises(ValueError): self.publisher.prepare()
        self.assertEqual(len(self.commands), 1)

    def test_chart_proposal_changes_only_release_files_and_pins_candidate(self):
        self.publish()
        chart = self.chart()
        result = chart.proposal.perform(self.source)
        proposal = read_proposal(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        self.assertEqual(proposal["prNumber"], 7)
        self.assertEqual(proposal["manifestReference"], self.f.record["releaseCandidate"]["manifestReference"])
        self.assertIn(self.f.record["sessionId"], proposal["branch"])
        self.assertEqual(self.f.record["releaseCandidate"]["state"], "PROPOSED")
        self.assertEqual(self.f.record["acceptedRelease"], self.baseline)
        self.assertNotIn("merge", [endpoint for endpoint, _ in chart.calls])
        self.assertEqual(result["prNumber"], 7)

    def test_lost_ref_and_pr_responses_recover_only_exact_created_proposal(self):
        self.publish()
        chart = self.chart()
        chart.lost_ref = chart.lost_pr = True
        chart.proposal.perform(self.source)
        first = deepcopy(self.f.record["releaseCandidate"]["proposalReference"])
        chart.proposal.perform(self.source)
        self.assertEqual(first, self.f.record["releaseCandidate"]["proposalReference"])
        self.assertEqual(sum(endpoint == "pulls" for endpoint, _ in chart.calls), 1)

    def test_chart_main_or_lease_change_prevents_pr_creation(self):
        self.publish()
        chart = self.chart()
        chart.main = "f" * 40
        with self.assertRaises(ValueError): chart.proposal.perform(self.source)
        self.assertFalse(chart.calls)
        self.assertNotIn("proposalReference", self.f.record["releaseCandidate"])

    def test_ready_chart_callback_binds_only_the_actual_active_candidate(self):
        from chart_callback import bound_chart_run
        self.publish()
        chart = self.chart()
        chart.proposal.perform(self.source)
        proposal = read_proposal(self.f.clients["s3"], "control-bucket", self.f.record, "execution-v1")
        notification = dict(id=901, status="completed", run_attempt=1, event="pull_request", path=".github/workflows/verify.yml",
            head_branch=proposal["branch"], head_sha=proposal["headSha"], created_at=self.f.now.isoformat(),
            repository=dict(full_name="cloud-computing-microservice/techx-chart"), head_repository=dict(full_name="cloud-computing-microservice/techx-chart"),
            pull_requests=[dict(number=7, head=dict(sha=proposal["headSha"]), base=dict(ref="main", sha=proposal["baselineSha"]))])
        old = chart.github.get
        chart.pr["merged"] = False
        chart.github.get = lambda path: deepcopy(notification) if path == "actions/runs/901" else deepcopy(chart.pr) if path == "pulls/7" else old(path)
        def bound():
            return bound_chart_run(self.f.clients["s3"], "control-bucket", self.f.record, self.f.execution, "execution-v1", chart.github, 901)
        self.assertTrue(bound())
        original = deepcopy(notification)
        for fields in (dict(head_sha="0" * 40), dict(head_branch="unrelated"), dict(status="in_progress"),
                dict(repository=dict(full_name="fork/chart")), dict(created_at=(self.f.now - timedelta(seconds=1)).isoformat())):
            notification.update(original); notification.update(fields)
            with self.subTest(fields=fields): self.assertFalse(bound())
        notification.update(original)
        self.f.record["releaseCandidate"]["state"] = "PUBLISHED"
        self.assertFalse(bound())
        self.f.record["releaseCandidate"]["state"] = "PROPOSED"
        self.f.record["status"] = "DESTROYING"
        self.assertFalse(bound())

    def test_ready_callback_does_not_fall_back_to_cold_or_ignore_changed_receipt(self):
        from chart_callback import bound_chart_run
        self.publish()
        self.chart().proposal.perform(self.source)
        self.f.record["releaseCandidate"]["manifestReference"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            bound_chart_run(self.f.clients["s3"], "control-bucket", self.f.record, self.f.execution, "execution-v1", SimpleNamespace(get=lambda path: {}), 901)


if __name__ == "__main__": unittest.main()
