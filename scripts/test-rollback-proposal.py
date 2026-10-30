"""Actual guarded Git tree/ref/PR restoration against accepted baseline blobs."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import unittest

from bootstrap_proposal import blob_sha
from chart_selection import ALLOWED_FILES
from plan_review_store import checksum
from rollback_proposal import BaselineRevert, validate_intent

spec = importlib.util.spec_from_file_location("rollback_chart_support", Path(__file__).with_name("test-bootstrap-proposal.py"))
support = importlib.util.module_from_spec(spec); spec.loader.exec_module(support)


class RevertTests(unittest.TestCase):
    def setUp(self):
        self.p = support.ProposalTests()
        self.p.setUp(); self.addCleanup(self.p.doCleanups)
        p = self.p
        p.perform()
        accepted, accepted_tree = p.branch, p.commits[p.branch]["tree"]["sha"]
        self.accepted_tree = deepcopy(p.trees[accepted_tree])
        self.accepted_manifest = deepcopy(p.f.manifest)
        new_manifest = deepcopy(p.f.manifest)
        new_manifest.update(commit="c" * 40, tag="staging-" + "c" * 12)
        for row in new_manifest["images"]:
            row.update(image=row["image"].split(":staging-", 1)[0] + ":" + new_manifest["tag"], digest="sha256:" + "f" * 64)
        renderer_spec = importlib.util.spec_from_file_location("rollback_fixture_renderer", p.source / "techx-platform/scripts/prepare-chart-release.py")
        renderer = importlib.util.module_from_spec(renderer_spec); renderer_spec.loader.exec_module(renderer)
        accepted_values = p.blobs[self.accepted_tree["values-staging.yaml"]["sha"]].decode()
        changed = {"values-staging.yaml": renderer.render_values(accepted_values, new_manifest).encode(), "releases/staging.json": json.dumps(new_manifest).encode()}
        failed_tree = deepcopy(self.accepted_tree)
        for name, data in changed.items():
            digest = blob_sha(data); p.blobs[digest] = data
            failed_tree[name] = dict(sha=digest, mode="100644")
        p.base = p.main = "f" * 40; p.base_tree = "e" * 40
        p.trees[p.base_tree] = failed_tree
        p.commits[p.base] = dict(sha=p.base, tree=dict(sha=p.base_tree), parents=[dict(sha=accepted)])
        p.branch, p.pr = None, None
        p.calls.clear()
        self.now = datetime.now(timezone.utc)
        session, failed_id = p.f.record["sessionId"], "a" * 64
        def ref(key): return dict(key=key, versionId="proof-v1", sha256="b" * 64)
        names = dict(frontend="frontend", catalog="catalog-api", order="order-api")
        images = {names[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in self.accepted_manifest["images"]}
        self.intent = dict(schemaVersion=1, kind="baseline-rollback-intent", sessionId=session, executionVersion="execution-v1",
            failedReleaseId=failed_id, failedChartSha=p.base,
            failureReference=ref(f"acceptance/staging/{session}/active/{failed_id}/public/" + "c" * 64 + "/failure.json"),
            rolloutReference=ref(f"private/staging/{session}/active/{failed_id}/rollout.json"), attempt=1,
            acceptedBaseline=dict(platformSha=self.accepted_manifest["commit"], chartSha=accepted, images=images,
                acceptanceReference=ref(f"private/staging/{session}/acceptance/" + "d" * 32 + ".json")),
            createdAt=self.now.isoformat(), expiresAt=(self.now + timedelta(minutes=20)).isoformat(), action="deploy-window-rollback",
            cleanupStartAt=(self.now + timedelta(minutes=90)).isoformat(), deadline=(self.now + timedelta(hours=3)).isoformat())
        self.intent["rollbackId"] = checksum(self.intent)
        get = p.github.get
        p.github.get = lambda endpoint: get(endpoint.replace("git/ref/heads/rollback/", "git/ref/heads/release/"))
        self.producer = BaselineRevert(p.github, self.intent, lambda: deepcopy(self.intent), clock=lambda: self.now)
        p.proposal = self.producer

    def perform(self): return self.producer.perform(self.p.source)

    def test_real_git_restore_uses_accepted_blobs_and_only_opens_one_unmerged_pr(self):
        result = self.perform()
        self.assertEqual(result["kind"], "baseline-rollback-proposal")
        self.assertEqual(self.p.trees[result["treeSha"]], self.accepted_tree)
        self.assertEqual(self.p.commits[result["headSha"]]["parents"], [dict(sha=self.intent["failedChartSha"])])
        self.assertEqual(self.perform(), result)
        self.assertEqual(len([call for call in self.p.calls if call[0] == "pulls"]), 1)
        self.assertFalse(self.p.pr.get("merged_at"))
        self.assertTrue(all(endpoint in {"git/trees", "git/commits", "git/refs", "pulls"} for endpoint, _ in self.p.calls))

    def test_lost_ref_and_pr_responses_reconcile_positive_objects_without_second_creation(self):
        self.p.lost_ref = self.p.lost_pr = True
        result = self.perform()
        self.assertEqual(result["headSha"], self.p.branch)
        self.assertEqual(len([call for call in self.p.calls if call[0] == "git/refs"]), 1)
        self.assertEqual(len([call for call in self.p.calls if call[0] == "pulls"]), 1)

    def test_unrelated_chart_changes_are_not_reverted(self):
        self.p.trees[self.p.base_tree]["templates/deployment.yaml"]["sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "unrelated"): self.perform()
        self.assertFalse(self.p.calls)

    def test_accepted_manifest_or_values_with_wrong_digest_cannot_restore(self):
        self.intent["acceptedBaseline"]["images"]["order-api"] = "wrong"
        self.intent["rollbackId"] = checksum({key: item for key, item in self.intent.items() if key != "rollbackId"})
        self.producer = BaselineRevert(self.p.github, self.intent, lambda: deepcopy(self.intent), clock=lambda: self.now)
        with self.assertRaisesRegex(ValueError, "accepted runtime digests"): self.perform()
        self.assertFalse(self.p.calls)

    def test_current_main_or_intent_or_window_change_fences_proposal(self):
        self.p.main = "0" * 40
        with self.assertRaisesRegex(ValueError, "main moved"): self.perform()
        self.p.main = self.p.base
        self.now += timedelta(minutes=21)
        with self.assertRaisesRegex(ValueError, "expired"): self.perform()
        self.assertFalse(self.p.calls)

    def test_main_move_during_git_object_creation_prevents_branch_or_pr_mutation(self):
        self.p.on_post = lambda endpoint: setattr(self.p, "main", "0" * 40)
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.p.branch); self.assertFalse(self.p.pr)

    def test_existing_other_branch_or_closed_pr_is_not_replaced(self):
        self.p.branch = "0" * 40
        with self.assertRaisesRegex(ValueError, "cannot be replaced"): self.perform()
        self.assertFalse(self.p.pr)

    def test_tampered_tree_cannot_become_a_reviewable_rollback(self):
        self.p.bad_tree = True
        with self.assertRaises(ValueError): self.perform()
        self.assertFalse(self.p.branch)

    def test_attempt_or_cross_session_proof_cannot_authorize_a_proposal(self):
        for field, value in (("attempt", 2), ("schemaVersion", True), ("action", "deploy-window"),
                             ("expiresAt", (self.now + timedelta(hours=2)).isoformat()),
                             ("rolloutReference", dict(self.intent["rolloutReference"], key="private/other/rollout.json"))):
            changed = dict(deepcopy(self.intent), **{field: value})
            changed["rollbackId"] = checksum({key: item for key, item in changed.items() if key != "rollbackId"})
            with self.subTest(field=field), self.assertRaises(ValueError): validate_intent(changed)


if __name__ == "__main__": unittest.main()
