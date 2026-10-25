from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from chart_selection import CHART_REPO, INFRA_REPO, read_proposal, read_release, read_selection, selection
from cleanup_schedule import definition
from lifecycle_coordinator import initial_record
from plan_review_store import ACCOUNT, checksum
from select_bootstrap_chart import ChartSelector, WORKFLOW


def blob_sha(data): return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class Conflict(Exception): pass


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.execution = initial_record("staging-select123", {"techx-chart": "a" * 40, "techx-platform": "c" * 40, "techx-infra": "d" * 40},
            4, "owner@example.invalid", "deploy-window", self.now, approved_budget_usd=60)
        self.execution["costEstimate"] = dict(approvedBudgetUsd=60, upperBoundUsd=20, maximumHours=4, includesAutomation=True)
        self.record = dict(deepcopy(self.execution), status="PROVISIONING", executionVersion="exec-v1", stages=dict(foundation=dict(state="APPLIED")))
        self.manifest = dict(commit="c" * 40, tag="staging-" + "c" * 12, images=[dict(component=name,
            image=f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/techx/{name}:staging-{'c' * 12}", digest="sha256:" + "e" * 64)
            for name in ("frontend", "catalog", "order")])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name)
        repo = Path(__file__).parents[2]
        (self.source / "techx-chart").mkdir()
        (self.source / "techx-platform/scripts").mkdir(parents=True)
        self.base_values = (repo / "techx-chart/values-staging.yaml").read_bytes()
        (self.source / "techx-chart/values-staging.yaml").write_bytes(self.base_values)
        validator = repo / "techx-platform/scripts/prepare-chart-release.py"
        (self.source / "techx-platform/scripts/prepare-chart-release.py").write_bytes(validator.read_bytes())
        spec = importlib.util.spec_from_file_location("selection_fixture", validator)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        selected_values = module.render_values(self.base_values.decode(), self.manifest).encode()
        base_files = {"values-staging.yaml": self.base_values, "releases/staging.json": b"{}", "templates/deployment.yaml": b"trusted template"}
        selected_files = dict(base_files, **{"values-staging.yaml": selected_values, "releases/staging.json": json.dumps(self.manifest).encode()})
        self.blobs = {blob_sha(value): value for value in [*base_files.values(), *selected_files.values()]}
        self.base_tree, self.selected_tree = "1" * 40, "2" * 40
        def tree(files, value): return dict(sha=value, truncated=False, tree=[dict(path=name, type="blob", mode="100644", sha=blob_sha(data)) for name, data in files.items()])
        self.chart_data = {
            "pulls/7": dict(number=7, merged=True, state="closed", changed_files=2, merge_commit_sha="b" * 40, user=dict(login="release[bot]"),
                base=dict(ref="main", repo=dict(full_name=CHART_REPO)), head=dict(sha="f" * 40, repo=dict(full_name=CHART_REPO))),
            "pulls/7/files?per_page=100": [dict(filename=name, status="modified") for name in ("values-staging.yaml", "releases/staging.json")],
            "pulls/7/reviews?per_page=100": [dict(state="APPROVED", commit_id="f" * 40, submitted_at="2026-10-24T00:00:00Z", user=dict(type="User", id=1, login="reviewer"))],
            "git/ref/heads/main": dict(object=dict(sha="b" * 40)),
            "actions/workflows/verify.yml/runs?head_sha=" + "b" * 40 + "&per_page=100": dict(total_count=1, workflow_runs=[
                dict(id=11, head_sha="b" * 40, head_branch="main", event="push", path=".github/workflows/verify.yml",
                    repository=dict(full_name=CHART_REPO), head_repository=dict(full_name=CHART_REPO), status="completed", conclusion="success")]),
            "git/commits/" + "a" * 40: dict(sha="a" * 40, tree=dict(sha=self.base_tree)),
            "git/commits/" + "b" * 40: dict(sha="b" * 40, tree=dict(sha=self.selected_tree)),
            f"git/trees/{self.base_tree}?recursive=1": tree(base_files, self.base_tree),
            f"git/trees/{self.selected_tree}?recursive=1": tree(selected_files, self.selected_tree),
        }
        self.main_reads = 0
        self.race_main = self.race_cleanup = False
        def chart_get(endpoint):
            if endpoint == "git/ref/heads/main":
                self.main_reads += 1
                if self.main_reads > 1:
                    if self.race_main: return dict(object=dict(sha="0" * 40))
                    if self.race_cleanup: self.record["status"] = "DESTROYING"
            if endpoint.startswith("git/blobs/"):
                digest = endpoint.rsplit("/", 1)[1]
                payload = self.blobs[digest]
                return dict(sha=digest, encoding="base64", content=base64.b64encode(payload).decode(), size=len(payload))
            return deepcopy(self.chart_data[endpoint])
        self.chart = SimpleNamespace(get=chart_get)
        self.infra_run = dict(id=100, status="in_progress", event="workflow_dispatch", run_attempt=1, head_branch="main",
            head_sha="d" * 40, path=WORKFLOW, repository=dict(full_name=INFRA_REPO), head_repository=dict(full_name=INFRA_REPO))
        self.infra = SimpleNamespace(get=lambda _: deepcopy(self.infra_run))
        self.release = dict(schemaVersion=1, sessionId=self.record["sessionId"], executionVersion="exec-v1",
            sourceRevisions=self.execution["sourceRevisions"], manifest=self.manifest, sourceCiPassed=True,
            sourceCi=dict(repository="cloud-computing-microservice/techx-platform", platformSha="c" * 40, infraSha="d" * 40,
                workflowRunId=100, ciRunId=99))
        self.release_key = f"releases/staging/{self.record['sessionId']}/bootstrap.json"
        self.record["bootstrapRelease"] = dict(key=self.release_key, versionId="v1", sha256=checksum(self.release))
        self.objects = {self.release_key: json.dumps(self.release).encode()}
        self.proposal = dict(schemaVersion=1, sessionId=self.record["sessionId"], executionVersion="exec-v1",
            sourceRevisions=self.execution["sourceRevisions"], releaseReference=deepcopy(self.record["bootstrapRelease"]),
            baselineSha="a" * 40, branch=f"release/bootstrap-{self.record['sessionId']}", headSha="f" * 40, treeSha=self.selected_tree,
            prNumber=7, pullRequest=f"https://github.com/{CHART_REPO}/pull/7")
        proposal_key = f"proposals/staging/{self.record['sessionId']}/bootstrap.json"
        self.objects[proposal_key] = json.dumps(self.proposal).encode()
        self.record["bootstrapProposal"] = dict(key=proposal_key, versionId="v1", sha256=checksum(self.proposal))
        self.writes = []
        def get(**kw): return dict(VersionId="v1", Body=io.BytesIO(self.objects[kw["Key"]]))
        def put(**kw):
            if kw["Key"] in self.objects: raise AssertionError("Immutable receipt cannot be replaced")
            self.objects[kw["Key"]] = kw["Body"]
            self.writes.append(kw["Key"])
            return dict(VersionId="v1")
        def listing(**kw): return dict(Contents=[dict(Key=kw["Prefix"])] if kw["Prefix"] in self.objects else [], IsTruncated=False)
        self.identity = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-chart-selector/techx-chart-select-100")
        self.schedule = definition(self.record, "exec-v1", f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
            f"arn:aws:iam::{ACCOUNT}:role/techx-automation-scheduler", f"arn:aws:sqs:us-east-1:{ACCOUNT}:techx-automation-cleanup-dlq", self.now)
        self.schedule["Arn"] = f"arn:aws:scheduler:us-east-1:{ACCOUNT}:schedule/techx-staging/{self.schedule['Name']}"
        self.record["cleanupScheduleArn"] = self.schedule["Arn"]
        self.etag = 1
        def write(value, etag):
            if etag != self.etag: raise Conflict()
            self.record = deepcopy(value)
            self.etag += 1
        self.store = SimpleNamespace(read=lambda: (deepcopy(self.record), self.etag), write=write)
        self.clients = dict(sts=SimpleNamespace(get_caller_identity=lambda: deepcopy(self.identity)),
            scheduler=SimpleNamespace(get_schedule=lambda **kw: deepcopy(self.schedule)),
            s3=SimpleNamespace(get_object=get, put_object=put, list_objects_v2=listing))
        self.selector = ChartSelector(self.clients, self.store, "control-bucket", self.execution, "exec-v1", 100, clock=lambda: self.now)

    def select(self):
        return self.selector.publish(self.source, self.chart, self.infra, "b" * 40, 7)

    def test_verified_pr_ci_tree_and_manifest_produce_one_protected_selection(self):
        ref = self.select()
        self.assertEqual(self.record["bootstrapChart"], ref)
        value = read_selection(self.clients["s3"], "control-bucket", self.record, self.execution, "exec-v1")
        self.assertEqual(value["chartSha"], "b" * 40)
        self.assertEqual(value["provenance"]["reviewers"], ["reviewer"])
        self.assertEqual(self.select(), ref)
        self.assertEqual(len(self.writes), 1)

    def test_latest_failed_ci_cannot_fall_back_to_an_old_green_run(self):
        runs = self.chart_data["actions/workflows/verify.yml/runs?head_sha=" + "b" * 40 + "&per_page=100"]
        newer = dict(runs["workflow_runs"][0], id=12, conclusion="failure")
        runs["workflow_runs"].append(newer)
        runs["total_count"] = 2
        with self.assertRaisesRegex(ValueError, "latest chart main CI"): self.select()
        self.assertFalse(self.writes)

    def test_fork_unmerged_pr_or_stale_human_review_is_rejected(self):
        pull = self.chart_data["pulls/7"]
        pull["head"]["repo"]["full_name"] = "other/chart"
        with self.assertRaises(ValueError): self.select()
        pull["head"]["repo"]["full_name"] = CHART_REPO
        pull["merged"] = False
        with self.assertRaises(ValueError): self.select()
        pull["merged"] = True
        self.chart_data["pulls/7/reviews?per_page=100"][0]["commit_id"] = "0" * 40
        with self.assertRaises(ValueError): self.select()

    def test_change_request_after_approval_blocks_selection(self):
        self.chart_data["pulls/7/reviews?per_page=100"].append(dict(state="CHANGES_REQUESTED", commit_id="f" * 40,
            submitted_at="2026-10-24T01:00:00Z", user=dict(type="User", id=1, login="reviewer")))
        with self.assertRaises(ValueError): self.select()

    def test_pending_review_is_not_a_decision_and_missing_human_identity_is_rejected(self):
        reviews = self.chart_data["pulls/7/reviews?per_page=100"]
        reviews.append(dict(state="PENDING", submitted_at=None, user=dict(type="User", id=2, login="another")))
        self.select()
        reviews[0]["user"].pop("login")
        with self.assertRaisesRegex(ValueError, "identity is incomplete"): self.select()

    def test_template_or_release_content_outside_manifest_is_rejected(self):
        tree = self.chart_data[f"git/trees/{self.selected_tree}?recursive=1"]["tree"]
        original = deepcopy(tree)
        tree[-1]["sha"] = "9" * 40  # templates/deployment.yaml
        with self.assertRaisesRegex(ValueError, "baseline"): self.select()
        tree[:] = original
        payload = b"wrong values"
        digest = blob_sha(payload)
        self.blobs[digest] = payload
        tree[0]["sha"] = digest
        with self.assertRaisesRegex(ValueError, "exact cold-published"): self.select()

    def test_truncated_tree_or_blob_checksum_mismatch_is_rejected(self):
        tree = self.chart_data[f"git/trees/{self.selected_tree}?recursive=1"]
        tree["truncated"] = True
        with self.assertRaises(ValueError): self.select()
        tree["truncated"] = False
        digest = tree["tree"][0]["sha"]
        self.blobs[digest] = b"tampered"
        with self.assertRaisesRegex(ValueError, "checksum"): self.select()

    def test_main_move_or_cleanup_takeover_never_publishes_a_receipt(self):
        self.race_main = True
        with self.assertRaises(ValueError): self.select()
        self.race_main = False
        self.main_reads = 0
        self.race_cleanup = True
        with self.assertRaises(ValueError): self.select()
        self.assertFalse(self.writes)

    def test_selector_identity_and_workflow_source_are_enforced(self):
        self.identity["Arn"] += "other"
        with self.assertRaises(ValueError): self.select()
        self.identity["Arn"] = self.identity["Arn"][:-5]
        self.infra_run["head_branch"] = "untrusted"
        with self.assertRaises(ValueError): self.select()
        self.assertFalse(self.writes)

    def test_consumer_rejects_another_release_or_corrupt_selection(self):
        ref = self.select()
        original = deepcopy(self.record["bootstrapRelease"])
        self.record["bootstrapRelease"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): read_selection(self.clients["s3"], "control-bucket", self.record, self.execution, "exec-v1")
        self.record["bootstrapRelease"] = original
        value = json.loads(self.objects[ref["key"]])
        value["provenance"]["reviewers"] = []
        self.objects[ref["key"]] = json.dumps(value).encode()
        self.record["bootstrapChart"]["sha256"] = checksum(value)
        with self.assertRaisesRegex(ValueError, "provenance"):
            read_selection(self.clients["s3"], "control-bucket", self.record, self.execution, "exec-v1")

    def test_lost_session_cas_recovers_original_immutable_receipt(self):
        write = self.store.write
        def lost(*args): raise Conflict()
        self.store.write = lost
        with self.assertRaises(Conflict): self.select()
        self.assertEqual(len(self.writes), 1)
        self.store.write = write
        ref = self.select()
        self.assertEqual(self.record["bootstrapChart"], ref)
        self.assertEqual(len(self.writes), 1)

    def test_missing_or_different_proposal_head_cannot_select_an_equivalent_release_pr(self):
        ref = self.record.pop("bootstrapProposal")
        with self.assertRaisesRegex(ValueError, "proposal receipt"): self.select()
        self.record["bootstrapProposal"] = ref
        value = json.loads(self.objects[ref["key"]])
        value["headSha"] = "0" * 40
        self.objects[ref["key"]] = json.dumps(value).encode()
        self.record["bootstrapProposal"]["sha256"] = checksum(value)
        with self.assertRaisesRegex(ValueError, "immutable proposal"): self.select()
        self.assertFalse(self.writes)

    def test_null_object_versions_are_not_allowed_to_fall_back_to_latest(self):
        for field, reader in (("bootstrapRelease", read_release), ("bootstrapProposal", read_proposal)):
            old = self.record[field]["versionId"]
            self.record[field]["versionId"] = None
            with self.subTest(field=field), self.assertRaises(ValueError):
                reader(self.clients["s3"], "control-bucket", self.record, self.execution, "exec-v1")
            self.record[field]["versionId"] = old
        self.select()
        self.record["bootstrapChart"]["versionId"] = None
        with self.assertRaises(ValueError): read_selection(self.clients["s3"], "control-bucket", self.record, self.execution, "exec-v1")

    def test_caller_success_boolean_without_real_source_proof_is_not_a_release(self):
        artifact = deepcopy(self.release)
        artifact.pop("sourceCi")
        self.objects[self.release_key] = json.dumps(artifact).encode()
        self.record["bootstrapRelease"]["sha256"] = checksum(artifact)
        with self.assertRaisesRegex(ValueError, "source/CI provenance"): self.select()
        self.assertFalse(self.writes)


if __name__ == "__main__": unittest.main()
