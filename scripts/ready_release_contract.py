"""Immutable candidate contract for releases within an already accepted session."""
from copy import deepcopy
import re

from automation_session import utc
from chart_selection import CHART_REPO, PLATFORM_REPO, sha
from plan_approval import version
from plan_review_store import checksum, read_json

VERIFY = ".github/workflows/verify.yml"


def validate_manifest(manifest, source_sha):
    sha(source_sha)
    if (not isinstance(manifest, dict) or set(manifest) != {"commit", "tag", "images"} or manifest.get("commit") != source_sha
            or manifest.get("tag") != "staging-" + source_sha[:12] or not isinstance(manifest.get("images"), list) or len(manifest["images"]) != 3):
        raise ValueError("Active manifest must identify the exact source and three artifacts")
    seen = set()
    for image in manifest["images"]:
        name = image.get("component") if isinstance(image, dict) else None
        if (not isinstance(image, dict) or set(image) != {"component", "image", "digest"} or name not in ("frontend", "catalog", "order")
                or name in seen or image.get("image") != f"058114477594.dkr.ecr.us-east-1.amazonaws.com/techx/{name}:staging-{source_sha[:12]}"
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("digest", ""))):
            raise ValueError("Active manifest repository/tag/digest contract changed")
        seen.add(name)
    return manifest


def candidate_id(record, execution_version, source_sha, baseline):
    return checksum(dict(sessionId=record["sessionId"], executionVersion=execution_version, sourceSha=sha(source_sha), baseline=baseline))


def candidate_key(session, release_id, filename):
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session) or not re.fullmatch(r"[a-f0-9]{64}", release_id):
        raise ValueError("Exact admitted session/release identity required")
    prefix = {"intent": "releases", "manifest": "releases", "proposal": "proposals"}.get(filename)
    if prefix is None: raise ValueError("Unsupported release artifact")
    return f"{prefix}/staging/{session}/active/{release_id}/{filename}.json"


def read_reference(client, bucket, ref, key):
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"} or ref.get("key") != key
            or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))):
        raise ValueError("Release artifact requires the exact immutable reference")
    version(ref["versionId"])
    value, _ = read_json(client, bucket, key, ref["versionId"])
    if checksum(value) != ref["sha256"]: raise ValueError("Release artifact checksum mismatch")
    return value


def accepted_baseline(client, bucket, record, execution_version):
    # Cold READY evidence remains immutable. Subsequent accepted-release
    # receipts will have their own reader; never rewrite original source SHAs
    # or reinterpret a candidate as an accepted release.
    from private_acceptance_contract import ready_release
    accepted = ready_release(client, bucket, record, execution_version)
    if accepted != record.get("acceptedRelease"): raise ValueError("Current baseline is not the accepted private release")
    return deepcopy(accepted)


