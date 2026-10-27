"""Read-only active promotion checks with an explicit immutable release context."""
from copy import deepcopy
from datetime import timedelta
import importlib.util
import json
import re

from automation_budget import validate_estimate
from automation_session import utc, validate
from chart_promotion import ColdPromotionChecks, LIVE, PromotionPending
from chart_selection import ALLOWED_FILES, blob, git_tree
from cleanup_schedule import verify_existing
from ready_release_contract import (VERIFY, accepted_baseline, read_candidate, read_manifest, read_proposal)
from ready_release_publish import CONTEXT


def completed_source(github, intent, record):
    """The immutable intent's real source CI must remain verifiable after publish."""
    from chart_selection import PLATFORM_REPO
    proof = intent["sourceProof"]
    run_id, source_sha = proof["workflowRunId"], intent["sourceSha"]
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != run_id or run.get("event") not in ("push", "workflow_dispatch")
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != source_sha or run.get("path") not in (VERIFY, VERIFY + "@main")
            or any(run.get(key, {}).get("full_name") != PLATFORM_REPO for key in ("repository", "head_repository"))
            or not utc(record["createdAt"]) <= utc(run.get("created_at", "")) <= utc(intent["createdAt"])):
        raise ValueError("Active promotion requires the intent's actual completed source run")
    if run.get("status") in LIVE: raise PromotionPending("The candidate's source publisher is still running")
    if run.get("status") != "completed": raise ValueError("Unknown active source run state")
    response = github.get(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = response.get("jobs")
    if (not isinstance(jobs, list) or len(jobs) > 100 or type(response.get("total_count")) is not int
            or response["total_count"] != len(jobs)
            or any(row.get("run_id") != run_id or row.get("head_sha") != source_sha for row in jobs)):
        raise ValueError("Complete jobs for the intent's source run are required")
    ci = [row for row in jobs if row.get("id") == proof["ciJobId"]]
    publisher = [row for row in jobs if row.get("id") == proof["publisherJobId"]]
    if (len(ci) != 1 or ci[0].get("name") != "acceptance / acceptance" or ci[0].get("status") != "completed"
            or ci[0].get("conclusion") != "success" or len(publisher) != 1 or publisher[0].get("name") != "release / publish"
            or publisher[0].get("status") != "completed"):
        raise ValueError("Original application CI has not passed or publisher is still running")
    # A lost response may have failed the first publisher after a valid intent.
    # Producer-only immutable receipts can be recovered by a later trusted run;
    # the originating application's successful CI must still be the exact one.
    if github.get("git/ref/heads/main").get("object", {}).get("sha") != source_sha:
        raise ValueError("A superseded platform source cannot be promoted")


class ReadyPromotionChecks(ColdPromotionChecks):
    kind = "ready-promotion"

    def __init__(self, *args, platform, testing_task=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.platform = platform
        if testing_task is not None and not re.fullmatch(r"[a-f0-9]{32}", testing_task): raise ValueError("Exact private rollout task required")
        self.testing_task = testing_task

    def guard(self, *, owner=None, epoch=None):
        record, _ = self.store.read()
        validate(record)
        testing = self.testing_task is not None and owner is not None and record["status"] == "TESTING"
        if (any(record.get(key) != self.execution.get(key) for key in CONTEXT) or record.get("executionVersion") != self.version
                or not (record["status"] == "READY" or testing) or record.get("pendingPlan") is not None
                or record.get("releaseCandidate", {}).get("state") != "PROPOSED"
                or self.clock() + timedelta(minutes=3) >= utc(record["cleanupStartAt"])):
            raise ValueError("Active promotion requires the proposed candidate in its original READY session/window")
        if testing:
            from deployment_tasks import read_task
            task = read_task(self.clients["s3"], self.bucket, record, self.testing_task, self.clock())
            if (task["mode"], task["stage"], task["attempt"]) != ("release", "active", 1) or record["deploymentTask"]["state"] != "RUNNING":
                raise ValueError("Promotion is not this claimed private rollout task")
        lease = record.get("lease")
        if owner is None:
            if lease: raise ValueError("Active promotion preview requires an idle session")
        elif (not isinstance(owner, str) or not re.fullmatch(r"arn:aws:ec2:us-east-1:058114477594:instance/i-[a-f0-9]{8,17}", owner)
                or type(epoch) is not int or epoch < 1 or not isinstance(lease, dict)
                or lease.get("owner") != owner or lease.get("operation") != "release" or lease.get("epoch") != epoch
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Active promotion requires the private executor's live release lease")
        validate_estimate(record, self.execution["costEstimate"])
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        accepted_baseline(self.clients["s3"], self.bucket, record, self.version)
        intent = read_candidate(self.clients["s3"], self.bucket, record, self.version)
        completed_source(self.platform, intent, record)
        return record

    def artifacts(self, record):
        release = read_manifest(self.clients["s3"], self.bucket, record, self.version)
        proposal = read_proposal(self.clients["s3"], self.bucket, record, self.version)
        if proposal["reuse"]: raise ValueError("A new active source must have its own nonempty reviewed release PR")
        return release, proposal

    def snapshot(self, record):
        return dict(candidate=deepcopy(record.get("releaseCandidate")), baseline=deepcopy(record.get("acceptedRelease")))

    def request_context(self, record):
        pointer = record["releaseCandidate"]
        return dict(kind="ready-promotion", releaseId=pointer["id"], intentReference=pointer["intentReference"],
            releaseReference=pointer["manifestReference"], proposalReference=pointer["proposalReference"],
            acceptedBaseline=deepcopy(record["acceptedRelease"]))

    def tree(self, source, github, record, release, proposal):
        # Use the actual accepted chart baseline explicitly. Original execution
        # sourceRevisions and cold acceptance receipts are never rewritten.
        _, baseline = git_tree(github, proposal["baselineSha"])
        tree_sha, chosen = git_tree(github, proposal["headSha"])
        changed = {name for name in set(baseline) | set(chosen) if baseline.get(name) != chosen.get(name)}
        if changed - ALLOWED_FILES or not ALLOWED_FILES <= set(chosen):
            raise ValueError("Active release may change only its two release files")
        if any(chosen[name]["mode"] != baseline.get(name, {"mode": "100644"})["mode"] for name in ALLOWED_FILES):
            raise ValueError("Active release cannot change file permissions")
        values = blob(github, baseline["values-staging.yaml"]["sha"])
        spec = importlib.util.spec_from_file_location("ready_selected_manifest", source.resolve() / "techx-platform/scripts/prepare-chart-release.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        module.validate_manifest(release["manifest"])
        expected = module.render_values(values.decode("utf-8-sig"), release["manifest"]).replace("\r\n", "\n")
        if (blob(github, chosen["values-staging.yaml"]["sha"]).decode("utf-8-sig").replace("\r\n", "\n") != expected
                or json.loads(blob(github, chosen["releases/staging.json"]["sha"]).decode("utf-8-sig")) != release["manifest"]):
            raise ValueError("Active chart does not select the immutable candidate artifacts")
        return dict(treeSha=tree_sha, changedFiles=sorted(changed))
