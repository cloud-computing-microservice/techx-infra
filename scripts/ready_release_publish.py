"""Lease-bound ECR publish and immutable chart proposal for an active session."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess

from automation_budget import validate_estimate
from automation_session import utc, validate
from bootstrap_proposal import ChartProposal
from chart_selection import blob, sha
from cleanup_schedule import verify_existing
from plan_review_store import checksum, immutable_json, read_json
from ready_release_contract import (accepted_baseline, candidate_id, candidate_key, read_candidate, read_manifest,
    source_proof, validate_manifest)

ACCOUNT = "058114477594"
CONTEXT = ("sessionId", "account", "region", "environment", "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action")


class ReadyPublisher:
    def __init__(self, clients, store, bucket, execution, execution_version, source_sha, owner, *, github,
                 clock=None, run=subprocess.run):
        validate(execution)
        self.clients, self.store, self.bucket = clients, store, bucket
        self.execution, self.version, self.source_sha, self.owner = deepcopy(execution), execution_version, sha(source_sha), owner
        self.github, self.clock, self.run = github, clock or (lambda: datetime.now(timezone.utc)), run
        self.epoch = None

    def admission(self, record):
        validate(record)
        if (any(record.get(key) != self.execution.get(key) for key in CONTEXT) or record.get("executionVersion") != self.version
                or record["status"] != "READY" or record.get("pendingPlan") is not None
                or self.clock() + timedelta(minutes=30) >= utc(record["cleanupStartAt"])):
            raise ValueError("Active release requires the same accepted session and sufficient original window")
        identity = self.clients["sts"].get_caller_identity()
        proof = source_proof(self.github, self.source_sha, self.owner)
        if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
                f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-publisher/techx-publish-{proof['workflowRunId']}"):
            raise ValueError("Active release requires the scoped publisher OIDC identity")
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        validate_estimate(record, self.execution["costEstimate"])
        baseline = accepted_baseline(self.clients["s3"], self.bucket, record, self.version)
        if self.source_sha == baseline["platformSha"]: raise ValueError("Source is already the accepted release")
        comparison = self.github.get(f"compare/{baseline['platformSha']}...{self.source_sha}")
        if comparison.get("status") != "ahead" or comparison.get("merge_base_commit", {}).get("sha") != baseline["platformSha"]:
            raise ValueError("New release must descend from the accepted platform commit")
        release_id = candidate_id(record, self.version, self.source_sha, baseline)
        candidate = record.get("releaseCandidate")
        if candidate and candidate.get("id") != release_id:
            raise ValueError("Another release candidate must be resolved before publishing")
        if candidate and (candidate.get("promotionApprovalReference")
                or record.get("workflowHandoff", {}).get("phase", "CONSUMED") != "CONSUMED"):
            raise ValueError("An active review/grant already owns this candidate; do not republish")
        return proof, baseline, release_id

    def guard(self):
        record, etag = self.store.read()
        lease = record.get("lease", {})
        if (lease.get("owner") != self.owner or lease.get("operation") != "release" or lease.get("epoch") != self.epoch
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Active publisher lost the release lease")
        self.admission(record)
        return record, etag

    def checkpoint(self, **fields):
        current, etag = self.guard()
        if current.get("releaseCandidate", {}).get("id") != self.release_id:
            raise ValueError("Release candidate was superseded")
        updated = deepcopy(current)
        updated["releaseCandidate"].update(fields)
        self.store.write(updated, etag)

    def prepare(self):
        record, etag = self.guard()
        proof, baseline, self.release_id = self.admission(record)
        intent = dict(schemaVersion=1, kind="ready-release-intent", sessionId=record["sessionId"], executionVersion=self.version,
            releaseId=self.release_id, sourceSha=self.source_sha, baseline=baseline, createdAt=self.clock().isoformat(), sourceProof=proof)
        key = candidate_key(record["sessionId"], self.release_id, "intent")
        ref = immutable_json(self.clients["s3"], self.bucket, key, intent,
            equivalent=lambda existing, proposed: {key: value for key, value in existing.items() if key not in ("createdAt", "sourceProof")} ==
                {key: value for key, value in proposed.items() if key not in ("createdAt", "sourceProof")})
        stored, _ = read_json(self.clients["s3"], self.bucket, key, ref["versionId"])
        ref["sha256"] = checksum(stored)
        current, etag = self.guard()
        updated = deepcopy(current)
        if not updated.get("releaseCandidate"):
            updated["releaseCandidate"] = dict(id=self.release_id, sourceSha=self.source_sha, intentReference=ref, state="PUBLISHING")
        elif updated["releaseCandidate"].get("intentReference") != ref:
            raise ValueError("Existing candidate references another immutable intent")
        read_candidate(self.clients["s3"], self.bucket, updated, self.version)
        self.store.write(updated, etag)
        return stored

    def scan(self, manifest):
        result = {}
        for image in manifest["images"]:
            self.guard()
            client, name = self.clients["ecr"], "techx/" + image["component"]
            repositories = client.describe_repositories(repositoryNames=[name]).get("repositories", [])
            if len(repositories) != 1 or repositories[0].get("imageTagMutability") != "IMMUTABLE":
                raise ValueError("Active release repository must remain immutable")
            self.guard()
            rows = client.describe_images(repositoryName=name, imageIds=[dict(imageTag=manifest["tag"])]).get("imageDetails", [])
            if len(rows) != 1 or rows[0].get("imageDigest") != image["digest"]: raise ValueError("Published digest changed")
            self.guard()
            scan = client.describe_image_scan_findings(repositoryName=name, imageId=dict(imageDigest=image["digest"]))
            counts = scan.get("imageScanFindings", {}).get("findingSeverityCounts", {})
            if (scan.get("imageScanStatus", {}).get("status") != "COMPLETE" or not isinstance(scan.get("imageScanFindings"), dict)
                    or any(type(counts.get(key, 0)) is not int or counts.get(key, 0) != 0 for key in ("HIGH", "CRITICAL"))):
                raise ValueError("Published digest must have a completed zero HIGH/CRITICAL scan")
            result[image["component"]] = dict(status="COMPLETE", high=0, critical=0)
        return result

    def publish(self, platform, directory):
        self.prepare()
        record, _ = self.guard()
        path = Path(directory) / "release.json"
        self.run(["pwsh", "-NoProfile", "-File", str(platform / "scripts/publish-ecr-images.ps1"),
            "-ExpectedAccountId", ACCOUNT, "-ManifestPath", str(path)], cwd=platform, check=True)
        if path.stat().st_size > 64 * 1024: raise ValueError("Release manifest exceeds size policy")
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        spec = importlib.util.spec_from_file_location("active_manifest", platform / "scripts/prepare-chart-release.py")
        validator = importlib.util.module_from_spec(spec); spec.loader.exec_module(validator)
        validator.validate_manifest(manifest)
        validate_manifest(manifest, self.source_sha)
        if manifest["commit"] != self.source_sha: raise ValueError("Publisher manifest is for another source")
        scan = self.scan(manifest)
        current, _ = self.guard()
        artifact = dict(schemaVersion=1, kind="ready-release-manifest", intentReference=current["releaseCandidate"]["intentReference"], manifest=manifest, scan=scan)
        key = candidate_key(record["sessionId"], self.release_id, "manifest")
        self.guard()
        ref = immutable_json(self.clients["s3"], self.bucket, key, artifact)
        ref["sha256"] = checksum(artifact)
        # Do not regress a durable proposal on a publisher retry.
        state = "PROPOSED" if current["releaseCandidate"].get("proposalReference") else "PUBLISHED"
        self.checkpoint(state=state, manifestReference=ref)
        return read_manifest(self.clients["s3"], self.bucket, self.guard()[0], self.version)


class ReadyProposal(ChartProposal):
    def __init__(self, publisher, github):
        self.publisher, self.github = publisher, github
        record, _ = publisher.guard()
        self.intent = read_candidate(publisher.clients["s3"], publisher.bucket, record, publisher.version)
        self.candidate_id = self.intent["releaseId"]
        self.release_ref = deepcopy(record["releaseCandidate"]["manifestReference"])
        self.release = read_manifest(publisher.clients["s3"], publisher.bucket, record, publisher.version)
        self.baseline = self.intent["baseline"]["chartSha"]
        self.branch = f"release/{record['sessionId']}-{self.candidate_id[:24]}"
        self.commit_date = self.intent["createdAt"]

    def guard(self):
        record, etag = self.publisher.guard()
        pointer = record.get("releaseCandidate", {})
        if pointer.get("id") != self.candidate_id or pointer.get("manifestReference") != self.release_ref:
            raise ValueError("Active release changed while proposing chart")
        if self.github.get("git/ref/heads/main").get("object", {}).get("sha") != self.baseline:
            raise ValueError("Chart main changed after the accepted baseline; do not rebase automatically")
        return record, etag

    def values(self, source, baseline):
        return blob(self.github, baseline["values-staging.yaml"]["sha"])

    def message(self, record):
        return f"chore(staging): release {self.intent['sourceSha'][:12]} for {record['sessionId']}"

    def persist(self, tree_sha, head_sha, disposition):
        record, _ = self.guard()
        reuse = disposition == dict(kind="reuse")
        value = dict(schemaVersion=1, kind="ready-release-proposal", intentReference=record["releaseCandidate"]["intentReference"],
            manifestReference=self.release_ref, baselineSha=self.baseline, headSha=head_sha, treeSha=tree_sha, branch=self.branch,
            reuse=reuse, prNumber=disposition.get("prNumber"), pullRequest=disposition.get("pullRequest"))
        key = candidate_key(record["sessionId"], self.candidate_id, "proposal")
        self.guard()
        ref = immutable_json(self.publisher.clients["s3"], self.publisher.bucket, key, value)
        ref["sha256"] = checksum(value)
        self.guard()
        self.publisher.checkpoint(state="PROPOSED", proposalReference=ref)
        return dict(reference=ref, **disposition)
