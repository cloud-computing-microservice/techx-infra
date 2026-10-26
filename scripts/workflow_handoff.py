"""Single-consumption GitHub workflow handoffs bound to protected S3 intents.

The dispatch API has no idempotency key. Persist CALLING before POST and never
resubmit an uncertain call; recover only a positively identified real run.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from automation_session import utc, validate
from chart_selection import CHART_REPO, INFRA_REPO, PLATFORM_REPO, sha
from lifecycle_dispatch import CONTEXT
from plan_approval import version
from plan_review_store import ACCOUNT, checksum, encoded, immutable_json, read_json

WORKFLOWS = {
    "review-plan": (INFRA_REPO, "staging-plan-review.yml"),
    "cold-bootstrap": (PLATFORM_REPO, "bootstrap-staging.yml"),
    "reuse-chart": (INFRA_REPO, "staging-chart-reuse.yml"),
    "select-chart": (INFRA_REPO, "staging-chart-select.yml"),
    "review-promotion": (INFRA_REPO, "staging-chart-promote.yml"),
}
LIVE = {"queued", "requested", "waiting", "pending", "in_progress"}


class DispatchUncertain(ValueError): pass


def title(dispatch_id):
    if not isinstance(dispatch_id, str) or not re.fullmatch(r"[a-f0-9]{64}", dispatch_id):
        raise ValueError("A full immutable workflow dispatch identity is required")
    return "TechX handoff " + dispatch_id


def target_for(operation, record, execution, *, chart_sha=None, pr_number=None):
    if operation not in WORKFLOWS: raise ValueError("Unsupported workflow handoff")
    repository, workflow = WORKFLOWS[operation]
    inputs = dict(session_id=record["sessionId"])
    if operation == "review-plan":
        ref = record.get("pendingPlan", {})
        if (ref.get("sessionId") != record["sessionId"] or ref.get("stage") not in ("foundation", "edge")
                or type(ref.get("attempt")) is not int or not 1 <= ref["attempt"] <= 999999
                or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", ""))
                or ref.get("key") != f"private/staging/{record['sessionId']}/plans/{ref['stage']}/{ref['attempt']}/request.json"):
            raise ValueError("Review handoff requires the exact pending private plan")
        version(ref.get("versionId"))
        inputs.update(stage=ref["stage"], plan_attempt=str(ref["attempt"]), request_version=ref["versionId"], request_sha256=ref["sha256"],
                      execution_version=record["executionVersion"])
    else:
        inputs["execution_version"] = record["executionVersion"]
        version(inputs["execution_version"])
        if operation == "select-chart":
            sha(chart_sha)
            if type(pr_number) is not int or pr_number < 1: raise ValueError("A verified merged proposal PR is required")
            inputs.update(chart_sha=chart_sha, pr_number=str(pr_number))
    source = execution["sourceRevisions"][repository.rsplit("/", 1)[1]]
    return dict(operation=operation, repository=repository, workflow=workflow, sourceSha=sha(source), inputs=inputs)


def intent_for(record, execution_version, target):
    validate(record)
    version(execution_version)
    if target.get("operation") not in WORKFLOWS or (target.get("repository"), target.get("workflow")) != WORKFLOWS[target["operation"]]:
        raise ValueError("Workflow target escaped the fixed operation allowlist")
    sha(target.get("sourceSha"))
    body = dict(schemaVersion=1, context={key: deepcopy(record[key]) for key in CONTEXT}, executionVersion=execution_version,
        target=deepcopy(target), predecessor={key: deepcopy(record.get(key)) for key in
            ("lifecycleDispatch", "pendingPlan", "bootstrapRelease", "bootstrapProposal", "bootstrapChart")})
    dispatch_id = checksum(body)
    return dict(body, dispatchId=dispatch_id)


def intent_key(session_id, dispatch_id):
    title(dispatch_id)
    if not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session_id): raise ValueError("Invalid handoff session")
    return f"handoffs/staging/{session_id}/{dispatch_id}/intent.json"


def receipt_key(session_id, dispatch_id):
    return intent_key(session_id, dispatch_id).replace("/intent.json", "/receipt.json")


def response_identity(value, repository):
    number = value.get("workflow_run_id")
    if (set(value) != {"workflow_run_id", "run_url", "html_url"} or type(number) is not int or number < 1
            or value["run_url"] != f"https://api.github.com/repos/{repository}/actions/runs/{number}"
            or value["html_url"] != f"https://github.com/{repository}/actions/runs/{number}"):
        raise ValueError("Dispatch did not return a verified run ID and fixed-repository URLs")
    return value


class WorkflowAPI:
    def __init__(self, repository, token):
        if repository not in {row[0] for row in WORKFLOWS.values()} or not token:
            raise ValueError("A fixed lifecycle repository and scoped Actions token are required")
        self.repository, self.token = repository, token

    def get(self, endpoint): return self.request(endpoint)

    def request(self, endpoint, payload=None):
        writes = {f"actions/workflows/{workflow}/dispatches" for repo, workflow in WORKFLOWS.values() if repo == self.repository}
        if self.repository == INFRA_REPO: writes.add("actions/workflows/staging-orchestrate.yml/dispatches")
        if payload is not None and endpoint not in writes:
            raise ValueError("Only fixed lifecycle workflow dispatch endpoints may mutate GitHub")
        request = Request(f"https://api.github.com/repos/{self.repository}/{endpoint}",
            data=encoded(payload) if payload is not None else None,
            method="POST" if payload is not None else "GET", headers={"Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "techx-workflow-handoff"})
        with urlopen(request, timeout=30) as response:
            if response.headers.get("Link"): raise ValueError("Complete bounded workflow evidence is required")
            payload = response.read(2 * 1024 * 1024 + 1)
            if response.status != 200 or len(payload) > 2 * 1024 * 1024:
                raise ValueError("Workflow API response is unverified; do not repeat a POST")
        return json.loads(payload)

    def dispatch(self, target, dispatch_id):
        if (target["repository"] != self.repository or (self.repository, target["workflow"]) not in WORKFLOWS.values()):
            raise ValueError("Workflow API target escaped its token repository")
        return response_identity(self.request(f"actions/workflows/{target['workflow']}/dispatches",
            dict(ref="main", inputs=dict(target["inputs"], dispatch_id=dispatch_id))), self.repository)

    def wake(self, session_id, execution_version):
        if self.repository != INFRA_REPO or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session_id):
            raise ValueError("A wakeup can target only an existing infra staging session")
        version(execution_version)
        return response_identity(self.request("actions/workflows/staging-orchestrate.yml/dispatches",
            dict(ref="main", inputs=dict(session_id=session_id, execution_version=execution_version))), INFRA_REPO)


def listed_json(s3, bucket, key):
    result = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=2, ExpectedBucketOwner=ACCOUNT)
    rows = result.get("Contents", [])
    if (result.get("IsTruncated") is not False or not isinstance(rows, list) or len(rows) > 1
            or any(row.get("Key") != key for row in rows)):
        raise ValueError("Handoff receipt listing is incomplete or ambiguous")
    if not rows: return None
    value, object_version = read_json(s3, bucket, key)
    version(object_version)
    return value, dict(key=key, versionId=object_version, sha256=checksum(value))


def load_intent(s3, bucket, record, ref):
    dispatch_id = record["workflowHandoff"]["id"]
    if (not isinstance(ref, dict) or set(ref) != {"key", "versionId", "sha256"}
            or ref.get("key") != intent_key(record["sessionId"], dispatch_id)):
        raise ValueError("An exact protected workflow intent reference is required")
    version(ref.get("versionId"))
    value, _ = read_json(s3, bucket, ref["key"], ref["versionId"])
    body = {key: item for key, item in value.items() if key != "dispatchId"}
    if (checksum(value) != ref["sha256"] or checksum(body) != dispatch_id or value.get("schemaVersion") != 1
            or set(value) != {"schemaVersion", "context", "executionVersion", "target", "predecessor", "dispatchId"}
            or value.get("dispatchId") != dispatch_id or value.get("executionVersion") != record.get("executionVersion")
            or value.get("context") != {key: record[key] for key in CONTEXT}):
        raise ValueError("Protected handoff intent differs from this session/execution")
    target = value["target"]
    # Rebuild the target from its immutable predecessor, never mutable callback
    # inputs or an actor-controlled display title alone.
    predecessor = dict(record, **value["predecessor"])
    expected = target_for(target["operation"], predecessor, dict(sourceRevisions=record["sourceRevisions"]),
                          chart_sha=target["inputs"].get("chart_sha"),
                          pr_number=int(target["inputs"]["pr_number"]) if "pr_number" in target["inputs"] else None)
    if target != expected: raise ValueError("Protected workflow target is invalid")
    return value


def verified_run(api, target, dispatch_id, run_id, context):
    run = api.get(f"actions/runs/{run_id}")
    if (type(run.get("id")) is not int or run["id"] != run_id or run.get("head_sha") != target["sourceSha"]
            or run.get("head_branch") != "main" or run.get("event") != "workflow_dispatch"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("display_title") != title(dispatch_id)
            or run.get("path") not in (f".github/workflows/{target['workflow']}", f".github/workflows/{target['workflow']}@main")
            or any(run.get(key, {}).get("full_name") != target["repository"] for key in ("repository", "head_repository"))
            or not utc(context["createdAt"]) <= utc(run["created_at"]) < utc(context["cleanupStartAt"])
            or run.get("status") not in LIVE | {"completed"}):
        raise ValueError("Observed workflow is not this protected handoff's actual first-attempt run")
    return run


class HandoffDriver:
    def __init__(self, dispatcher, apis): self.dispatcher, self.apis = dispatcher, apis

    def load(self):
        record, _ = self.dispatcher.current(idle=False)
        state = record.get("workflowHandoff", {})
        value = load_intent(self.dispatcher.clients["s3"], self.dispatcher.bucket, record, state.get("intentReference"))
        return record, value

    def phase(self, dispatch_id, phase, **fields):
        for attempt in range(4):
            record, etag = self.dispatcher.current(idle=False)
            if record.get("workflowHandoff", {}).get("id") != dispatch_id: raise ValueError("Workflow handoff was superseded")
            updated = deepcopy(record)
            updated["workflowHandoff"].update(phase=phase, **fields)
            try:
                self.dispatcher.store.write(updated, etag)
                return
            except Exception:
                if attempt == 3: raise

    def receipt(self, intent, response):
        d = self.dispatcher
        record, _ = d.current(idle=False)
        response_identity(response, intent["target"]["repository"])
        value = dict(schemaVersion=1, dispatchId=intent["dispatchId"], intentReference=record["workflowHandoff"]["intentReference"], response=response)
        key = receipt_key(record["sessionId"], intent["dispatchId"])
        ref = immutable_json(d.clients["s3"], d.bucket, key, value)
        ref["sha256"] = checksum(value)
        self.phase(intent["dispatchId"], "OBSERVED", receiptReference=ref)
        return response

    def call_once(self, intent):
        """The mutable session pointer alone cannot prove a POST is unconsumed."""
        d = self.dispatcher
        record, _ = d.current()
        body = dict(schemaVersion=1, dispatchId=intent["dispatchId"], intentReference=record["workflowHandoff"]["intentReference"])
        key = intent_key(record["sessionId"], intent["dispatchId"]).replace("/intent.json", "/calling.json")
        try:
            result = d.clients["s3"].put_object(Bucket=d.bucket, Key=key, Body=encoded(body), IfNoneMatch="*",
                ExpectedBucketOwner=ACCOUNT, ContentType="application/json")
            version(result.get("VersionId"))
            return True
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "PreconditionFailed": raise
            previous, object_version = read_json(d.clients["s3"], d.bucket, key)
            version(object_version)
            if previous != body: raise ValueError("Protected workflow call marker differs from its intent")
            return False

    def recover(self):
        d = self.dispatcher
        record, intent = self.load()
        target, dispatch_id = intent["target"], intent["dispatchId"]
        existing = listed_json(d.clients["s3"], d.bucket, receipt_key(record["sessionId"], dispatch_id))
        if existing:
            value, ref = existing
            if (set(value) != {"schemaVersion", "dispatchId", "intentReference", "response"}
                    or value.get("schemaVersion") != 1 or value.get("dispatchId") != dispatch_id
                    or value.get("intentReference") != record["workflowHandoff"]["intentReference"]):
                raise ValueError("Protected workflow receipt differs from the handoff intent")
            response_identity(value["response"], target["repository"])
            if record["workflowHandoff"]["phase"] in ("PREPARED", "CALLING", "OBSERVED"):
                self.phase(dispatch_id, "OBSERVED", receiptReference=ref)
            return value["response"]
        if record["workflowHandoff"]["phase"] not in ("CALLING", "OBSERVED"):
            raise ValueError("Handoff has no API call to recover")
        api = self.apis[target["repository"]]
        since = utc(d.execution["createdAt"]).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = urlencode(dict(branch="main", event="workflow_dispatch", head_sha=target["sourceSha"], created=">=" + since, per_page=100))
        result = api.get(f"actions/workflows/{target['workflow']}/runs?{query}")
        rows = result.get("workflow_runs", [])
        if not isinstance(rows, list) or len(rows) > 100 or result.get("total_count") != len(rows):
            raise ValueError("Complete workflow recovery history is required")
        matches = [row for row in rows if row.get("display_title") == title(dispatch_id)]
        if len(matches) != 1: raise DispatchUncertain("Workflow call remains unobserved or ambiguous; never resubmit it")
        number = matches[0].get("id")
        if type(number) is not int or number < 1: raise ValueError("Recovered workflow ID is invalid")
        verified_run(api, target, dispatch_id, number, d.execution)
        return self.receipt(intent, dict(workflow_run_id=number,
            run_url=f"https://api.github.com/repos/{target['repository']}/actions/runs/{number}",
            html_url=f"https://github.com/{target['repository']}/actions/runs/{number}"))

    def start(self, target):
        d = self.dispatcher
        record, etag = d.current()
        intent = intent_for(record, d.version, target)
        dispatch_id = intent["dispatchId"]
        existing = record.get("workflowHandoff")
        if existing and existing.get("id") == dispatch_id:
            self.load()
            if existing.get("phase") != "PREPARED": return self.recover()
        elif existing and existing.get("phase") != "CONSUMED":
            raise ValueError("Another workflow handoff has not been consumed")
        else:
            ref = immutable_json(d.clients["s3"], d.bucket, intent_key(record["sessionId"], dispatch_id), intent)
            ref["sha256"] = checksum(intent)
            updated = deepcopy(record)
            updated["workflowHandoff"] = dict(id=dispatch_id, phase="PREPARED", intentReference=ref)
            d.store.write(updated, etag)
        record, stored = self.load()
        if record["workflowHandoff"]["phase"] != "PREPARED" or stored != intent:
            raise ValueError("Workflow handoff preparation changed")
        api = self.apis[target["repository"]]
        if api.get("git/ref/heads/main").get("object", {}).get("sha") != target["sourceSha"]:
            raise ValueError("Target workflow main moved from the admitted source")
        self.phase(dispatch_id, "CALLING")
        fresh, _ = d.current()
        if fresh.get("workflowHandoff", {}).get("id") != dispatch_id or fresh["workflowHandoff"]["phase"] != "CALLING":
            raise ValueError("Workflow dispatch was fenced before POST")
        try:
            if not self.call_once(intent): return self.recover()
            return self.receipt(intent, api.dispatch(target, dispatch_id))
        except Exception:
            # A lost response or S3 receipt response can be recovered only by
            # positive evidence. No retry path calls dispatch() in CALLING.
            return self.recover()

    def observe(self):
        _, intent = self.load()
        response = self.recover()
        record, intent = self.load()
        run = verified_run(self.apis[intent["target"]["repository"]], intent["target"], intent["dispatchId"], response["workflow_run_id"], self.dispatcher.execution)
        return record, intent, run


def guard_child(s3, bucket, *, dispatch_id, repository, workflow, source_sha, run_id, inputs,
                observations=24, sleep=time.sleep, clock=None):
    """Automatic children cannot mutate until the protected run receipt exists."""
    if not dispatch_id: return  # Explicit manual workflows retain their existing gates.
    title(dispatch_id)
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)): raise ValueError("A real dispatched workflow run is required")
    clock = clock or (lambda: datetime.now(timezone.utc))
    if type(observations) is not int or not 1 <= observations <= 24: raise ValueError("Child handoff wait must be bounded")
    for _ in range(observations):
        record, _ = read_json(s3, bucket, "automation/staging/session.json")
        validate(record)
        if (record["status"] not in ("PLANNING", "PROVISIONING")
                or clock() >= utc(record["cleanupStartAt"])):
            raise ValueError("Automatic child belongs to a closed staging session")
        # Use the protected intent itself, not mutable session pointer fields.
        key = intent_key(inputs["session_id"], dispatch_id)
        value, object_version = read_json(s3, bucket, key)
        version(object_version)
        checked = dict(record, workflowHandoff=dict(id=dispatch_id))
        intent = load_intent(s3, bucket, checked, dict(key=key, versionId=object_version, sha256=checksum(value)))
        target = intent["target"]
        if (target["repository"] != repository or target["workflow"] != workflow or target["sourceSha"] != source_sha
                or target["inputs"] != inputs or record["sessionId"] != inputs["session_id"]):
            raise ValueError("Automatic child inputs/source differ from the protected workflow intent")
        receipt = listed_json(s3, bucket, receipt_key(record["sessionId"], dispatch_id))
        if receipt:
            value, _ = receipt
            expected_ref = dict(key=key, versionId=object_version, sha256=checksum(intent))
            if (value.get("schemaVersion") != 1 or value.get("dispatchId") != dispatch_id
                    or value.get("intentReference") != expected_ref
                    or response_identity(value["response"], repository)["workflow_run_id"] != int(run_id)):
                raise ValueError("Automatic child run is not the protected dispatched run")
            return
        sleep(5)
    raise DispatchUncertain("Automatic child has no protected dispatch receipt; do not mutate or fabricate one")
