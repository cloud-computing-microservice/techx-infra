"""Select a reviewed, CI-verified immutable chart for a cold-start release."""
import base64
import hashlib
import importlib.util
import json
from pathlib import PurePosixPath
import re
from urllib.request import Request, urlopen
from bootstrap_provenance import PLATFORM_REPO, validate_proof

from automation_session import utc
from plan_approval import version
from plan_review_store import ACCOUNT, checksum, read_json

CHART_REPO = "cloud-computing-microservice/techx-chart"
INFRA_REPO = "cloud-computing-microservice/techx-infra"
ALLOWED_FILES = {"values-staging.yaml", "releases/staging.json"}
REUSE_WORKFLOW = ".github/workflows/staging-chart-reuse.yml"
REUSE_ENVIRONMENT = "staging-chart-reuse"


def reuse_proposal(value):
    return type(value.get("schemaVersion")) is int and value["schemaVersion"] == 2 and value.get("kind") == "reuse"


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("A full immutable Git SHA is required")
    return value


class GitHub:
    def __init__(self, repo, token):
        if repo not in (CHART_REPO, INFRA_REPO, PLATFORM_REPO) or not token:
            raise ValueError("A scoped read-only GitHub token and fixed repository are required")
        self.repo, self.token = repo, token

    def get(self, endpoint):
        request = Request(f"https://api.github.com/repos/{self.repo}/{endpoint}", headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "techx-automation"})
        with urlopen(request, timeout=30) as response:
            if response.headers.get("Link"): raise ValueError("Paginated chart provenance is not accepted")
            payload = response.read(2 * 1024 * 1024 + 1)
        if len(payload) > 2 * 1024 * 1024: raise ValueError("Chart provenance response exceeds the size policy")
        return json.loads(payload)


def read_release(client, bucket, record, execution, execution_version):
    ref = record.get("bootstrapRelease", {})
    key = f"releases/staging/{record['sessionId']}/bootstrap.json"
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"} or ref.get("key") != key
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Cold release must have an exact immutable reference")
    version(ref["versionId"])
    artifact, _ = read_json(client, bucket, key, ref["versionId"])
    if (checksum(artifact) != ref["sha256"] or artifact.get("schemaVersion") != 1
            or artifact.get("sessionId") != record["sessionId"] or artifact.get("executionVersion") != execution_version
            or artifact.get("sourceRevisions") != execution["sourceRevisions"] or artifact.get("sourceCiPassed") is not True
            or artifact.get("manifest", {}).get("commit") != execution["sourceRevisions"]["techx-platform"]):
        raise ValueError("Cold release differs from the admitted source/session")
    validate_proof(artifact.get("sourceCi"), execution)
    return artifact


def read_proposal(client, bucket, record, execution, execution_version):
    ref = record.get("bootstrapProposal", {})
    key = f"proposals/staging/{record['sessionId']}/bootstrap.json"
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"} or ref.get("key") != key
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Cold chart selection requires the immutable proposal receipt")
    version(ref["versionId"])
    value, _ = read_json(client, bucket, key, ref["versionId"])
    fields = {"schemaVersion", "sessionId", "executionVersion", "sourceRevisions", "releaseReference", "baselineSha", "headSha", "treeSha"}
    reuse = reuse_proposal(value)
    if (checksum(value) != ref["sha256"] or value.get("sessionId") != record["sessionId"]
            or value.get("executionVersion") != execution_version or value.get("sourceRevisions") != execution["sourceRevisions"]
            or value.get("releaseReference") != record.get("bootstrapRelease") or value.get("baselineSha") != execution["sourceRevisions"]["techx-chart"]):
        raise ValueError("Proposal receipt differs from the admitted release/source/PR")
    if reuse:
        if set(value) != fields | {"kind"} or value.get("headSha") != value["baselineSha"]:
            raise ValueError("Reuse proposal must select only the exact admitted baseline without a PR")
    else:
        number = value.get("prNumber")
        if (type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
                or set(value) != fields | {"branch", "prNumber", "pullRequest"}
                or value.get("branch") != f"release/bootstrap-{record['sessionId']}" or type(number) is not int or number < 1
                or value.get("pullRequest") != f"https://github.com/{CHART_REPO}/pull/{number}"):
            raise ValueError("Proposal receipt has an invalid release PR identity")
    sha(value.get("headSha"))
    sha(value.get("treeSha"))
    return value


