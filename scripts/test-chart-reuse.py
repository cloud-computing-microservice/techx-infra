from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from chart_selection import CHART_REPO, INFRA_REPO, REUSE_WORKFLOW, read_proposal, read_selection
from plan_review_store import checksum
from reuse_bootstrap_chart import ChartReuse


def support(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selection_support = support("reuse_selection_support", "test-chart-selection.py")
proposal_support = support("reuse_proposal_support", "test-bootstrap-proposal.py")


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.f = selection_support.SelectionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        # The newly admitted baseline already contains exactly this release.
        f.chart_data["git/commits/" + "a" * 40]["tree"]["sha"] = f.selected_tree
        selected = f.chart_data[f"git/trees/{f.selected_tree}?recursive=1"]["tree"]
        values_sha = next(row["sha"] for row in selected if row["path"] == "values-staging.yaml")
        (f.source / "techx-chart/values-staging.yaml").write_bytes(f.blobs[values_sha])
        f.chart_data["git/ref/heads/main"] = dict(object=dict(sha="a" * 40))
        ci = deepcopy(f.chart_data["actions/workflows/verify.yml/runs?head_sha=" + "b" * 40 + "&per_page=100"])
        for run in ci["workflow_runs"]: run["head_sha"] = "a" * 40
        f.chart_data["actions/workflows/verify.yml/runs?head_sha=" + "a" * 40 + "&per_page=100"] = ci
        f.proposal = dict(schemaVersion=2, kind="reuse", sessionId=f.record["sessionId"], executionVersion="exec-v1",
            sourceRevisions=f.execution["sourceRevisions"], releaseReference=f.record["bootstrapRelease"],
            baselineSha="a" * 40, headSha="a" * 40, treeSha=f.selected_tree)
        self.save_proposal()
        f.infra_run["path"] = REUSE_WORKFLOW
        self.approvals = [dict(state="approved", environments=[dict(id=9, name="staging-chart-reuse")],
            user=dict(id=123, type="User", login="reviewer"))]
        def infra_get(endpoint):
            if endpoint == "actions/runs/100/approvals": return deepcopy(self.approvals)
            if endpoint == "actions/runs/100": return deepcopy(f.infra_run)
            raise AssertionError(endpoint)
        f.infra = SimpleNamespace(get=infra_get)
        self.reader = ChartReuse(f.clients, f.store, "control-bucket", f.execution, "exec-v1", 100,
                                read_only=True, clock=lambda: f.now)
        self.writer = ChartReuse(f.clients, f.store, "control-bucket", f.execution, "exec-v1", 100, clock=lambda: f.now)

    def save_proposal(self):
        ref = self.f.record["bootstrapProposal"]
        self.f.objects[ref["key"]] = json.dumps(self.f.proposal).encode()
        ref["sha256"] = checksum(self.f.proposal)

    def preview(self):
        f = self.f
        f.identity["Arn"] = self.reader.expected_identity()
        return self.reader.preview(f.source, f.chart, f.infra)

    def approve(self, request):
        f = self.f
        f.identity["Arn"] = self.writer.expected_identity()
        return self.writer.approve(f.source, f.chart, f.infra, request, checksum(request))

    def test_read_only_preview_requires_real_zero_diff_intent_and_exposes_no_email(self):
        request = self.preview()
        self.assertEqual(request["baselineSha"], "a" * 40)
        self.assertEqual(request["manifest"], self.f.manifest)
        self.assertNotIn("owner@example.invalid", json.dumps(request))
        self.assertFalse(self.f.writes)
        self.assertNotIn("bootstrapChart", self.f.record)
        with self.assertRaisesRegex(ValueError, "preview identity"):
            self.reader.approve(self.f.source, self.f.chart, self.f.infra, request, checksum(request))

    def test_human_review_selects_exact_baseline_and_retry_preserves_receipt(self):
        request = self.preview()
        ref = self.approve(request)
        chosen = read_selection(self.f.clients["s3"], "control-bucket", self.f.record, self.f.execution, "exec-v1")
        self.assertEqual(chosen["kind"], "reuse")
        self.assertEqual(chosen["chartSha"], "a" * 40)
        self.assertEqual(chosen["provenance"]["previewSha256"], checksum(request))
        self.assertEqual(chosen["provenance"]["reviewer"], "reviewer")
        self.assertEqual(self.approve(request), ref)
        self.assertEqual(len(self.f.writes), 1)

    def test_missing_rejected_bot_or_ambiguous_review_cannot_select(self):
        request = self.preview()
        valid = deepcopy(self.approvals)
        histories = [[], [dict(valid[0], state="rejected")],
            [dict(valid[0], user=dict(id=123, type="Bot", login="release[bot]"))], valid + valid]
        for history in histories:
            self.approvals = history
            with self.subTest(history=history), self.assertRaises(ValueError): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_changed_preview_checksum_or_new_ci_requires_new_human_review(self):
        request = self.preview()
        changed = deepcopy(request)
        changed["manifest"]["images"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "changed after human review"): self.approve(changed)
        self.f.identity["Arn"] = self.writer.expected_identity()
        with self.assertRaisesRegex(ValueError, "exact reviewed reuse preview"):
            self.writer.approve(self.f.source, self.f.chart, self.f.infra, request, "0" * 64)
        runs = self.f.chart_data["actions/workflows/verify.yml/runs?head_sha=" + "a" * 40 + "&per_page=100"]
        runs["workflow_runs"][0]["id"] += 1
        with self.assertRaisesRegex(ValueError, "changed after human review"): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_changed_release_must_use_pr_and_cannot_use_reuse_review(self):
        f = self.f
        f.proposal = dict(f.proposal, schemaVersion=1, branch=f"release/bootstrap-{f.record['sessionId']}",
                         prNumber=7, pullRequest=f"https://github.com/{CHART_REPO}/pull/7")
        f.proposal.pop("kind")
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "reviewed PR"): self.preview()
        self.assertFalse(f.writes)

    def test_reuse_proposal_with_pr_identity_or_other_head_is_invalid(self):
        self.f.proposal["prNumber"] = 7
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "without a PR"): self.preview()
        self.f.proposal.pop("prNumber")
        self.f.proposal["headSha"] = "b" * 40
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "without a PR"): self.preview()
        self.assertFalse(self.f.writes)

    def test_proposal_tree_or_manifest_drift_cannot_reuse(self):
        self.f.proposal["treeSha"] = "0" * 40
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "zero tree changes"): self.preview()
        self.f.proposal["treeSha"] = self.f.selected_tree
        self.save_proposal()
        self.f.release["manifest"]["images"][0]["digest"] = "sha256:" + "0" * 64
        self.f.objects[self.f.release_key] = json.dumps(self.f.release).encode()
        self.f.record["bootstrapRelease"]["sha256"] = checksum(self.f.release)
        self.f.proposal["releaseReference"] = deepcopy(self.f.record["bootstrapRelease"])
        self.save_proposal()
        with self.assertRaisesRegex(ValueError, "exact cold-published manifest"): self.preview()
        self.assertFalse(self.f.writes)

    def test_closed_deadline_busy_or_cleanup_takeover_blocks_publication(self):
        request = self.preview()
        for name, value in (("status", "DESTROYING"), ("cleanupStartAt", self.f.now.isoformat()),
                            ("lease", dict(owner="other", epoch=1))):
            original = deepcopy(self.f.record)
            self.f.record[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError): self.approve(request)
            self.f.record = original
        self.f.race_cleanup = True
        with self.assertRaises(ValueError): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_main_move_or_latest_failed_ci_blocks_reuse(self):
        request = self.preview()
        self.f.race_main = True
        with self.assertRaises(ValueError): self.approve(request)
        self.f.race_main = False
        runs = self.f.chart_data["actions/workflows/verify.yml/runs?head_sha=" + "a" * 40 + "&per_page=100"]
        runs["workflow_runs"][0]["conclusion"] = "failure"
        with self.assertRaisesRegex(ValueError, "latest chart main CI"): self.approve(request)
        self.assertFalse(self.f.writes)

    def test_workflow_rerun_wrong_source_or_wrong_role_cannot_preview(self):
        for name, value in (("run_attempt", 2), ("path", ".github/workflows/verify.yml"), ("head_sha", "0" * 40)):
            original = self.f.infra_run[name]
            self.f.infra_run[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError): self.preview()
            self.f.infra_run[name] = original
        self.f.identity["Arn"] = self.writer.expected_identity()
        with self.assertRaises(ValueError): self.reader.preview(self.f.source, self.f.chart, self.f.infra)
        self.assertFalse(self.f.writes)

    def test_lost_session_cas_recovers_same_receipt_without_another_selection_write(self):
        request = self.preview()
        original = self.f.store.write
        self.f.store.write = lambda *args: (_ for _ in ()).throw(selection_support.Conflict())
        with self.assertRaises(selection_support.Conflict): self.approve(request)
        self.assertEqual(len(self.f.writes), 1)
        self.f.store.write = original
        ref = self.approve(request)
        self.assertEqual(self.f.record["bootstrapChart"], ref)
        self.assertEqual(len(self.f.writes), 1)

    def test_review_withdrawal_or_cancel_after_preview_blocks_selection_write(self):
        request = self.preview()
        original = self.f.infra.get
        for target in ("approvals", "run"):
            reads = {"approvals": 0, "run": 0}
            def changed(endpoint):
                kind = "approvals" if endpoint.endswith("/approvals") else "run"
                reads[kind] += 1
                result = original(endpoint)
                if kind == target and reads[kind] >= 2:
                    return [] if kind == "approvals" else dict(result, status="cancelled")
                return result
            self.f.infra.get = changed
            with self.subTest(target=target), self.assertRaises(ValueError): self.approve(request)
            self.assertFalse(self.f.writes)
        self.f.infra.get = original

    def test_consumers_reject_fake_reviewer_missing_preview_or_nonbaseline_reuse(self):
        ref = self.approve(self.preview())
        original = json.loads(self.f.objects[ref["key"]])
        for name, value in (("reviewer", "release[bot]"), ("previewSha256", ""), ("reviewRun", "https://example.invalid/run/1")):
            changed = deepcopy(original)
            changed["provenance"][name] = value
            self.f.objects[ref["key"]] = json.dumps(changed).encode()
            self.f.record["bootstrapChart"]["sha256"] = checksum(changed)
            with self.subTest(name=name), self.assertRaises(ValueError):
                read_selection(self.f.clients["s3"], "control-bucket", self.f.record, self.f.execution, "exec-v1")
        changed = dict(original, chartSha="b" * 40)
        self.f.objects[ref["key"]] = json.dumps(changed).encode()
        self.f.record["bootstrapChart"]["sha256"] = checksum(changed)
        with self.assertRaises(ValueError): read_selection(self.f.clients["s3"], "control-bucket", self.f.record, self.f.execution, "exec-v1")


