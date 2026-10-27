"""Protected active-review handoff and grant consumption; no workload mutation."""
from copy import deepcopy
import re

from automation_session import utc
from chart_promotion import candidate_ci, pull_identity
from chart_selection import INFRA_REPO, human_approvals
from plan_approval import version
from plan_review_store import read_json
from ready_chart_promotion import completed_source
from ready_release_contract import accepted_baseline, read_candidate, read_manifest, read_proposal
from review_chart_promotion import ENVIRONMENT, WORKFLOW, promotion_index_key, read_promotion_approval, verified_reviewer
from workflow_handoff import target_for


class ReadyReleaseHandoff:
    def __init__(self, dispatcher, chart, platform, infra):
        self.d, self.chart, self.platform, self.infra = dispatcher, chart, platform, infra

    def artifacts(self, record):
        d = self.d
        if record.get("status") != "READY" or record.get("releaseCandidate", {}).get("state") != "PROPOSED":
            raise ValueError("Active review requires the fully proposed READY candidate")
        accepted_baseline(d.clients["s3"], d.bucket, record, d.version)
        intent = read_candidate(d.clients["s3"], d.bucket, record, d.version)
        manifest = read_manifest(d.clients["s3"], d.bucket, record, d.version)
        proposal = read_proposal(d.clients["s3"], d.bucket, record, d.version)
        if proposal["reuse"]: raise ValueError("Active source needs its own reviewable changed-chart proposal")
        completed_source(self.platform, intent, record)
        return intent, manifest, proposal

    def approval(self, record, ref):
        d = self.d
        intent, manifest, proposal = self.artifacts(record)
        value = read_promotion_approval(d.clients["s3"], d.bucket, d.execution, d.version, ref, d.clock(), kind="ready-promotion")
        request, pointer = value["request"], record["releaseCandidate"]
        expected = dict(releaseId=intent["releaseId"], intentReference=pointer["intentReference"],
            releaseReference=pointer["manifestReference"], proposalReference=pointer["proposalReference"],
            acceptedBaseline=record["acceptedRelease"], manifest=manifest["manifest"],
            baselineSha=proposal["baselineSha"], headSha=proposal["headSha"], treeSha=proposal["treeSha"],
            prNumber=proposal["prNumber"], pullRequest=proposal["pullRequest"])
        if any(request.get(key) != item for key, item in expected.items()):
            raise ValueError("Active approval belongs to another candidate, baseline or artifact")
        pull, files = pull_identity(self.chart, proposal)
        reviewers = human_approvals(pull, self.chart.get(f"pulls/{proposal['prNumber']}/reviews?per_page=100"))
        if (request.get("changedFiles") != files or request.get("candidateCi") != candidate_ci(self.chart, proposal, d.execution)
                or request.get("reviewers") != reviewers):
            raise ValueError("Active candidate CI/file diff changed after human review")
        url = request.get("workflowRun", "")
        if not re.fullmatch(rf"https://github.com/{INFRA_REPO}/actions/runs/[1-9][0-9]*", url):
            raise ValueError("Active grant lacks a real protected review run")
        number = int(url.rsplit("/", 1)[1])
        run = self.infra.get(f"actions/runs/{number}")
        approved = utc(value["approvedAt"])
        if (run.get("id") != number or run.get("status") != "completed" or run.get("conclusion") != "success"
                or run.get("event") != "workflow_dispatch" or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
                or run.get("head_branch") != "main" or run.get("head_sha") != d.execution["sourceRevisions"]["techx-infra"]
                or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
                or any(run.get(key, {}).get("full_name") != INFRA_REPO for key in ("repository", "head_repository"))
                or not utc(record["createdAt"]) <= utc(run["created_at"]) <= approved
                or not approved.replace(microsecond=0) <= utc(run["updated_at"]) <= d.clock()
                or verified_reviewer(self.infra, number, environment=ENVIRONMENT) != value["reviewer"]):
            raise ValueError("Active grant lacks its successful review run and actual human approval")
        return value

    def target(self, record):
        if record.get("lease"): return dict(waiting="ACTIVE_RELEASE_BUSY")
        _, _, proposal = self.artifacts(record)
        ref = record["releaseCandidate"].get("promotionApprovalReference")
        if ref:
            self.approval(record, ref)
            return dict(waiting="ACTIVE_PRIVATE_PROMOTION", releaseId=record["releaseCandidate"]["id"])
        pull_identity(self.chart, proposal)
        candidate_ci(self.chart, proposal, self.d.execution)
        return target_for("review-promotion", record, self.d.execution)

    def consume(self, record, intent, run, source_run):
        d = self.d
        index, object_version = read_json(d.clients["s3"], d.bucket, promotion_index_key(record["sessionId"], run["id"]))
        version(object_version)
        expected = dict(schemaVersion=1, kind="ready-promotion-index", sessionId=record["sessionId"],
            executionVersion=d.version, sourceRevisions=d.execution["sourceRevisions"], dispatchId=intent["dispatchId"], workflowRun=source_run)
        if (not isinstance(index, dict) or set(index) != set(expected) | {"approvalReference"}
                or any(index.get(key) != item for key, item in expected.items())):
            raise ValueError("Completed active review has no matching protected grant index")
        ref = index["approvalReference"]
        if d.advance().get("waiting") != "READY":
            raise ValueError("Active review cannot consume a grant before the previous private manager is terminal")
        for attempt in range(4):
            current, etag = d.current(allow_ready=True)
            predecessor = intent["predecessor"]
            candidate = current.get("releaseCandidate", {})
            original = deepcopy(candidate)
            original.pop("promotionApprovalReference", None)
            if (predecessor.get("status") != "READY" or original != predecessor.get("releaseCandidate")
                    or current.get("acceptedRelease") != predecessor.get("acceptedRelease")
                    or current.get("lifecycleDispatch") != predecessor.get("lifecycleDispatch")
                    or current.get("workflowHandoff", {}).get("id") != intent["dispatchId"]
                    or candidate.get("promotionApprovalReference") not in (None, ref)):
                raise ValueError("Active review predecessor was superseded before grant consumption")
            grant = self.approval(current, ref)
            if grant["request"]["workflowRun"] != source_run:
                raise ValueError("Active grant came from another review run")
            latest, latest_etag = d.current(allow_ready=True)
            if latest != current or latest_etag != etag: continue
            if candidate.get("promotionApprovalReference") == ref: return
            updated = deepcopy(current)
            updated["releaseCandidate"]["promotionApprovalReference"] = deepcopy(ref)
            try:
                d.store.write(updated, etag)
                return
            except Exception:
                if attempt == 3: raise
        raise ValueError("Active review lost its exact CAS predecessor")