def git_tree(github, commit_sha):
    commit = github.get(f"git/commits/{sha(commit_sha)}")
    if commit.get("sha") != commit_sha: raise ValueError("GitHub returned a different chart commit")
    tree_sha = sha(commit["tree"]["sha"])
    result = github.get(f"git/trees/{tree_sha}?recursive=1")
    rows = result.get("tree", [])
    if result.get("sha") != tree_sha or result.get("truncated") is not False or not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("Complete bounded chart tree is required")
    blobs = {}
    for row in rows:
        path = row.get("path", "")
        parts = PurePosixPath(path)
        if not path or parts.is_absolute() or ".." in parts.parts or "\\" in path or ":" in path:
            raise ValueError("Unsafe chart tree path")
        if row.get("type") == "tree" and row.get("mode") == "040000": continue
        if row.get("type") != "blob" or row.get("mode") not in ("100644", "100755") or path in blobs:
            raise ValueError("Chart cannot contain links, submodules or duplicate paths")
        blobs[path] = dict(sha=sha(row.get("sha")), mode=row["mode"])
    return tree_sha, blobs


def blob(github, blob_sha):
    result = github.get(f"git/blobs/{sha(blob_sha)}")
    if result.get("sha") != blob_sha or result.get("encoding") != "base64" or type(result.get("size")) is not int or not 0 <= result["size"] <= 1024 * 1024:
        raise ValueError("Invalid chart blob metadata")
    value = base64.b64decode("".join(result["content"].split()), validate=True)
    actual = hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()
    if len(value) != result["size"] or actual != blob_sha: raise ValueError("Chart blob checksum/size mismatch")
    return value