class ProducerReuseTests(unittest.TestCase):
    def test_changed_release_preparation_has_no_github_write_capability_or_receipt(self):
        p = proposal_support.ProposalTests()
        p.setUp()
        self.addCleanup(p.doCleanups)
        readonly = SimpleNamespace(get=p.get)
        preparation = proposal_support.ChartProposal(p.f.publisher, readonly)
        self.assertEqual(preparation.perform(p.source, prepare_only=True), dict(kind="pull-request"))
        self.assertFalse(p.calls)
        self.assertNotIn("bootstrapProposal", p.f.record)

    def test_one_file_change_still_creates_a_real_reviewable_release_pr(self):
        p = proposal_support.ProposalTests()
        p.setUp()
        self.addCleanup(p.doCleanups)
        spec = importlib.util.spec_from_file_location("single_file_render", p.source / "techx-platform/scripts/prepare-chart-release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        values = module.render_values(p.files["values-staging.yaml"].decode(), p.f.manifest).encode()
        p.blobs[proposal_support.blob_sha(values)] = values
        p.trees[p.base_tree]["values-staging.yaml"]["sha"] = proposal_support.blob_sha(values)
        (p.source / "techx-chart/values-staging.yaml").write_bytes(values)
        result = p.perform()
        receipt = read_proposal(p.f.clients["s3"], p.f.publisher.bucket, p.f.record, p.f.execution, "exec-v1")
        changed = {name for name, item in p.trees[receipt["treeSha"]].items() if p.trees[p.base_tree].get(name) != item}
        self.assertEqual(changed, {"releases/staging.json"})
        self.assertEqual(result["prNumber"], 7)
        self.assertNotIn("kind", result)

    def test_real_zero_diff_producer_handoff_requires_fresh_review_without_git_writes(self):
        p = proposal_support.ProposalTests()
        p.setUp()
        self.addCleanup(p.doCleanups)
        # Build a genuine admitted baseline containing the newly cold-published
        # manifest, then use the production producer/reader/approval/consumer.
        spec = importlib.util.spec_from_file_location("reuse_render", p.source / "techx-platform/scripts/prepare-chart-release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = p.proposal.release["manifest"]
        p.files["values-staging.yaml"] = module.render_values(p.files["values-staging.yaml"].decode(), manifest).encode()
        p.files["releases/staging.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
        p.trees[p.base_tree] = {path: dict(sha=proposal_support.blob_sha(data), mode="100644") for path, data in p.files.items()}
        p.blobs.update({proposal_support.blob_sha(data): data for data in p.files.values()})
        (p.source / "techx-chart/values-staging.yaml").write_bytes(p.files["values-staging.yaml"])
        preparation = proposal_support.ChartProposal(p.f.publisher, SimpleNamespace(get=p.get))
        result = preparation.perform(p.source, prepare_only=True)
        self.assertEqual(result["kind"], "reuse")
        self.assertEqual(p.calls, [])
        self.assertIsNone(p.branch)
        self.assertIsNone(p.pr)
        self.assertNotIn("bootstrapChart", p.f.record)
        receipt = read_proposal(p.f.clients["s3"], p.f.publisher.bucket, p.f.record, p.f.execution, "exec-v1")
        self.assertEqual(receipt["headSha"], p.base)
        self.assertEqual(p.perform(), result)
        p.f.record.pop("lease")
        p.f.clients["s3"].list_objects_v2 = lambda **kw: dict(IsTruncated=False, Contents=[dict(Key=kw["Prefix"])] if kw["Prefix"] in p.f.objects else [])
        def chart_get(endpoint):
            if endpoint.startswith("actions/workflows/verify.yml/runs?"):
                return dict(total_count=1, workflow_runs=[dict(id=301, head_sha=p.base, head_branch="main", event="push",
                    path=".github/workflows/verify.yml", repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO),
                    status="completed", conclusion="success")])
            return p.get(endpoint)
        def infra_get(endpoint):
            if endpoint.endswith("/approvals"):
                return [dict(state="approved", environments=[dict(id=9, name="staging-chart-reuse")], user=dict(id=1, type="User", login="reviewer"))]
            return dict(id=200, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main", head_sha="a" * 40,
                        path=REUSE_WORKFLOW, repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        chart, infra = SimpleNamespace(get=chart_get), SimpleNamespace(get=infra_get)
        selector = ChartReuse(p.f.clients, p.f.store, p.f.publisher.bucket, p.f.execution, "exec-v1", 200, clock=lambda: p.f.now)
        p.f.identity["Arn"] = selector.expected_identity()
        request = selector.preview(p.source, chart, infra)
        ref = selector.approve(p.source, chart, infra, request, checksum(request))
        chosen = read_selection(p.f.clients["s3"], p.f.publisher.bucket, p.f.record, p.f.execution, "exec-v1")
        self.assertEqual(chosen["chartSha"], p.base)
        self.assertEqual(chosen["proposalReference"], result["reference"])
        self.assertEqual(chosen["provenance"]["previewSha256"], checksum(request))
        self.assertEqual(p.f.record["bootstrapChart"], ref)
        self.assertEqual(p.calls, [])


if __name__ == "__main__": unittest.main()
