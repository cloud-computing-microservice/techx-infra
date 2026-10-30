"""One exact-head cold chart merge on the owned, cleanup-stoppable executor."""
from copy import deepcopy
from datetime import timedelta

from automation_session import utc
from chart_promotion import ColdPromotionChecks
from chart_selection import CHART_REPO, git_tree, sha
from deployment_tasks import promotion_reference, read_task
from github_app_auth import app_secret_snapshot, installation_client
from lifecycle_stage import StageController
from plan_approval import version
from plan_review_store import ACCOUNT, checksum, encoded, immutable_json, read_json
from promotion_authority import admitted
from private_credentials import credential_proof
from review_chart_promotion import authorize_promotion
from workflow_handoff import listed_json


class MergeUncertain(ValueError):
    """No positive merge receipt exists; observe this intent, never send again."""


def app_clients(runtime, policy, clock, execution_version):
    promotion, promotion_ref = app_secret_snapshot(runtime.clients["secretsmanager"], "promotion", kms_key=runtime.kms)
    opening = admitted(runtime.execution)
    if (promotion["appId"] != policy["appId"] or promotion["slug"] != policy["appSlug"]
            or promotion["appId"] != opening["appId"] or promotion["slug"] != opening["appSlug"]
            or promotion["installationId"] != opening["installationId"] or policy["policySha256"] != opening["policySha256"]):
        raise ValueError("Private promotion key differs from the human-reviewed branch authority")
    proof = credential_proof(runtime.private, runtime.execution, execution_version)
    if promotion_ref != proof["promotion"]["secret"]:
        raise ValueError("Private promotion key changed after foundation credential pinning")
    lifecycle, lifecycle_ref = app_secret_snapshot(runtime.clients["secretsmanager"], "lifecycle", kms_key=runtime.kms)
    expected = proof["lifecycle"]["identity"]
    if (lifecycle_ref != proof["lifecycle"]["secret"] or lifecycle["appId"] != expected["appId"]
            or lifecycle["slug"] != expected["appSlug"] or lifecycle["installationId"] != expected["installationId"]):
        raise ValueError("Private Lifecycle key changed after foundation credential pinning")
    from chart_selection import INFRA_REPO
    chart = installation_client(promotion, CHART_REPO, runtime.work, clock, merge=True)
    infra = installation_client(lifecycle, INFRA_REPO, runtime.work, clock)
    return chart, infra