def provenance(github, chart_sha, pr_number):
    sha(chart_sha)
    if type(pr_number) is not int or pr_number < 1: raise ValueError("A merged chart pull request is required")
    pull = github.get(f"pulls/{pr_number}")
    if (pull.get("number") != pr_number or pull.get("merged") is not True or pull.get("state") != "closed"
            or pull.get("merge_commit_sha") != chart_sha or pull.get("base", {}).get("ref") != "main"
            or any(pull.get(side, {}).get("repo", {}).get("full_name") != CHART_REPO for side in ("base", "head"))):
        raise ValueError("Chart must come from a merged same-repository main release PR")
    head = sha(pull["head"]["sha"])
    files = github.get(f"pulls/{pr_number}/files?per_page=100")
    if (not isinstance(files, list) or not 1 <= len(files) <= 2 or type(pull.get("changed_files")) is not int or pull["changed_files"] != len(files)
            or len({row.get("filename") for row in files}) != len(files)
            or {row.get("filename") for row in files} - ALLOWED_FILES
            or any(row.get("status") not in ("added", "modified") for row in files)):
        raise ValueError("Release PR must change one or two allowed release files")
    reviews = github.get(f"pulls/{pr_number}/reviews?per_page=100")
    if not isinstance(reviews, list): raise ValueError("Chart review history is invalid")
    latest = {}
    decisions = [row for row in reviews if row.get("state") not in ("COMMENTED", "PENDING")]
    for review in sorted(decisions, key=lambda row: utc(row["submitted_at"])):
        if review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            raise ValueError("Unknown chart review decision")
        user = review.get("user", {})
        if user.get("type") != "User" or type(user.get("id")) is not int or user["id"] < 1: continue
        if not isinstance(user.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", user["login"]):
            raise ValueError("Human review identity is incomplete")
        latest[user["id"]] = review
    if any(row.get("state") == "CHANGES_REQUESTED" for row in latest.values()):
        raise ValueError("An unresolved human change request blocks chart selection")
    approved = [row for row in latest.values() if row.get("state") == "APPROVED" and row.get("commit_id") == head
        and row["user"].get("login") != pull.get("user", {}).get("login")]
    if not approved: raise ValueError("The exact chart PR head needs a human approval")
    ci_run = latest_chart_ci(github, chart_sha)
    return dict(pullRequest=f"https://github.com/{CHART_REPO}/pull/{pr_number}", prNumber=pr_number,
        prHeadSha=head, reviewers=sorted({row["user"]["login"] for row in approved}),
        changedFiles=sorted(row["filename"] for row in files), ciRun=ci_run)


def latest_chart_ci(github, chart_sha):
    sha(chart_sha)
    main = github.get("git/ref/heads/main")
    if main.get("object", {}).get("sha") != chart_sha: raise ValueError("Selected release is no longer chart main")
    response = github.get(f"actions/workflows/verify.yml/runs?head_sha={chart_sha}&per_page=100")
    runs = response.get("workflow_runs", [])
    if response.get("total_count") != len(runs) or not runs:
        raise ValueError("Complete chart CI history for the selected SHA is required")
    valid = [run for run in runs if run.get("head_sha") == chart_sha and run.get("head_branch") == "main"
        and run.get("event") in ("push", "workflow_dispatch")
        and run.get("path") in (".github/workflows/verify.yml", ".github/workflows/verify.yml@main")
        and all(run.get(key, {}).get("full_name") == CHART_REPO for key in ("repository", "head_repository"))]
    if not valid or any(type(run.get("id")) is not int or run["id"] < 1 for run in valid):
        raise ValueError("CI must run the trusted chart workflow on exact main SHA")
    run = max(valid, key=lambda row: row["id"])
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("The latest chart main CI run must pass")
    return f"https://github.com/{CHART_REPO}/actions/runs/{run['id']}"


def release_tree(github, source, execution, release, chart_sha):
    """Verify the full tree and exact manifest, including the zero-diff case."""
    baseline_sha = execution["sourceRevisions"]["techx-chart"]
    _, baseline = git_tree(github, baseline_sha)
    tree_sha, chosen = git_tree(github, chart_sha)
    changed = {path for path in set(baseline) | set(chosen) if baseline.get(path) != chosen.get(path)}
    if changed - ALLOWED_FILES or not ALLOWED_FILES <= set(chosen):
        raise ValueError("Selected chart templates/configuration differ from the admitted baseline")
    if any(chosen[path]["mode"] != baseline.get(path, {"mode": "100644"})["mode"] for path in ALLOWED_FILES):
        raise ValueError("Release selection cannot change file permissions")
    source = source.resolve()
    values = (source / "techx-chart/values-staging.yaml").read_bytes()
    if hashlib.sha1(b"blob " + str(len(values)).encode() + b"\0" + values).hexdigest() != baseline["values-staging.yaml"]["sha"]:
        raise ValueError("Archived baseline values differ from the reviewed Git tree")
    module_path = source / "techx-platform/scripts/prepare-chart-release.py"
    spec = importlib.util.spec_from_file_location("selected_manifest", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = release["manifest"]
    module.validate_manifest(manifest)
    expected = module.render_values(values.decode("utf-8-sig"), manifest).replace("\r\n", "\n")
    actual = blob(github, chosen["values-staging.yaml"]["sha"]).decode("utf-8-sig").replace("\r\n", "\n")
    actual_manifest = json.loads(blob(github, chosen["releases/staging.json"]["sha"]).decode("utf-8-sig"))
    if actual != expected or actual_manifest != manifest:
        raise ValueError("Merged chart does not select the exact cold-published manifest")
    if github.get("git/ref/heads/main").get("object", {}).get("sha") != chart_sha:
        raise ValueError("Chart main changed during selection checks")
    return dict(baselineSha=baseline_sha, chartSha=chart_sha, treeSha=tree_sha, changedFiles=sorted(changed))


def selection(github, source, record, execution, execution_version, release, chart_sha, pr_number):
    proof = provenance(github, chart_sha, pr_number)
    tree = release_tree(github, source, execution, release, chart_sha)
    if tree["changedFiles"] != proof["changedFiles"]:
        raise ValueError("Merged PR diff does not match the exact admitted baseline change")
    return dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=execution_version,
        sourceRevisions=execution["sourceRevisions"], releaseReference=record["bootstrapRelease"], proposalReference=record["bootstrapProposal"],
        baselineSha=tree["baselineSha"], chartSha=chart_sha, treeSha=tree["treeSha"], provenance=proof)


def read_selection(client, bucket, record, execution, execution_version):
    ref = record.get("bootstrapChart", {})
    key = f"chart-selections/staging/{record['sessionId']}/bootstrap.json"
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"} or ref.get("key") != key
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Bootstrap chart must have an exact protected selection reference")
    version(ref["versionId"])
    proposal = record.get("bootstrapProposal", {})
    if (not isinstance(proposal, dict) or set(proposal) != {"key", "versionId", "sha256"}
            or proposal.get("key") != f"proposals/staging/{record['sessionId']}/bootstrap.json"
            or not re.fullmatch(r"[a-f0-9]{64}", proposal.get("sha256", ""))):
        raise ValueError("Selected chart must remain bound to its immutable proposal")
    version(proposal["versionId"])
    value, _ = read_json(client, bucket, key, ref["versionId"])
    reuse = reuse_proposal(value)
    fields = {"schemaVersion", "sessionId", "executionVersion", "sourceRevisions", "releaseReference", "proposalReference", "baselineSha", "chartSha", "treeSha", "provenance"}
    if (set(value) != fields | ({"kind"} if reuse else set()) or checksum(value) != ref["sha256"] or type(value.get("schemaVersion")) is not int
            or not (value["schemaVersion"] == 1 and "kind" not in value or reuse)
            or value.get("sessionId") != record["sessionId"]
            or value.get("executionVersion") != execution_version or value.get("sourceRevisions") != execution["sourceRevisions"]
            or value.get("releaseReference") != record.get("bootstrapRelease") or value.get("proposalReference") != record.get("bootstrapProposal")
            or value.get("baselineSha") != execution["sourceRevisions"]["techx-chart"]):
        raise ValueError("Chart selection belongs to another release/execution")
    sha(value.get("chartSha"))
    sha(value.get("treeSha"))
    proof = value.get("provenance", {})
    if reuse:
        if (value["chartSha"] != value["baselineSha"]
                or set(proof) != {"reviewRun", "reviewer", "previewSha256", "ciRun"}
                or not re.fullmatch(rf"https://github.com/{re.escape(INFRA_REPO)}/actions/runs/[1-9][0-9]*", proof.get("reviewRun", ""))
                or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", proof.get("reviewer", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", proof.get("previewSha256", ""))
                or not re.fullmatch(rf"https://github.com/{re.escape(CHART_REPO)}/actions/runs/[1-9][0-9]*", proof.get("ciRun", ""))):
            raise ValueError("Reuse selection is missing exact baseline and human-reviewed preview provenance")
        return value
    number = proof.get("prNumber")
    if (value["chartSha"] == value["baselineSha"] or type(number) is not int or number < 1
            or proof.get("pullRequest") != f"https://github.com/{CHART_REPO}/pull/{number}"
            or not isinstance(proof.get("changedFiles"), list) or not 1 <= len(proof["changedFiles"]) <= 2
            or proof["changedFiles"] != sorted(set(proof["changedFiles"])) or set(proof["changedFiles"]) - ALLOWED_FILES
            or not re.fullmatch(rf"https://github.com/{re.escape(CHART_REPO)}/actions/runs/[1-9][0-9]*", proof.get("ciRun", ""))
            or not isinstance(proof.get("reviewers"), list) or not proof["reviewers"]
            or any(not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", user) for user in proof["reviewers"])):
        raise ValueError("Chart selection is missing reviewed CI provenance")
    sha(proof.get("prHeadSha"))
    return value