def source_proof(github, source_sha, owner):
    sha(source_sha)
    if not re.fullmatch(re.escape(PLATFORM_REPO) + r":[1-9][0-9]*:1", owner):
        raise ValueError("Release publisher requires a first-attempt trusted platform run")
    run_id = int(owner.split(":")[1])
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != run_id or run.get("status") != "in_progress" or run.get("event") not in ("push", "workflow_dispatch")
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != source_sha or run.get("path") not in (VERIFY, VERIFY + "@main")
            or any(run.get(key, {}).get("full_name") != PLATFORM_REPO for key in ("repository", "head_repository"))):
        raise ValueError("Active release source must be the actual trusted platform main run")
    response = github.get(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    rows = response.get("jobs")
    if (not isinstance(rows, list) or len(rows) > 100 or type(response.get("total_count")) is not int
            or response["total_count"] != len(rows) or any(row.get("run_id") != run_id or row.get("head_sha") != source_sha for row in rows)):
        raise ValueError("Complete jobs of the same source run are required")
    ci = [row for row in rows if row.get("name") == "acceptance / acceptance"]
    publisher = [row for row in rows if row.get("name") == "release / publish"]
    if (len(ci) != 1 or ci[0].get("status") != "completed" or ci[0].get("conclusion") != "success"
            or len(publisher) != 1 or publisher[0].get("status") != "in_progress"
            or any(type(row.get("id")) is not int or row["id"] < 1 for row in ci + publisher)):
        raise ValueError("The same run's application acceptance must pass before publishing")
    if github.get("git/ref/heads/main").get("object", {}).get("sha") != source_sha:
        raise ValueError("Superseded platform commits cannot publish an active release")
    return dict(repository=PLATFORM_REPO, sourceSha=source_sha, workflowRunId=run_id, ciJobId=ci[0]["id"], publisherJobId=publisher[0]["id"])


def read_candidate(client, bucket, record, execution_version):
    pointer = record.get("releaseCandidate", {})
    release_id = pointer.get("id", "")
    intent = read_reference(client, bucket, pointer.get("intentReference"), candidate_key(record["sessionId"], release_id, "intent"))
    if (not isinstance(intent, dict) or set(intent) != {"schemaVersion", "kind", "sessionId", "executionVersion", "releaseId", "sourceSha", "baseline", "createdAt", "sourceProof"}
            or intent.get("schemaVersion") != 1 or intent.get("kind") != "ready-release-intent"
            or intent.get("sessionId") != record["sessionId"] or intent.get("executionVersion") != execution_version
            or intent.get("releaseId") != release_id or intent.get("sourceSha") != pointer.get("sourceSha")
            or candidate_id(record, execution_version, intent["sourceSha"], intent.get("baseline")) != release_id
            or intent.get("baseline") != record.get("acceptedRelease")
            or not utc(record["createdAt"]) <= utc(intent.get("createdAt", "")) < utc(record["cleanupStartAt"])):
        raise ValueError("Release candidate belongs to a different execution/baseline")
    proof = intent["sourceProof"]
    if (not isinstance(proof, dict) or set(proof) != {"repository", "sourceSha", "workflowRunId", "ciJobId", "publisherJobId"}
            or proof.get("repository") != PLATFORM_REPO or proof.get("sourceSha") != intent["sourceSha"]
            or any(type(proof.get(key)) is not int or proof[key] < 1 for key in ("workflowRunId", "ciJobId", "publisherJobId"))):
        raise ValueError("Candidate lacks exact trusted CI provenance")
    return intent


def read_manifest(client, bucket, record, execution_version):
    intent = read_candidate(client, bucket, record, execution_version)
    pointer = record["releaseCandidate"]
    value = read_reference(client, bucket, pointer.get("manifestReference"), candidate_key(record["sessionId"], intent["releaseId"], "manifest"))
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "kind", "intentReference", "manifest", "scan"}
            or value.get("schemaVersion") != 1 or value.get("kind") != "ready-release-manifest"
            or value.get("intentReference") != pointer["intentReference"] or value.get("manifest", {}).get("commit") != intent["sourceSha"]
            or set(value.get("scan", {})) != {"frontend", "catalog", "order"}
            or any(item != dict(status="COMPLETE", high=0, critical=0) for item in value["scan"].values())):
        raise ValueError("Candidate manifest/scan differs from its immutable intent")
    validate_manifest(value["manifest"], intent["sourceSha"])
    return value


def read_proposal(client, bucket, record, execution_version):
    intent = read_candidate(client, bucket, record, execution_version)
    pointer = record["releaseCandidate"]
    value = read_reference(client, bucket, pointer.get("proposalReference"), candidate_key(record["sessionId"], intent["releaseId"], "proposal"))
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "kind", "intentReference", "manifestReference", "baselineSha", "headSha", "treeSha", "branch", "reuse", "prNumber", "pullRequest"}
            or value.get("kind") != "ready-release-proposal" or value.get("schemaVersion") != 1 or value.get("intentReference") != pointer["intentReference"]
            or value.get("manifestReference") != pointer.get("manifestReference") or value.get("baselineSha") != intent["baseline"]["chartSha"]
            or value.get("branch") != f"release/{record['sessionId']}-{intent['releaseId'][:24]}"):
        raise ValueError("Release proposal does not identify the exact active candidate")
    sha(value.get("headSha")); sha(value.get("treeSha"))
    if value.get("reuse") is True:
        if value.get("headSha") != value["baselineSha"] or value.get("prNumber") is not None or value.get("pullRequest") is not None:
            raise ValueError("Zero-diff active release cannot inherit an older PR")
    elif (value.get("reuse") is not False or type(value.get("prNumber")) is not int or value["prNumber"] < 1
            or value.get("pullRequest") != f"https://github.com/{CHART_REPO}/pull/{value['prNumber']}"):
        raise ValueError("Active release proposal requires its exact reviewable PR")
    return value
