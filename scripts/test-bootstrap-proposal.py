from copy import deepcopy
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError

from bootstrap_proposal import ChartProposal, ChartWriter, blob_sha
from chart_selection import ALLOWED_FILES, CHART_REPO, read_proposal
from select_bootstrap_chart import ChartSelector, WORKFLOW as SELECT_WORKFLOW

spec = importlib.util.spec_from_file_location("proposal_publisher_support", Path(__file__).with_name("test-bootstrap-publish.py"))
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.f = support.ColdPublishTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.perform()
        self.source = self.f.base / "proposal-source"
        (self.source / "techx-chart").mkdir(parents=True)
        (self.source / "techx-platform/scripts").mkdir(parents=True)
        root = Path(__file__).parents[2]
        values = (root / "techx-chart/values-staging.yaml").read_bytes()
        (self.source / "techx-chart/values-staging.yaml").write_bytes(values)
        (self.source / "techx-platform/scripts/prepare-chart-release.py").write_bytes((root / "techx-platform/scripts/prepare-chart-release.py").read_bytes())
        self.files = {"values-staging.yaml": values, "releases/staging.json": b"{}", "templates/deployment.yaml": b"keep this template"}
        self.base = "a" * 40
        self.main, self.branch, self.pr = self.base, None, None
        self.base_tree = "1" * 40
        self.blobs = {blob_sha(data): data for data in self.files.values()}
        self.trees = {self.base_tree: {path: dict(sha=blob_sha(data), mode="100644") for path, data in self.files.items()}}
        self.commits = {self.base: dict(sha=self.base, tree=dict(sha=self.base_tree), parents=[])}
        self.calls, self.on_post = [], None
        self.lost_ref = self.lost_pr = self.bad_tree = self.bad_blob = False
        self.github = SimpleNamespace(get=self.get, post=self.post)
        self.proposal = ChartProposal(self.f.publisher, self.github)

    def get(self, endpoint):
        if endpoint == "git/ref/heads/main": return dict(object=dict(sha=self.main))
        if endpoint.startswith("git/ref/heads/release/"):
            if self.branch is None: raise HTTPError("https://api.github.com/ref", 404, "missing", {}, None)
            return dict(ref=f"refs/heads/{self.proposal.branch}", object=dict(type="commit", sha=self.branch))
        if endpoint.startswith("git/commits/"): return deepcopy(self.commits[endpoint.split("/")[-1]])
        if endpoint.startswith("git/blobs/"):
            sha = endpoint.split("/")[-1]
            data = self.blobs[sha]
            return dict(sha=sha, size=len(data), encoding="base64", content=base64.b64encode(data).decode())
        if endpoint.startswith("git/trees/"):
            sha = endpoint.split("/")[-1].split("?")[0]
            rows = [dict(path=path, type="blob", **metadata) for path, metadata in self.trees[sha].items()]
            if self.bad_tree and sha != self.base_tree: rows.append(dict(path="evil.txt", type="blob", mode="100644", sha="e" * 40))
            return dict(sha=sha, truncated=False, tree=rows)
        if endpoint.startswith("pulls?"): return [deepcopy(self.pr)] if self.pr else []
        raise AssertionError(endpoint)

    def post(self, endpoint, payload):
        self.calls.append((endpoint, deepcopy(payload)))
        if self.on_post: self.on_post(endpoint)
        if endpoint == "git/blobs":
            data = payload["content"].encode()
            sha = blob_sha(data)
            self.blobs[sha] = data
            return dict(sha="0" * 40 if self.bad_blob else sha)
        if endpoint == "git/trees":
            self.assertEqual({row["path"] for row in payload["tree"]}, ALLOWED_FILES)
            tree = deepcopy(self.trees[payload["base_tree"]])
            for row in payload["tree"]: tree[row["path"]] = dict(sha=row["sha"], mode=row["mode"])
            sha = hashlib.sha1(json.dumps(tree, sort_keys=True).encode()).hexdigest()
            self.trees[sha] = tree
            return dict(sha=sha)
        if endpoint == "git/commits":
            sha = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            value = dict(sha=sha, tree=dict(sha=payload["tree"]), parents=[dict(sha=item) for item in payload["parents"]])
            self.commits[sha] = value
            return deepcopy(value)
        if endpoint == "git/refs":
            if self.branch is not None: raise HTTPError("https://api.github.com/ref", 422, "exists", {}, None)
            self.branch = payload["sha"]
            if self.lost_ref: raise TimeoutError("GitHub ref response lost")
            return dict()
        if endpoint == "pulls":
            self.assertFalse(self.pr)
            self.assertEqual(payload["base"], "main")
            self.pr = dict(number=7, html_url=f"https://github.com/{CHART_REPO}/pull/7", state="open", draft=False, merged_at=None,
                head=dict(ref=payload["head"], sha=self.branch, repo=dict(full_name=CHART_REPO)),
                base=dict(ref="main", sha=self.base, repo=dict(full_name=CHART_REPO)))
            if self.lost_pr: raise TimeoutError("GitHub PR response lost")
            return deepcopy(self.pr)
        raise AssertionError(endpoint)

    def perform(self): return self.proposal.perform(self.source)

    def test_exact_manifest_changes_only_two_blobs_and_creates_one_unmerged_proposal(self):
        result = self.perform()
        value = read_proposal(self.f.clients["s3"], self.f.publisher.bucket, self.f.record, self.f.execution, "exec-v1")
        self.assertEqual(value["headSha"], self.branch)
        self.assertEqual(value["prNumber"], result["prNumber"])
        actual = self.trees[value["treeSha"]]
        self.assertEqual(actual["templates/deployment.yaml"], self.trees[self.base_tree]["templates/deployment.yaml"])
        self.assertEqual(json.loads(self.blobs[actual["releases/staging.json"]["sha"]]), self.f.manifest)
        self.assertEqual(self.main, self.base)
        self.assertEqual(self.f.record["status"], "PROVISIONING")
        self.assertNotIn("bootstrapChart", self.f.record)
        self.assertEqual(sum(endpoint == "pulls" for endpoint, _ in self.calls), 1)

    def test_retry_is_deterministic_and_never_updates_an_existing_branch_or_duplicates_pr(self):
        first = self.perform()
        self.assertEqual(self.perform(), first)
        self.assertEqual(sum(endpoint == "git/refs" for endpoint, _ in self.calls), 1)
        self.assertEqual(sum(endpoint == "pulls" for endpoint, _ in self.calls), 1)
        commits = [payload for endpoint, payload in self.calls if endpoint == "git/commits"]
        self.assertEqual(commits[0], commits[1])
        self.assertEqual(commits[0]["parents"], [self.base])

    def test_changed_main_or_archived_values_fail_before_any_write(self):
        self.main = "b" * 40
        with self.assertRaises(ValueError): self.perform()
        self.main = self.base
        (self.source / "techx-chart/values-staging.yaml").write_text("tampered")
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)

    def test_changed_blob_or_extra_template_content_cannot_create_a_ref(self):
        self.bad_blob = True
        with self.assertRaisesRegex(ValueError, "blob differs"): self.perform()
        self.bad_blob = False
        self.bad_tree = True
        with self.assertRaisesRegex(ValueError, "two release blobs"): self.perform()
        self.assertFalse(self.branch)
        self.assertFalse(self.pr)

    def test_conflicting_branch_or_closed_pr_cannot_be_overwritten_or_reopened(self):
        self.branch = "f" * 40
        with self.assertRaisesRegex(ValueError, "branch cannot be replaced"): self.perform()
        self.assertFalse(self.pr)
        self.branch = None
        self.perform()
        self.pr["state"] = "closed"
        before = sum(endpoint == "pulls" for endpoint, _ in self.calls)
        with self.assertRaisesRegex(ValueError, "no longer open"): self.perform()
        self.assertEqual(sum(endpoint == "pulls" for endpoint, _ in self.calls), before)

    def test_lost_ref_and_pr_responses_recover_by_positive_read_without_duplicate_posts(self):
        self.lost_ref = self.lost_pr = True
        self.perform()
        self.assertEqual(sum(endpoint == "git/refs" for endpoint, _ in self.calls), 1)
        self.assertEqual(sum(endpoint == "pulls" for endpoint, _ in self.calls), 1)
        self.assertIn("bootstrapProposal", self.f.record)

    def test_cleanup_during_object_creation_stops_before_branch_and_receipt(self):
        self.on_post = lambda endpoint: self.f.record.update(status="DESTROYING")
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.branch)
        self.assertFalse(self.pr)
        self.assertNotIn("bootstrapProposal", self.f.record)

    def test_lost_session_cas_can_reuse_original_proposal_receipt(self):
        write = self.f.store.write
        self.f.store.write = lambda *args: (_ for _ in ()).throw(support.Conflict())
        with self.assertRaises(support.Conflict): self.perform()
        self.assertTrue(any(key.startswith("proposals/") for key in self.f.objects))
        self.assertNotIn("bootstrapProposal", self.f.record)
        self.f.store.write = write
        self.perform()
        self.assertEqual(sum(endpoint == "pulls" for endpoint, _ in self.calls), 1)

    def test_wrong_release_or_selected_session_cannot_create_another_proposal(self):
        self.f.record["bootstrapRelease"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.calls)
        self.f.record["bootstrapChart"] = dict(key="selected")
        with self.assertRaises(ValueError): self.perform()

    def test_writer_has_no_ref_update_merge_or_delete_endpoint(self):
        writer = ChartWriter("fake-test-token")
        for endpoint in ("pulls/7/merge", "git/refs/heads/main", "contents/values-staging.yaml"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError): writer.post(endpoint, {})

    def test_emitted_proposal_is_consumed_by_selector_after_human_review_merge_and_exact_ci(self):
        self.perform()
        proposal = read_proposal(self.f.clients["s3"], self.f.publisher.bucket, self.f.record, self.f.execution, "exec-v1")
        merged_sha = "b" * 40
        self.commits[merged_sha] = dict(sha=merged_sha, tree=dict(sha=proposal["treeSha"]), parents=[dict(sha=self.base), dict(sha=self.branch)])
        self.main = merged_sha
        self.f.record.pop("lease")
        self.f.identity["Arn"] = "arn:aws:sts::058114477594:assumed-role/techx-automation-chart-selector/techx-chart-select-200"
        self.f.clients["s3"].list_objects_v2 = lambda **kw: dict(Contents=[], IsTruncated=False)
        def chart_get(endpoint):
            if endpoint == "pulls/7":
                return dict(self.pr, merged=True, state="closed", changed_files=2, merge_commit_sha=merged_sha, user=dict(login="release[bot]"))
            if endpoint == "pulls/7/files?per_page=100": return [dict(filename=name, status="modified") for name in sorted(ALLOWED_FILES)]
            if endpoint == "pulls/7/reviews?per_page=100":
                return [dict(state="APPROVED", commit_id=self.branch, submitted_at=self.f.now.isoformat(), user=dict(type="User", id=1, login="reviewer"))]
            if endpoint.startswith("actions/workflows/verify.yml/runs?"):
                return dict(total_count=1, workflow_runs=[dict(id=301, head_sha=merged_sha, head_branch="main", event="push",
                    path=".github/workflows/verify.yml", repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO),
                    status="completed", conclusion="success")])
            return self.get(endpoint)
        infra = SimpleNamespace(get=lambda _: dict(id=200, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha="a" * 40, path=SELECT_WORKFLOW, repository=dict(full_name="cloud-computing-microservice/techx-infra"),
            head_repository=dict(full_name="cloud-computing-microservice/techx-infra")))
        selector = ChartSelector(self.f.clients, self.f.store, self.f.publisher.bucket, self.f.execution, "exec-v1", 200, clock=lambda: self.f.now)
        ref = selector.publish(self.source, SimpleNamespace(get=chart_get), infra, merged_sha, 7)
        self.assertEqual(self.f.record["bootstrapChart"], ref)
        chosen = json.loads(self.f.objects[ref["key"]])
        self.assertEqual(chosen["proposalReference"], self.f.record["bootstrapProposal"])
        self.assertEqual(chosen["chartSha"], merged_sha)
        self.assertEqual(chosen["provenance"]["prHeadSha"], self.branch)


if __name__ == "__main__": unittest.main()
