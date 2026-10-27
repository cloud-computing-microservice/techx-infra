"""Bind a chart notification to the current immutable release proposal.

A notification is only a wakeup hint. Candidate CI, human review, promotion
grants and selected-release provenance are independently checked by their owners.
"""
from automation_session import utc
from chart_selection import CHART_REPO, read_proposal, reuse_proposal, sha

VERIFY = ".github/workflows/verify.yml"
REVIEW = ".github/workflows/chart-review-event.yml"


def bound_chart_run(s3, bucket, record, execution, execution_version, chart, run_id):
    if record.get("status") == "READY":
        if record.get("releaseCandidate", {}).get("state") != "PROPOSED": return False
        from ready_release_contract import accepted_baseline, read_candidate, read_manifest, read_proposal as active_proposal
        accepted_baseline(s3, bucket, record, execution_version)
        intent = read_candidate(s3, bucket, record, execution_version)
        read_manifest(s3, bucket, record, execution_version)
        proposal = active_proposal(s3, bucket, record, execution_version)
        if proposal["reuse"]: return False
        created_at = intent["createdAt"]
    elif record.get("status") == "PROVISIONING" and record.get("bootstrapProposal"):
        proposal = read_proposal(s3, bucket, record, execution, execution_version)
        created_at = execution["createdAt"]
    else: return False
    if reuse_proposal(proposal): return False  # Its protected review already sends a bound callback.
    run = chart.get(f"actions/runs/{run_id}")
    path, event = run.get("path"), run.get("event")
    if (run.get("id") != run_id or run.get("status") != "completed"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            or path not in (VERIFY, VERIFY + "@main", REVIEW, REVIEW + "@main")
            or any(run.get(key, {}).get("full_name") != CHART_REPO for key in ("repository", "head_repository"))
            or not utc(created_at) <= utc(run["created_at"]) < utc(execution["cleanupStartAt"])):
        return False
    pull = chart.get(f"pulls/{proposal['prNumber']}")
    if (pull.get("number") != proposal["prNumber"] or pull.get("html_url") != proposal["pullRequest"]
            or pull.get("head", {}).get("sha") != proposal["headSha"]
            or pull["head"].get("ref") != proposal["branch"] or pull.get("base", {}).get("ref") != "main"
            or any(pull.get(key, {}).get("repo", {}).get("full_name") != CHART_REPO for key in ("head", "base"))):
        return False
    if event in ("push", "workflow_dispatch"):
        return (path in (VERIFY, VERIFY + "@main") and run.get("head_branch") == "main"
                and pull.get("merged") is True and pull.get("state") == "closed"
                and sha(pull.get("merge_commit_sha")) == run.get("head_sha"))
    if (event not in ("pull_request", "pull_request_review")
            or (event == "pull_request" and path not in (VERIFY, VERIFY + "@main"))
            or (event == "pull_request_review" and path not in (REVIEW, REVIEW + "@main"))
            or pull.get("merged") is not False or pull.get("state") != "open"
            or pull["base"].get("sha") != proposal["baselineSha"] or run.get("head_branch") != proposal["branch"]):
        return False
    associated = run.get("pull_requests")
    # GitHub can return [] for review runs. The actual run must still bind the
    # exact same-repository release branch and current head/synthetic SHA. When
    # associations are present they must be complete and match this proposal.
    if not isinstance(associated, list) or len(associated) > 1: return False
    if associated and (associated[0].get("number") != proposal["prNumber"]
            or associated[0].get("head", {}).get("sha") != proposal["headSha"]
            or associated[0].get("base", {}).get("sha") != proposal["baselineSha"]
            or associated[0]["base"].get("ref") != "main"):
        return False
    # Review events use refs/pull/N/merge. Permit only the actual current
    # synthetic merge or exact proposal head, never an unrelated run SHA.
    if run.get("head_sha") == proposal["headSha"]: return True
    if pull.get("merge_commit_sha") is None: return False
    return run.get("head_sha") == sha(pull["merge_commit_sha"])
