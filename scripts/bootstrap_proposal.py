"""Propose only the two cold-release files; never merge or move an existing ref."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from automation_session import utc
from chart_selection import ALLOWED_FILES, CHART_REPO, GitHub, git_tree, read_release, sha
from plan_review_store import checksum, encoded, immutable_json, read_json


class ChartWriter(GitHub):
    def __init__(self, token):
        super().__init__(CHART_REPO, token)

    def post(self, endpoint, payload):
        if endpoint not in ("git/blobs", "git/trees", "git/commits", "git/refs", "pulls"):
            raise ValueError("Chart proposal does not support this endpoint")
        request = Request(f"https://api.github.com/repos/{CHART_REPO}/{endpoint}", data=encoded(payload), method="POST", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "techx-bootstrap-proposal"})
        with urlopen(request, timeout=30) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024: raise ValueError("Chart write response exceeds size policy")
        return json.loads(data)


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class ChartProposal:
    def __init__(self, publisher, github):
        self.publisher, self.github = publisher, github
        record, _ = publisher.guard()
        self.release_ref = deepcopy(record.get("bootstrapRelease"))
        self.release = read_release(publisher.clients["s3"], publisher.bucket, record, publisher.execution, publisher.version)
        self.baseline = publisher.execution["sourceRevisions"]["techx-chart"]
        self.branch = f"release/bootstrap-{record['sessionId']}"

    def guard(self):
        record, etag = self.publisher.guard()
        if record.get("bootstrapRelease") != self.release_ref:
            raise ValueError("Cold release changed during chart proposal")
        if self.github.get("git/ref/heads/main").get("object", {}).get("sha") != self.baseline:
            raise ValueError("Chart main differs from the admitted baseline; do not rebase automatically")
        return record, etag

    def write(self, endpoint, payload):
        self.guard()
        return self.github.post(endpoint, payload)

    def ref(self):
        try:
            value = self.github.get(f"git/ref/heads/{self.branch}")
        except HTTPError as error:
            if error.code == 404: return None
            raise
        if value.get("ref") != f"refs/heads/{self.branch}" or value.get("object", {}).get("type") != "commit":
            raise ValueError("Existing proposal ref is ambiguous")
        return sha(value["object"].get("sha"))

    def pull(self, commit):
        rows = self.github.get(f"pulls?state=all&head=cloud-computing-microservice:{self.branch}&base=main&per_page=100")
        if not isinstance(rows, list) or len(rows) > 1:
            raise ValueError("Proposal pull request lookup is ambiguous")
        if not rows: return None
        row = rows[0]
        number = row.get("number")
        if (type(number) is not int or number < 1 or row.get("html_url") != f"https://github.com/{CHART_REPO}/pull/{number}"
                or row.get("state") != "open" or row.get("merged_at") is not None or row.get("draft") is not False
                or row.get("head", {}).get("ref") != self.branch or row["head"].get("sha") != commit
                or row.get("base", {}).get("ref") != "main" or row["base"].get("sha") != self.baseline
                or any(row.get(side, {}).get("repo", {}).get("full_name") != CHART_REPO for side in ("head", "base"))):
            raise ValueError("Existing proposal belongs to a different source/base or is no longer open")
        return dict(prNumber=number, pullRequest=row["html_url"])

    def perform(self, source):
        record, _ = self.guard()
        tree_sha, baseline = git_tree(self.github, self.baseline)
        values = (source / "techx-chart/values-staging.yaml").read_bytes()
        if blob_sha(values) != baseline["values-staging.yaml"]["sha"]:
            raise ValueError("Archived chart values differ from admitted Git tree")
        spec = importlib.util.spec_from_file_location("proposal_manifest", source / "techx-platform/scripts/prepare-chart-release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = self.release["manifest"]
        files = {"values-staging.yaml": module.render_values(values.decode("utf-8-sig"), manifest).encode(),
                 "releases/staging.json": (json.dumps(manifest, indent=2) + "\n").encode()}
        expected, entries = deepcopy(baseline), []
        for name in sorted(ALLOWED_FILES):
            digest = blob_sha(files[name])
            result = self.write("git/blobs", dict(content=files[name].decode(), encoding="utf-8"))
            if result.get("sha") != digest: raise ValueError("GitHub blob differs from exact rendered release")
            mode = baseline.get(name, {}).get("mode", "100644")
            expected[name] = dict(sha=digest, mode=mode)
            entries.append(dict(path=name, mode=mode, type="blob", sha=digest))
        tree = self.write("git/trees", dict(base_tree=tree_sha, tree=entries))
        new_tree = sha(tree.get("sha"))
        author = dict(name="techx-release[bot]", email="techx-release[bot]@users.noreply.github.com",
                      date=utc(self.publisher.execution["createdAt"]).strftime("%Y-%m-%dT%H:%M:%SZ"))
        commit = self.write("git/commits", dict(message=f"chore(staging): bootstrap {record['sessionId']}",
            tree=new_tree, parents=[self.baseline], author=author, committer=author))
        commit_sha = sha(commit.get("sha"))
        if commit.get("tree", {}).get("sha") != new_tree or [row.get("sha") for row in commit.get("parents", [])] != [self.baseline]:
            raise ValueError("Proposal commit does not retain the admitted parent/tree")
        actual_tree, actual = git_tree(self.github, commit_sha)
        if actual_tree != new_tree or actual != expected:
            raise ValueError("Proposal may modify only the exact two release blobs")
        current = self.ref()
        if current not in (None, commit_sha): raise ValueError("Existing proposal branch cannot be replaced")
        if current is None:
            try:
                self.write("git/refs", dict(ref=f"refs/heads/{self.branch}", sha=commit_sha))
            except Exception:
                if self.ref() != commit_sha: raise
            if self.ref() != commit_sha: raise ValueError("Proposal branch creation remains unproven")
        pull = self.pull(commit_sha)
        if pull is None:
            body = (f"Staging session: `{record['sessionId']}`\n\nSource commit: `{manifest['commit']}`\n\n"
                    f"Admitted chart baseline: `{self.baseline}`\n\n"
                    "Three immutable ECR digests passed the scan gate. Chart CI and human review are required before merge. "
                    "Do not merge after the session closes; this proposal does not authorize deployment.\n")
            try:
                self.write("pulls", dict(title=f"chore(staging): bootstrap {record['sessionId']}", head=self.branch,
                    base="main", body=body, draft=False, maintainer_can_modify=False))
            except Exception:
                if self.pull(commit_sha) is None: raise
            pull = self.pull(commit_sha)
            if pull is None: raise ValueError("Pull request creation remains unproven")
        record, etag = self.guard()
        value = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=self.publisher.version,
            sourceRevisions=self.publisher.execution["sourceRevisions"], releaseReference=self.release_ref,
            baselineSha=self.baseline, branch=self.branch, headSha=commit_sha, treeSha=new_tree, **pull)
        key = f"proposals/staging/{record['sessionId']}/bootstrap.json"
        ref = immutable_json(self.publisher.clients["s3"], self.publisher.bucket, key, value)
        stored, _ = read_json(self.publisher.clients["s3"], self.publisher.bucket, key, ref["versionId"])
        ref["sha256"] = checksum(stored)
        record, etag = self.guard()
        if record.get("bootstrapProposal") not in (None, ref): raise ValueError("Proposal handoff cannot replace another receipt")
        updated = deepcopy(record)
        updated["bootstrapProposal"] = ref
        self.publisher.store.write(updated, etag)
        return dict(reference=ref, **pull)