class PrivateChartPromotion:
    def __init__(self, runtime, store, execution_version, task_id, *, clock, clients=app_clients):
        self.runtime, self.store, self.version, self.task_id = runtime, store, execution_version, task_id
        self.clock, self.clients = clock, clients
        self.control = StageController(runtime.clients["s3"], store, runtime.bucket, clock)
        self.checks = ColdPromotionChecks(runtime.clients, store, runtime.bucket, runtime.execution, execution_version, clock=clock)

    def approval_reference(self, record):
        return promotion_reference(record)

    def guard(self):
        record, _ = self.control.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        self.checks.guard(owner=self.owner, epoch=self.epoch)
        task = read_task(self.runtime.clients["s3"], self.runtime.bucket, record, self.task_id, self.clock())
        instance = self.runtime.private["deploymentExecutor"]["instanceId"]
        if ((task["mode"], task["stage"], task["attempt"]) != ("promote", "chart", 1)
                or task["instanceId"] != instance or self.owner != f"arn:aws:ec2:us-east-1:{ACCOUNT}:instance/{instance}"
                or record["deploymentTask"].get("state") != "RUNNING"):
            raise ValueError("Promotion is not this executor's exact claimed private task")
        if hasattr(self, "approval_ref") and promotion_reference(record) != self.approval_ref:
            raise ValueError("Promotion approval was superseded")
        if hasattr(self, "request") and any(record.get(field) != self.request[reference] for field, reference in
                (("bootstrapRelease", "releaseReference"), ("bootstrapProposal", "proposalReference"))):
            raise ValueError("Promotion release/proposal was superseded")
        return record

    def window(self, approval):
        record = self.guard()
        if self.chart.get("git/ref/heads/main").get("object", {}).get("sha") != self.request["baselineSha"]:
            raise ValueError("Chart main moved before the exact approved merge call")
        limit = min(utc(approval["expiresAt"]), utc(record["lease"]["expiresAt"]), utc(record["cleanupStartAt"]))
        if self.clock() + timedelta(seconds=60) >= limit:
            raise ValueError("Insufficient owned approval/lease/window for the bounded merge call")

    def put(self, key, value):
        self.guard()
        ref = immutable_json(self.runtime.clients["s3"], self.runtime.bucket, key, value)
        return dict(ref, sha256=checksum(value))

    def once(self, intent_ref):
        self.guard()
        marker = dict(schemaVersion=1, intentReference=intent_ref)
        key = self.prefix + "/calling.json"
        try:
            result = self.runtime.clients["s3"].put_object(Bucket=self.runtime.bucket, Key=key, Body=encoded(marker),
                IfNoneMatch="*", ExpectedBucketOwner=ACCOUNT, ContentType="application/json")
            version(result.get("VersionId"))
            return True
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed": raise
            old, object_version = read_json(self.runtime.clients["s3"], self.runtime.bucket, key)
            version(object_version)
            if old != marker: raise ValueError("Protected merge call marker differs from its intent")
            return False

    def recover(self, intent, intent_ref):
        self.guard()
        marker = listed_json(self.runtime.clients["s3"], self.runtime.bucket, self.prefix + "/calling.json")
        if marker is None or marker[0] != dict(schemaVersion=1, intentReference=intent_ref):
            raise MergeUncertain("Merge has no positive protected calling marker; do not fabricate a result")
        request = intent["request"]
        pull = self.chart.get(f"pulls/{request['prNumber']}")
        if pull.get("merged") is not True or pull.get("state") != "closed":
            raise MergeUncertain("Merge call remains unobserved; never issue another PUT")
        chosen = sha(pull.get("merge_commit_sha"))
        commit = self.chart.get(f"git/commits/{chosen}")
        if (pull.get("number") != request["prNumber"] or pull.get("html_url") != request["pullRequest"]
                or pull.get("head", {}).get("sha") != request["headSha"] or pull.get("base", {}).get("ref") != "main"
                or any(pull.get(side, {}).get("repo", {}).get("full_name") != CHART_REPO for side in ("head", "base"))
                or pull.get("merged_by", {}).get("type") != "Bot" or pull["merged_by"].get("login") != request["branchPolicy"]["appSlug"] + "[bot]"
                or not utc(intent["createdAt"]).replace(microsecond=0) <= utc(pull["merged_at"]) < utc(intent["approvalExpiresAt"])
                or utc(pull["merged_at"]) > self.clock()
                or commit.get("sha") != chosen or commit.get("tree", {}).get("sha") != request["treeSha"]
                or [row.get("sha") for row in commit.get("parents", [])] != [request["baselineSha"], request["headSha"]]
                or not isinstance(commit.get("message"), str) or commit["message"].split("\n", 1)[0] != self.title
                or self.chart.get("git/ref/heads/main").get("object", {}).get("sha") != chosen):
            raise ValueError("Observed merge is not this exact intent's App, commit, parents, tree and window")
        tree, _ = git_tree(self.chart, chosen)  # Complete, hash-verified Git tree metadata.
        if tree != request["treeSha"]: raise ValueError("Merge changed the approved chart tree")
        receipt = dict(schemaVersion=1, intentReference=intent_ref, approvalReference=self.approval_ref,
            taskId=self.task_id, chartSha=chosen, headSha=request["headSha"], treeSha=tree, mergedAt=pull["merged_at"])
        ref = self.put(self.prefix + "/receipt.json", receipt)
        return receipt, ref

    def checkpoint(self, receipt, ref, request):
        self.guard()
        current, etag = self.control.owned(self.runtime.execution["sessionId"], self.owner, self.epoch)
        if (promotion_reference(current) != self.approval_ref or current.get("deploymentTask", {}).get("taskId") != self.task_id
                or current["deploymentTask"].get("state") != "RUNNING"
                or current.get("bootstrapRelease") != request["releaseReference"] or current.get("bootstrapProposal") != request["proposalReference"]):
            raise ValueError("Merge checkpoint lost its exact task/approval")
        value = dict(state="PROMOTED", attempt=1, taskId=self.task_id, approvalReference=self.approval_ref,
            releaseReference=request["releaseReference"], proposalReference=request["proposalReference"],
            chartSha=receipt["chartSha"], headSha=receipt["headSha"], treeSha=receipt["treeSha"], mergeReceiptReference=ref)
        previous = current.get("stages", {}).get("chart")
        if previous not in (None, value): raise ValueError("A different promotion checkpoint cannot be replaced")
        updated = deepcopy(current)
        updated.setdefault("stages", {})["chart"] = value
        self.store.write(updated, etag)
        return dict(chartSha=receipt["chartSha"], mergeReceiptReference=ref)

    def perform(self, owner, epoch):
        self.owner, self.epoch = owner, epoch
        self.runtime.executor()
        record = self.guard()
        self.approval_ref = deepcopy(self.approval_reference(record))
        approval, _ = read_json(self.runtime.clients["s3"], self.runtime.bucket, self.approval_ref["key"], self.approval_ref["versionId"])
        if checksum(approval) != self.approval_ref["sha256"]: raise ValueError("Private promotion approval checksum changed")
        self.request = deepcopy(approval["request"])
        self.guard()
        self.prefix = f"promotions/staging/{record['sessionId']}/{self.approval_ref['sha256']}"
        self.chart, self.infra = self.clients(self.runtime, approval["request"]["branchPolicy"], self.clock, self.version)
        expected = dict(schemaVersion=1, sessionId=record["sessionId"], executionVersion=self.version, sourceRevisions=self.runtime.execution["sourceRevisions"],
            taskId=self.task_id, instanceId=self.runtime.private["deploymentExecutor"]["instanceId"], approvalReference=self.approval_ref,
            request=approval["request"], approvalExpiresAt=approval["expiresAt"])
        existing = listed_json(self.runtime.clients["s3"], self.runtime.bucket, self.prefix + "/intent.json")
        if existing:
            intent, intent_ref = existing
            if {key: value for key, value in intent.items() if key != "createdAt"} != expected:
                raise ValueError("Existing merge intent belongs to another task, source or approval")
            if not (utc(approval["approvedAt"]) <= utc(intent["createdAt"]) < utc(approval["expiresAt"]) and utc(intent["createdAt"]) <= self.clock()):
                raise ValueError("Existing merge intent has an invalid authorization timestamp")
        else:
            authorize_promotion(self.checks, self.runtime.source, self.chart, self.infra, self.approval_ref,
                owner=self.owner, epoch=self.epoch, app_id=expected["request"]["branchPolicy"]["appId"], app_slug=expected["request"]["branchPolicy"]["appSlug"])
            intent = dict(expected, createdAt=self.clock().isoformat())
            intent_ref = self.put(self.prefix + "/intent.json", intent)
        self.title = "TechX promotion " + checksum(intent)
        marker = listed_json(self.runtime.clients["s3"], self.runtime.bucket, self.prefix + "/calling.json")
        if marker is None:
            authorize_promotion(self.checks, self.runtime.source, self.chart, self.infra, self.approval_ref,
                owner=self.owner, epoch=self.epoch, app_id=expected["request"]["branchPolicy"]["appId"], app_slug=expected["request"]["branchPolicy"]["appSlug"])
            self.runtime.executor()
            self.window(approval)
            try:
                fresh = self.once(intent_ref)
            except Exception:
                # A marker response is not permission to issue the GitHub PUT.
                receipt, ref = self.recover(intent, intent_ref)
            else:
                if fresh:
                    self.window(approval)
                    request = intent["request"]
                    try:
                        result = self.chart.merge(request["prNumber"], request["headSha"], self.title,
                            f"Session {record['sessionId']}; task {self.task_id}")
                        if result.get("merged") is not True: raise MergeUncertain("Merge response is not positive")
                    except Exception:
                        # Includes lost/failed HTTP responses. Reconcile only;
                        # no alternative endpoint or retry may issue a merge.
                        receipt, ref = self.recover(intent, intent_ref)
                    else:
                        receipt, ref = self.recover(intent, intent_ref)
                else:
                    receipt, ref = self.recover(intent, intent_ref)
        else:
            receipt, ref = self.recover(intent, intent_ref)
        return self.checkpoint(receipt, ref, intent["request"])
