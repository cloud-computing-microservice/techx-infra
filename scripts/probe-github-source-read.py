"""Cloud-free CI proof of GitHub token access; no source approval is inferred."""
import os
import re

from chart_selection import GitHub
from lifecycle_admission import REPOS


def main():
    token = os.environ["GITHUB_TOKEN"]
    for name, repo in REPOS.items():
        github = GitHub(repo, token)
        sha = github.get("git/ref/heads/main").get("object", {}).get("sha", "")
        if not re.fullmatch(r"[a-f0-9]{40}", sha): raise ValueError("Source API did not expose a full main revision")
        result = github.get(f"actions/workflows/verify.yml/runs?head_sha={sha}&per_page=100")
        if not isinstance(result.get("workflow_runs"), list): raise ValueError("Workflow history is not readable with this GitHub token")
        print(f"Read-only source/Actions API access verified: {name}")
    run_id = os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[1-9][0-9]*", run_id): raise ValueError("A real CI run is required")
    approvals = GitHub(REPOS["techx-infra"], token).get(f"actions/runs/{run_id}/approvals")
    if not isinstance(approvals, list): raise ValueError("Review-history API is not readable")
    print("Review-history API readable; this CI probe does not assert human approval, OIDC or AWS acceptance.")


if __name__ == "__main__": main()
