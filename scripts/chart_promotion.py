"""Read-only promotion policy for an exact cold-release proposal.

This module does not grant merge authority, acquire a lease or mutate GitHub.
Both a future review preview and the owned private promotion worker must call
it: a previously green check/preview never authorizes a later merge by itself.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re

from automation_budget import validate_estimate
from automation_session import utc, validate
from chart_selection import (ALLOWED_FILES, CHART_REPO, INFRA_REPO, git_tree,
                             human_approvals, read_proposal, read_release,
                             release_tree, reuse_proposal, sha)
from cleanup_schedule import verify_existing
from plan_approval import version
from plan_review_store import checksum
from promotion_authority import admitted

LIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


class PromotionPending(ValueError):
    """A real CI/review prerequisite is still pending; never treat it as green."""


def protection(github, app_id, app_slug):
    """Require the future promoter to be the only allowed main push actor."""
    if (type(app_id) is not int or app_id < 1 or not isinstance(app_slug, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", app_slug)):
        raise ValueError("An actual dedicated promotion App ID and slug are required")
    policy = github.get("branches/main/protection")
    reviews = policy.get("required_pull_request_reviews", {})
    checks = policy.get("required_status_checks", {})
    actors = policy.get("restrictions") or {}
    bypass = reviews.get("bypass_pull_request_allowances") or {}
    required = checks.get("checks", [])
    if (policy.get("enforce_admins", {}).get("enabled") is not True
            or policy.get("allow_force_pushes", {}).get("enabled") is not False
            or policy.get("allow_deletions", {}).get("enabled") is not False
            or policy.get("required_conversation_resolution", {}).get("enabled") is not True
            or checks.get("strict") is not True or not isinstance(required, list)
            or not any(row.get("context") == "manifests" and row.get("app_id") == 15368 for row in required)
            or type(reviews.get("required_approving_review_count")) is not int or reviews["required_approving_review_count"] < 1
            or reviews.get("dismiss_stale_reviews") is not True or reviews.get("require_last_push_approval") is not True
            or any(bypass.get(key, []) != [] for key in ("users", "teams", "apps"))
            or actors.get("users") != [] or actors.get("teams") != []
            or not isinstance(actors.get("apps"), list) or len(actors["apps"]) != 1
            or actors["apps"][0].get("id") != app_id or actors["apps"][0].get("slug") != app_slug):
        raise ValueError("Protected main must require reviewed CI and restrict promotion to the dedicated App without bypass")
    return dict(appId=app_id, appSlug=app_slug, policySha256=checksum(policy))


def pull_identity(github, proposal):
    pull = github.get(f"pulls/{proposal['prNumber']}")
    if (pull.get("number") != proposal["prNumber"] or pull.get("html_url") != proposal["pullRequest"]
            or pull.get("state") != "open" or pull.get("merged") is not False or pull.get("draft") is not False
            or pull.get("base", {}).get("ref") != "main" or pull["base"].get("sha") != proposal["baselineSha"]
            or pull.get("head", {}).get("ref") != proposal["branch"] or pull["head"].get("sha") != proposal["headSha"]
            or any(pull.get(side, {}).get("repo", {}).get("full_name") != CHART_REPO for side in ("head", "base"))):
        raise ValueError("Promotion requires the exact open same-repository proposal head and admitted main base")
    files = github.get(f"pulls/{proposal['prNumber']}/files?per_page=100")
    if (not isinstance(files, list) or not 1 <= len(files) <= 2 or type(pull.get("changed_files")) is not int
            or pull["changed_files"] != len(files) or len({row.get("filename") for row in files}) != len(files)
            or {row.get("filename") for row in files} - ALLOWED_FILES
            or any(row.get("status") not in ("added", "modified") for row in files)):
        raise ValueError("Promotion PR must change only one or two distinct release files")
    if pull.get("mergeable") is None or pull.get("mergeable_state") == "unknown":
        raise PromotionPending("GitHub has not finished computing mergeability")
    if pull.get("mergeable") is not True or pull.get("mergeable_state") != "clean":
        # 'blocked' may mean a required human review has not happened yet.
        # It remains a wait, never authority to bypass branch protection.
        if pull.get("mergeable") is True and pull.get("mergeable_state") == "blocked":
            raise PromotionPending("PR review or a required check is still blocking merge")
        raise ValueError("Promotion PR is conflicted, behind or otherwise not clean")
    return pull, sorted(row["filename"] for row in files)


def candidate_ci(github, proposal, execution):
    result = github.get(f"actions/workflows/verify.yml/runs?head_sha={proposal['headSha']}&per_page=100")
    rows = result.get("workflow_runs", [])
    if (not isinstance(rows, list) or len(rows) > 100 or type(result.get("total_count")) is not int
            or result["total_count"] != len(rows)):
        raise ValueError("Complete bounded candidate CI history is required")
    valid = [row for row in rows if row.get("head_sha") == proposal["headSha"]
        and row.get("head_branch") == proposal["branch"] and row.get("event") == "pull_request"
        and row.get("path") in (".github/workflows/verify.yml", ".github/workflows/verify.yml@main")
        and all(row.get(key, {}).get("full_name") == CHART_REPO for key in ("repository", "head_repository"))]
    if not valid: raise PromotionPending("Exact candidate PR CI has not been observed")
    if any(type(row.get("id")) is not int or row["id"] < 1 for row in valid):
        raise ValueError("Candidate CI has an invalid run identity")
    chosen = max(valid, key=lambda row: row["id"])
    run = github.get(f"actions/runs/{chosen['id']}")
    if (any(run.get(key) != chosen.get(key) for key in ("id", "head_sha", "head_branch", "event", "path", "repository", "head_repository"))
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            or not utc(execution["createdAt"]) <= utc(run["created_at"]) < utc(execution["cleanupStartAt"])):
        raise ValueError("Actual candidate CI run differs from the bounded history or session window")
    if run.get("status") in LIVE: raise PromotionPending("Latest exact candidate CI is still running")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValueError("Latest exact candidate CI failed; never fall back to an older green run")
    return dict(runId=run["id"], runAttempt=run["run_attempt"],
                runUrl=f"https://github.com/{CHART_REPO}/actions/runs/{run['id']}")


class ColdPromotionChecks:
    def __init__(self, clients, store, bucket, execution, execution_version, *, clock=None):
        validate(execution)
        version(execution_version)
        self.clients, self.store, self.bucket = clients, store, bucket
        self.execution, self.version = deepcopy(execution), execution_version
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def guard(self, *, owner=None, epoch=None):
        record, _ = self.store.read()
        validate(record)
        for key in ("sessionId", "account", "region", "environment", "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"):
            if record.get(key) != self.execution.get(key): raise ValueError("Promotion scope differs from immutable execution")
        if (record.get("executionVersion") != self.version or record["status"] != "PROVISIONING"
                or record.get("pendingPlan") is not None or record.get("bootstrapChart") is not None
                or record.get("stages", {}).get("foundation", {}).get("state") != "APPLIED"
                or record.get("stages", {}).get("gitops") or record.get("stages", {}).get("edge")
                or self.clock() >= utc(record["cleanupStartAt"])):
            raise ValueError("Promotion requires the current pre-GitOps cold session before cleanup")
        lease = record.get("lease")
        if owner is None:
            if lease: raise ValueError("Promotion preview requires an idle session")
        elif (not isinstance(owner, str) or not re.fullmatch(r"arn:aws:ec2:us-east-1:058114477594:instance/i-[a-f0-9]{8,17}", owner)
                or type(epoch) is not int or epoch < 1 or not isinstance(lease, dict)
                or lease.get("owner") != owner or lease.get("operation") != "deploy" or lease.get("epoch") != epoch
                or utc(lease.get("expiresAt", "1970-01-01T00:00:00Z")) <= self.clock()):
            raise ValueError("Promotion requires the private executor's exact live deploy lease")
        validate_estimate(record, self.execution["costEstimate"])
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        return record

    def evaluate(self, source, github, *, app_id, app_slug, owner=None, epoch=None):
        record = self.guard(owner=owner, epoch=epoch)
        release = read_release(self.clients["s3"], self.bucket, record, self.execution, self.version)
        proposal = read_proposal(self.clients["s3"], self.bucket, record, self.execution, self.version)
        if reuse_proposal(proposal): raise ValueError("Zero-diff baseline reuse has its own human review, not a merge")
        policy = protection(github, app_id, app_slug)
        expected = admitted(self.execution)
        if policy != {key: expected[key] for key in ("appId", "appSlug", "policySha256")}:
            raise ValueError("Promotion App or branch policy differs from the human-reviewed opening authority")
        pull, files = pull_identity(github, proposal)
        tree = release_tree(github, source, self.execution, release, proposal["headSha"], candidate=True)
        commit = github.get(f"git/commits/{proposal['headSha']}")
        if ([row.get("sha") for row in commit.get("parents", [])] != [proposal["baselineSha"]]
                or tree["treeSha"] != proposal["treeSha"] or tree["changedFiles"] != files):
            raise ValueError("Promotion candidate must retain the exact admitted parent, full tree and PR file diff")
        reviewers = human_approvals(pull, github.get(f"pulls/{proposal['prNumber']}/reviews?per_page=100"))
        ci = candidate_ci(github, proposal, self.execution)
        # External reads can take time. Repeat the live boundaries after all
        # verification, then compare each result so a mid-check race fails shut.
        if protection(github, app_id, app_slug) != policy or pull_identity(github, proposal) != (pull, files):
            raise ValueError("Promotion branch policy or PR changed during verification")
        if human_approvals(pull, github.get(f"pulls/{proposal['prNumber']}/reviews?per_page=100")) != reviewers:
            raise ValueError("Promotion human review changed during verification")
        if candidate_ci(github, proposal, self.execution) != ci:
            raise ValueError("Promotion candidate CI changed during verification")
        if github.get("git/ref/heads/main").get("object", {}).get("sha") != proposal["baselineSha"]:
            raise ValueError("Promotion main moved from the admitted baseline")
        current = self.guard(owner=owner, epoch=epoch)
        if any(current.get(key) != record.get(key) for key in ("bootstrapRelease", "bootstrapProposal")):
            raise ValueError("Promotion release/proposal was superseded during verification")
        return dict(schemaVersion=1, kind="cold-promotion", sessionId=record["sessionId"], executionVersion=self.version,
            sourceRevisions=self.execution["sourceRevisions"], releaseReference=record["bootstrapRelease"],
            proposalReference=record["bootstrapProposal"], baselineSha=proposal["baselineSha"], headSha=proposal["headSha"],
            treeSha=proposal["treeSha"], prNumber=proposal["prNumber"], pullRequest=proposal["pullRequest"],
            changedFiles=files, reviewers=reviewers, candidateCi=ci, branchPolicy=policy, manifest=release["manifest"],
            approvedBudgetUsd=record["approvedBudgetUsd"], deadline=record["deadline"], cleanupStartAt=record["cleanupStartAt"])
