"""Observe admitted work, dispatch typed workflow handoffs and consume real results."""
from copy import deepcopy
import time

from automation_session import utc
from chart_selection import (CHART_REPO, INFRA_REPO, read_proposal, read_release, read_selection, reuse_proposal, sha)
from chart_promotion import candidate_ci, PromotionPending, pull_identity
from continue_lifecycle import drive
from deployment_tasks import validate_promotion
from lifecycle_dispatch import dispatch_identity, observed_build, operation
from plan_approval import version
from plan_review_store import approval_key, checksum, read_json
from review_chart_promotion import ENVIRONMENT, promotion_index_key, read_promotion_approval, verified_reviewer
from workflow_handoff import DispatchUncertain, HandoffDriver, LIVE, target_for


class Orchestrator:
    def __init__(self, dispatcher, coordinator, apis, chart):
        self.dispatcher, self.coordinator, self.apis, self.chart = dispatcher, coordinator, apis, chart
        self.handoffs = HandoffDriver(dispatcher, apis)

    def consume(self, record, intent, run):
        d = self.dispatcher
        op = intent["target"]["operation"]
        source_run = f"https://github.com/{intent['target']['repository']}/actions/runs/{run['id']}"
        if op == "review-plan":
            ref = intent["predecessor"]["pendingPlan"]
            envelope, _ = read_json(d.clients["s3"], d.bucket, ref["key"], ref["versionId"])
            if checksum(envelope) != ref["sha256"]: raise ValueError("Review request bytes differ from the protected handoff")
            approval, _ = read_json(d.clients["s3"], d.bucket, approval_key(envelope["request"], ref["sha256"]))
            if approval.get("reviewRun") != source_run or approval.get("reviewReference") != ref:
                raise ValueError("Completed review workflow has no matching protected approval")
            apply = operation("apply", ref["stage"], ref["attempt"])
            predecessor = dict(record, lifecycleDispatch=intent["predecessor"]["lifecycleDispatch"], pendingPlan=ref)
            expected = dispatch_identity(predecessor, d.version, apply)
            if record.get("lifecycleDispatch", {}).get("id") != expected:
                if record["workflowHandoff"]["phase"] == "CONSUMING":
                    raise DispatchUncertain("Review consumption is uncertain; never repeat an apply request")
                if (record.get("pendingPlan") != ref or record.get("lifecycleDispatch") != intent["predecessor"]["lifecycleDispatch"]):
                    raise ValueError("Review predecessor was superseded before apply")
                self.handoffs.phase(intent["dispatchId"], "CONSUMING")
                self.coordinator.resume_approved(record["sessionId"])
            current, _ = d.current(idle=False)
            if current.get("lifecycleDispatch", {}).get("id") != expected:
                raise ValueError("Review did not dispatch the exact apply successor")
            observed_build(d.clients["codebuild"], current)  # A positive handle, not a guessed API result.
        elif op == "cold-bootstrap":
            release = read_release(d.clients["s3"], d.bucket, record, d.execution, d.version)
            read_proposal(d.clients["s3"], d.bucket, record, d.execution, d.version)
            if release["sourceCi"]["workflowRunId"] != run["id"]:
                raise ValueError("Cold release did not come from the dispatched platform workflow")
        elif op == "review-promotion":
            self.consume_promotion(record, intent, run, source_run)
        elif op == "accept-public":
            from public_acceptance_contract import read_public_receipt
            receipt = read_public_receipt(d.clients["s3"], d.bucket, record, d.version)
            if (receipt["workflowRun"] != source_run or receipt["dispatchId"] != intent["dispatchId"]
                    or record["stages"]["exposure"] != intent["predecessor"]["stages"]["exposure"]):
                raise ValueError("Public acceptance did not come from the exact dispatched predecessor/run")
        else:
            read_release(d.clients["s3"], d.bucket, record, d.execution, d.version)
            selected = read_selection(d.clients["s3"], d.bucket, record, d.execution, d.version)
            proof = selected["provenance"]
            actual = proof.get("reviewRun") if reuse_proposal(selected) else proof.get("selectionRun")
            if actual != source_run:
                raise ValueError("Chart selection was not written by the dispatched workflow")
        self.handoffs.phase(intent["dispatchId"], "CONSUMED")

    def consume_promotion(self, record, intent, run, source_run):
        """A successful dispatched review discovers its own protected grant."""
        d = self.dispatcher
        index, object_version = read_json(d.clients["s3"], d.bucket, promotion_index_key(record["sessionId"], run["id"]))
        version(object_version)
        expected = dict(schemaVersion=1, kind="cold-promotion-index", sessionId=record["sessionId"],
            executionVersion=d.version, sourceRevisions=d.execution["sourceRevisions"],
            dispatchId=intent["dispatchId"], workflowRun=source_run)
        if (not isinstance(index, dict) or set(index) != set(expected) | {"approvalReference"}
                or any(index.get(key) != value for key, value in expected.items())):
            raise ValueError("Completed promotion review has no matching protected grant index")
        ref = index["approvalReference"]
        approval = read_promotion_approval(d.clients["s3"], d.bucket, d.execution, d.version, ref, d.clock())
        request = approval["request"]
        if (request.get("workflowRun") != source_run
                or not utc(run["created_at"]) <= utc(approval["approvedAt"])
                or not utc(approval["approvedAt"]).replace(microsecond=0) <= utc(run["updated_at"]) <= d.clock()
                or verified_reviewer(self.apis[INFRA_REPO], run["id"], environment=ENVIRONMENT) != approval["reviewer"]):
            raise ValueError("Promotion grant was not issued by this dispatched human review")
        for attempt in range(4):
            current, etag = d.current()
            if (current.get("workflowHandoff", {}).get("id") != intent["dispatchId"]
                    or any(current.get(key) != intent["predecessor"].get(key)
                           for key in ("bootstrapRelease", "bootstrapProposal", "lifecycleDispatch"))
                    or request.get("releaseReference") != current.get("bootstrapRelease")
                    or request.get("proposalReference") != current.get("bootstrapProposal")
                    or current.get("bootstrapPromotionApproval") not in (None, ref)):
                raise ValueError("Promotion review predecessor was superseded before grant consumption")
            # The grant is checked again after external review reads and every
            # CAS retry; the dispatcher never creates or extends its expiry.
            read_promotion_approval(d.clients["s3"], d.bucket, d.execution, d.version, ref, d.clock())
            updated = deepcopy(current)
            updated["bootstrapPromotionApproval"] = deepcopy(ref)
            validate_promotion(updated, 1)
            if current.get("bootstrapPromotionApproval") == ref: return
            try:
                d.store.write(updated, etag)
                return
            except Exception:
                if attempt == 3: raise

    def target(self, result):
        d = self.dispatcher
        record, _ = d.current()
        waiting = result["waiting"]
        if waiting == "PLAN_APPROVAL": return target_for("review-plan", record, d.execution)
        if waiting == "PUBLIC_ACCEPTANCE": return target_for("accept-public", record, d.execution)
        if waiting in ("COLD_RELEASE", "CHART_PROPOSAL"):
            if waiting == "CHART_PROPOSAL":
                raise ValueError("A completed cold workflow must publish both release and proposal evidence")
            return target_for("cold-bootstrap", record, d.execution)
        if waiting == "CHART_SELECTION":
            proposal = read_proposal(d.clients["s3"], d.bucket, record, d.execution, d.version)
            if reuse_proposal(proposal): return target_for("reuse-chart", record, d.execution)
            pull = self.chart.get(f"pulls/{proposal['prNumber']}")
            if (pull.get("number") != proposal["prNumber"] or pull.get("head", {}).get("sha") != proposal["headSha"]
                    or pull.get("base", {}).get("ref") != "main"
                    or any(pull.get(side, {}).get("repo", {}).get("full_name") != CHART_REPO for side in ("head", "base"))):
                raise ValueError("Chart promotion differs from the immutable proposal")
            if pull.get("state") == "open" and pull.get("merged") is False:
                # Readiness cannot authorize a merge. The dispatched preview
                # repeats the full tree/review/branch-policy checks, then waits
                # for its own protected human approval before issuing a grant.
                if record.get("stages", {}).get("chart", {}).get("state") == "PROMOTED":
                    return dict(waiting="CHART_CI", pullRequest=proposal["pullRequest"])
                try:
                    pull_identity(self.chart, proposal)
                    candidate_ci(self.chart, proposal, d.execution)
                except PromotionPending:
                    return dict(waiting="CHART_PROMOTION", pullRequest=proposal["pullRequest"])
                return target_for("review-promotion", record, d.execution)
            if pull.get("merged") is not True or pull.get("state") != "closed":
                raise ValueError("The release proposal was closed without a valid merge")
            chosen = sha(pull.get("merge_commit_sha"))
            history = self.chart.get(f"actions/workflows/verify.yml/runs?head_sha={chosen}&per_page=100")
            runs = history.get("workflow_runs", [])
            if (not isinstance(runs, list) or len(runs) > 100 or type(history.get("total_count")) is not int
                    or history["total_count"] != len(runs)):
                raise ValueError("Complete chart main CI history is required before selection")
            if not runs: return dict(waiting="CHART_CI", chartSha=chosen, pullRequest=proposal["pullRequest"])
            valid = [run for run in runs if run.get("head_sha") == chosen and run.get("head_branch") == "main"
                and run.get("event") in ("push", "workflow_dispatch")
                and run.get("path") in (".github/workflows/verify.yml", ".github/workflows/verify.yml@main")
                and all(run.get(key, {}).get("full_name") == CHART_REPO for key in ("repository", "head_repository"))]
            if not valid or any(type(run.get("id")) is not int or run["id"] < 1 for run in valid):
                raise ValueError("Trusted chart CI is missing")
            ci = max(valid, key=lambda row: row["id"])
            if ci.get("status") in LIVE: return dict(waiting="CHART_CI", chartSha=chosen, pullRequest=proposal["pullRequest"])
            if ci.get("status") != "completed" or ci.get("conclusion") != "success":
                raise ValueError("Latest merged chart CI failed; do not select its release")
            return target_for("select-chart", record, d.execution, chart_sha=chosen, pr_number=proposal["prNumber"])
        return result

    def run(self, *, observations=240, sleep=time.sleep, monotonic=time.monotonic):
        if type(observations) is not int or not 1 <= observations <= 240:
            raise ValueError("Orchestration observation must be bounded to 40 minutes")
        end = monotonic() + 2400
        last = {}
        for _ in range(observations):
            if monotonic() >= end: break
            record, _ = self.dispatcher.current(idle=False, allow_ready=True)
            state = record.get("workflowHandoff", {})
            if state and state.get("phase") != "CONSUMED":
                _, intent = self.handoffs.load()
                if intent["target"]["repository"] not in self.apis:
                    return dict(waiting="GITHUB_APP_CONFIGURATION", targetRepository=intent["target"]["repository"])
                if state.get("phase") == "PREPARED": self.handoffs.start(intent["target"])
                record, intent, run = self.handoffs.observe()
                last = dict(waiting="WORKFLOW_RUNNING", repository=intent["target"]["repository"], workflowRunId=run["id"],
                            workflowRun=f"https://github.com/{intent['target']['repository']}/actions/runs/{run['id']}")
                if run["status"] in LIVE:
                    sleep(max(0, min(10, end - monotonic())))
                    continue
                if run.get("conclusion") != "success":
                    self.dispatcher.cleanup()
                    raise ValueError("Dispatched workflow failed/cancelled; independent cleanup requested")
                self.consume(record, intent, run)
                continue
            # One observation shares this loop's overall bound. It does not
            # start another 40-minute timer for every lifecycle stage.
            result = drive(self.dispatcher, self.coordinator, observations=1,
                           sleep=lambda delay: sleep(max(0, min(delay, end - monotonic()))), monotonic=monotonic)
            last = result
            if result.get("waiting") == "READY": return result
            if result.get("waiting") == "OBSERVATION_LIMIT": continue
            if result.get("waiting"):
                target = self.target(result)
                if target.get("waiting"):
                    if target["waiting"] in ("CHART_PROMOTION", "CHART_CI"):
                        last = target
                        sleep(max(0, min(10, end - monotonic())))
                        continue
                    return target
                if target["repository"] not in self.apis:
                    return dict(waiting="GITHUB_APP_CONFIGURATION", targetRepository=target["repository"])
                response = self.handoffs.start(target)
                last = dict(waiting="WORKFLOW_RUNNING", repository=target["repository"], workflowRunId=response["workflow_run_id"],
                            workflowRun=response["html_url"])
        return dict(last, waiting="OBSERVATION_LIMIT", sessionId=self.dispatcher.execution["sessionId"],
                    executionVersion=self.dispatcher.version)
