"""Propose an exact baseline restoration; no merge, deploy, or image publish.

The caller must supply its owned, versioned rollback intent and a guard that
rereads that intent/lease/window. The lifecycle integration remains a separate
consumer of this producer; an unmerged proposal is never rollback completion.
"""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import re

from automation_session import utc
from bootstrap_proposal import ChartProposal
from chart_selection import ALLOWED_FILES, blob, git_tree, sha
from plan_review_store import checksum
from ready_release_contract import validate_manifest


def validate_intent(value):
    fields = {"schemaVersion", "kind", "sessionId", "executionVersion", "failedReleaseId", "failedChartSha", "failureReference",
              "rolloutReference", "acceptedBaseline", "attempt", "createdAt", "expiresAt", "rollbackId", "action", "cleanupStartAt", "deadline"}
    if (not isinstance(value, dict) or set(value) != fields or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or value.get("kind") != "baseline-rollback-intent" or type(value.get("attempt")) is not int or value["attempt"] != 1
            or value.get("action") != "deploy-window-rollback"
            or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", value.get("sessionId", ""))
            or not re.fullmatch(r"[a-f0-9]{64}", value.get("failedReleaseId", ""))
            or checksum({key: item for key, item in value.items() if key != "rollbackId"}) != value.get("rollbackId")
            or not utc(value["createdAt"]) < utc(value["expiresAt"]) <= utc(value["cleanupStartAt"]) < utc(value["deadline"])
            or utc(value["expiresAt"]) > utc(value["createdAt"]) + timedelta(minutes=30)):
        raise ValueError("Exact immutable single-attempt rollback intent required")
    from plan_approval import version
    version(value["executionVersion"])
    baseline = value["acceptedBaseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"platformSha", "chartSha", "images", "acceptanceReference"}:
        raise ValueError("Rollback requires an accepted baseline with acceptance proof")
    sha(value["failedChartSha"]); sha(baseline["chartSha"]); sha(baseline["platformSha"])
    if value["failedChartSha"] == baseline["chartSha"]: raise ValueError("Rollback cannot invent a changed chart")
    for ref in (value["failureReference"], value["rolloutReference"], baseline["acceptanceReference"]):
        if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
                or not isinstance(ref.get("key"), str) or not ref["key"]
                or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
            raise ValueError("Rollback intent must pin its exact failure, rollout and accepted proof")
        version(ref["versionId"])
    prefix = f"private/staging/{value['sessionId']}/active/{value['failedReleaseId']}"
    if value["rolloutReference"]["key"] != prefix + "/rollout.json":
        raise ValueError("Rollback rollout reference belongs to another failed release")
    if not (re.fullmatch(re.escape(prefix) + r"/acceptance/[a-f0-9]{32}\.failure\.json", value["failureReference"]["key"])
            or re.fullmatch(re.escape(f"acceptance/staging/{value['sessionId']}/active/{value['failedReleaseId']}/public/")
                + r"[a-f0-9]{64}/failure\.json", value["failureReference"]["key"])):
        raise ValueError("Rollback failure reference belongs to another release")
    if not (re.fullmatch(re.escape(f"accepted/staging/{value['sessionId']}/") + r"[a-f0-9]{64}\.json", baseline["acceptanceReference"]["key"])
            or re.fullmatch(re.escape(f"private/staging/{value['sessionId']}/acceptance/") + r"[a-f0-9]{32}\.json", baseline["acceptanceReference"]["key"])):
        raise ValueError("Rollback baseline acceptance belongs to another session")
    return value


class BaselineRevert(ChartProposal):
    def __init__(self, github, intent, guard, *, clock):
        self.github, self.intent = github, deepcopy(validate_intent(intent))
        if not callable(guard): raise ValueError("A live owned rollback guard is mandatory")
        self.current_intent, self.clock = guard, clock
        self.baseline = intent["failedChartSha"]
        self.branch = f"rollback/{intent['sessionId']}-{intent['rollbackId'][:24]}"
        self.commit_date = intent["createdAt"]

    def guard(self):
        current = self.current_intent()
        if current != self.intent or not utc(self.intent["createdAt"]) <= self.clock() < utc(self.intent["expiresAt"]):
            raise ValueError("Rollback intent/lease/window changed or expired")
        if self.github.get("git/ref/heads/main").get("object", {}).get("sha") != self.baseline:
            raise ValueError("Chart main moved; never rebase a rollback automatically")
        return dict(sessionId=self.intent["sessionId"]), None

    def message(self, record): return f"revert(staging): restore accepted release for {record['sessionId']}"

    def perform(self, source):
        record, _ = self.guard()
        base_tree, failed = git_tree(self.github, self.baseline)
        _, accepted = git_tree(self.github, self.intent["acceptedBaseline"]["chartSha"])
        if ({key: item for key, item in failed.items() if key not in ALLOWED_FILES}
                != {key: item for key, item in accepted.items() if key not in ALLOWED_FILES}
                or any(name not in accepted or accepted[name]["mode"] != "100644" for name in ALLOWED_FILES)):
            raise ValueError("Rollback cannot restore unrelated chart changes or executable release files")
        manifest = json.loads(blob(self.github, accepted["releases/staging.json"]["sha"]))
        baseline = self.intent["acceptedBaseline"]
        validate_manifest(manifest, baseline["platformSha"])
        names = dict(frontend="frontend", catalog="catalog-api", order="order-api")
        images = {names[row["component"]]: row["image"].split(":staging-", 1)[0] + "@" + row["digest"] for row in manifest["images"]}
        if images != baseline["images"]: raise ValueError("Rollback artifacts differ from accepted runtime digests")
        values = blob(self.github, accepted["values-staging.yaml"]["sha"]).decode("utf-8-sig")
        spec = importlib.util.spec_from_file_location("rollback_manifest", Path(source) / "techx-platform/scripts/prepare-chart-release.py")
        renderer = importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
        # The baseline must already select these digests. Rendering validates
        # every image block but cannot silently repair or change accepted bytes.
        if renderer.render_values(values, manifest) != values:
            raise ValueError("Accepted chart values do not select the accepted manifest")
        expected = dict(deepcopy(failed), **{name: deepcopy(accepted[name]) for name in ALLOWED_FILES})
        if expected == failed: raise ValueError("Rollback has no release diff; do not create a misleading PR")
        tree = self.write("git/trees", dict(base_tree=base_tree,
            tree=[dict(path=name, type="blob", **accepted[name]) for name in sorted(ALLOWED_FILES)]))
        tree_sha = sha(tree.get("sha"))
        author = dict(name="techx-rollback[bot]", email="techx-rollback[bot]@users.noreply.github.com",
            date=utc(self.commit_date).strftime("%Y-%m-%dT%H:%M:%SZ"))
        commit = self.write("git/commits", dict(message=self.message(record), tree=tree_sha, parents=[self.baseline], author=author, committer=author))
        head = sha(commit.get("sha"))
        if commit.get("tree", {}).get("sha") != tree_sha or [row.get("sha") for row in commit.get("parents", [])] != [self.baseline]:
            raise ValueError("Rollback commit lost its exact failed-chart parent/tree")
        actual_tree, actual = git_tree(self.github, head)
        if actual_tree != tree_sha or actual != expected: raise ValueError("Rollback changed more than the two accepted release blobs")
        current = self.ref()
        if current not in (None, head): raise ValueError("An existing rollback branch cannot be replaced")
        if current is None:
            try: self.write("git/refs", dict(ref=f"refs/heads/{self.branch}", sha=head))
            except Exception:
                if self.ref() != head: raise
            if self.ref() != head: raise ValueError("Rollback branch creation remains unobserved")
        pull = self.pull(head)
        if pull is None:
            try:
                self.write("pulls", dict(title=self.message(record), head=self.branch, base="main", draft=False, maintainer_can_modify=False,
                    body=f"Restore the two release files to accepted chart `{baseline['chartSha']}` after release `{self.intent['failedReleaseId']}` failed. "
                    "CI and fresh human approval are required. This proposal does not merge, deploy or mark the environment recovered."))
            except Exception:
                if self.pull(head) is None: raise
            pull = self.pull(head)
            if pull is None: raise ValueError("Rollback PR creation remains unobserved")
        self.guard()
        return dict(schemaVersion=1, kind="baseline-rollback-proposal", rollbackId=self.intent["rollbackId"],
            branch=self.branch, baselineSha=self.baseline, headSha=head, treeSha=tree_sha,
            acceptedBaseline=deepcopy(baseline), **pull)
