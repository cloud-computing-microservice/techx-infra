"""Preview and consume a human-reviewed, fixed-window staging start request."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import urlopen

from approve_plan import verified_reviewer
from automation_budget import approved_budget, validate_estimate
from automation_bundle import extract, pack
from automation_session import utc
from cleanup_execution import assert_inventory
from lifecycle_coordinator import ACCOUNT, Coordinator, initial_record
from plan_review_store import checksum
from promotion_authority import authority, identity as app_identity
from run_bootstrap_publish import verified_checkout

REPOS = {name: f"cloud-computing-microservice/{name}" for name in ("techx-infra", "techx-chart", "techx-platform")}
WORKFLOW = ".github/workflows/staging-lifecycle.yml"
ENVIRONMENT = "staging-start"
INPUTS = {"action", "platform_sha", "chart_sha", "duration_hours", "approved_budget_usd", "budget_alert_email"}


def typed_inputs(value):
    if not isinstance(value, dict) or set(value) != INPUTS: raise ValueError("Complete typed lifecycle inputs are required")
    if value["action"] not in ("deploy-test-destroy", "deploy-window"): raise ValueError("Unsupported opening action")
    for key in ("platform_sha", "chart_sha"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{40}", value[key]): raise ValueError("Full peer SHAs are required")
    if not re.fullmatch(r"[0-9]{1,2}", str(value["duration_hours"])) or not 3 <= int(value["duration_hours"]) <= 24:
        raise ValueError("Choose an integer window between 3 and 24 hours")
    text = str(value["approved_budget_usd"])
    if not re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,2})?", text): raise ValueError("Budget must be a finite amount with at most two decimals")
    budget = approved_budget(float(text))
    email = value["budget_alert_email"]
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("A valid private budget alert recipient is required")
    return dict(value, duration_hours=int(value["duration_hours"]), approved_budget_usd=budget)


def start_run(github, run_id, infra_sha, now):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(run_id)) or not re.fullmatch(r"[a-f0-9]{40}", infra_sha):
        raise ValueError("A real opening run and exact infra SHA are required")
    run = github.get(f"actions/runs/{run_id}")
    if (run.get("id") != int(run_id) or run.get("status") != "in_progress" or run.get("event") != "workflow_dispatch"
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1 or run.get("head_branch") != "main"
            or run.get("head_sha") != infra_sha or run.get("path") not in (WORKFLOW, WORKFLOW + "@main")
            or any(run.get(key, {}).get("full_name") != REPOS["techx-infra"] for key in ("repository", "head_repository"))
            or not now - timedelta(hours=24) <= utc(run["created_at"]) <= now):
        raise ValueError("Opening must run once on trusted infra main, not a rerun or another workflow")
    return run


def source_checks(githubs, sources):
    proofs = {}
    for name, repo in REPOS.items():
        github, sha = githubs[name], sources[name]
        if github.get("git/ref/heads/main").get("object", {}).get("sha") != sha:
            raise ValueError("A selected source is no longer trusted main; dispatch a new preview")
        response = github.get(f"actions/workflows/verify.yml/runs?head_sha={sha}&per_page=100")
        runs = response.get("workflow_runs", [])
        if type(response.get("total_count")) is not int or response["total_count"] != len(runs) or not runs:
            raise ValueError("Complete source CI history is required")
        valid = [run for run in runs if run.get("head_sha") == sha and run.get("head_branch") == "main"
            and run.get("event") in ("push", "workflow_dispatch")
            and run.get("path") in (".github/workflows/verify.yml", ".github/workflows/verify.yml@main")
            and all(run.get(key, {}).get("full_name") == repo for key in ("repository", "head_repository"))]
        if not valid or any(type(run.get("id")) is not int or run["id"] < 1 for run in valid):
            raise ValueError("Each source needs its trusted main CI")
        latest = max(valid, key=lambda run: run["id"])
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            raise ValueError("Latest source main CI must pass; older green runs do not authorize start")
        proofs[name] = dict(sha=sha, ciRun=f"https://github.com/{repo}/actions/runs/{latest['id']}")
        if github.get("git/ref/heads/main").get("object", {}).get("sha") != sha:
            raise ValueError("Source moved during CI admission")
    return proofs


class StartWorkflow:
    def __init__(self, githubs, run_id, infra_sha, workspace, *, promotion_check, lifecycle_check, clients=None, store=None, bucket=None, kms_key=None,
                 clock=None, run=subprocess.run, checkout=verified_checkout, bundle_pack=pack, bundle_extract=extract, open_url=urlopen):
        self.githubs, self.run_id, self.infra_sha = githubs, str(run_id), infra_sha
        self.workspace, self.clients, self.store, self.bucket = Path(workspace).resolve(), clients, store, bucket
        self.kms_key = kms_key
        if not callable(promotion_check) or not callable(lifecycle_check): raise ValueError("Live App authority preflights are mandatory")
        self.promotion_check, self.lifecycle_check = promotion_check, lifecycle_check
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run, self.checkout, self.pack, self.extract, self.open_url = run, checkout, bundle_pack, bundle_extract, open_url

    def estimate(self, record):
        command = ["pwsh", "-NoProfile", "-File", str(self.workspace / "techx-infra/scripts/cost-estimate.ps1"),
                   "-Hours", str(record["maximumHours"]), "-ApprovedBudgetUsd", str(record["approvedBudgetUsd"])]
        try:
            result = self.run(command, check=True, capture_output=True, timeout=60)
        except subprocess.CalledProcessError as error:
            # This fixed estimator reads only numeric inputs and source rates;
            # its bounded diagnostic contains neither email nor AWS credentials.
            detail = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else str(error.stderr or "")
            raise ValueError("Cost preview rejected the window/budget: " + detail[-2048:]) from error
        if len(result.stdout) > 1024 * 1024: raise ValueError("Cost estimate exceeds size policy")
        estimate = json.loads(result.stdout)
        validate_estimate(record, estimate)
        return estimate

    def preview(self, inputs):
        values, now = typed_inputs(inputs), self.clock()
        start_run(self.githubs["techx-infra"], self.run_id, self.infra_sha, now)
        sources = {"techx-infra": self.infra_sha, "techx-platform": values["platform_sha"], "techx-chart": values["chart_sha"]}
        checks = source_checks(self.githubs, sources)
        promotion = authority(self.promotion_check())
        lifecycle = app_identity(self.lifecycle_check())
        if lifecycle["appId"] == promotion["appId"]: raise ValueError("Lifecycle and promotion require separate dedicated Apps")
        record = initial_record(f"staging-gh-{self.run_id}", sources, values["duration_hours"], values["budget_alert_email"], values["action"],
                                utc(now.isoformat()).replace(microsecond=0), approved_budget_usd=values["approved_budget_usd"])
        estimate = self.estimate(record)
        record.pop("budgetAlertEmail")
        return dict(schemaVersion=3, record=record, sourceChecks=checks, costEstimate=estimate, backend=self.backend(), promotionAuthority=promotion, lifecycleIdentity=lifecycle,
                    workflowRun=f"https://github.com/{REPOS['techx-infra']}/actions/runs/{self.run_id}")

    def backend(self):
        if (not isinstance(self.bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
                or not isinstance(self.kms_key, str) or not re.fullmatch(rf"arn:aws:kms:us-east-1:{ACCOUNT}:key/[a-f0-9-]{{36}}", self.kms_key)):
            raise ValueError("A fixed account/region KMS key and reviewed control bucket are required")
        return dict(bucket=self.bucket, kmsKeyArn=self.kms_key, region="us-east-1", stateKey="staging/terraform.tfstate")

    def verify_storage(self):
        self.backend()
        encryption = self.clients["s3"].get_bucket_encryption(Bucket=self.bucket, ExpectedBucketOwner=ACCOUNT)
        rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        if (len(rules) != 1 or rules[0].get("ApplyServerSideEncryptionByDefault") != dict(SSEAlgorithm="aws:kms", KMSMasterKeyID=self.kms_key)
                or rules[0].get("BucketKeyEnabled") is not True):
            raise ValueError("Control bucket encryption differs from the reviewed KMS backend")
        key = self.clients["kms"].describe_key(KeyId=self.kms_key).get("KeyMetadata", {})
        if (key.get("Arn") != self.kms_key or key.get("AWSAccountId") != ACCOUNT or key.get("KeyState") != "Enabled"
                or key.get("KeyManager") != "CUSTOMER" or key.get("KeyUsage") != "ENCRYPT_DECRYPT"):
            raise ValueError("Control KMS key is unavailable or differs from the admitted account/purpose")

    def extra_inventory(self):
        origins = self.clients["cloudfront"].list_vpc_origins(MaxItems="100").get("VpcOriginList", {})
        functions = self.clients["cloudfront"].list_functions(MaxItems="100").get("FunctionList", {})
        if (origins.get("IsTruncated") is not False or origins.get("NextMarker") or functions.get("NextMarker")
                or any(not isinstance(value.get("Items", []), list) or type(value.get("Quantity")) is not int
                       or value["Quantity"] != len(value.get("Items", [])) for value in (origins, functions))
                or any(not isinstance(row, dict) or not isinstance(row.get("Name"), str) or not row["Name"]
                       for value in (origins, functions) for row in value.get("Items", []))):
            raise ValueError("Extended inventory is incomplete; no session can start")
        if any(row.get("Name", "").startswith("techx-staging") for row in origins.get("Items", [])):
            raise ValueError("A staging CloudFront VPC origin remains")
        if any(row.get("Name", "").startswith("techx-staging") for row in functions.get("Items", [])):
            raise ValueError("A staging CloudFront function remains")
        for prefix in ("/aws/eks/techx-staging/", "/aws/client-vpn/techx-staging"):
            result = self.clients["logs"].describe_log_groups(logGroupNamePrefix=prefix, limit=50)
            if result.get("nextToken") or not isinstance(result.get("logGroups"), list) or result["logGroups"]:
                raise ValueError("Staging log group inventory is nonempty or unproven")

    def start(self, inputs, request, request_sha256):
        values, now = typed_inputs(inputs), self.clock()
        if (not isinstance(request, dict) or set(request) != {"schemaVersion", "record", "sourceChecks", "costEstimate", "workflowRun", "backend", "promotionAuthority", "lifecycleIdentity"}
                or type(request["schemaVersion"]) is not int or request["schemaVersion"] != 3 or not isinstance(request["record"], dict)):
            raise ValueError("A complete immutable preview is required")
        if checksum(request) != request_sha256: raise ValueError("Start request differs from the preview shown to the reviewer")
        public = request["record"]
        run = start_run(self.githubs["techx-infra"], self.run_id, self.infra_sha, now)
        created = utc(public["createdAt"])
        sources = {"techx-infra": self.infra_sha, "techx-platform": values["platform_sha"], "techx-chart": values["chart_sha"]}
        if (not isinstance(request["sourceChecks"], dict) or set(request["sourceChecks"]) != set(REPOS)
                or any(not isinstance(request["sourceChecks"][name], dict) or request["sourceChecks"][name].get("sha") != sha
                       or not re.fullmatch(rf"https://github.com/{re.escape(REPOS[name])}/actions/runs/[1-9][0-9]*", request["sourceChecks"][name].get("ciRun", ""))
                       for name, sha in sources.items())):
            raise ValueError("Opening preview source proofs are malformed")
        expected = initial_record(f"staging-gh-{self.run_id}", sources, values["duration_hours"], values["budget_alert_email"], values["action"],
                                  created, approved_budget_usd=values["approved_budget_usd"])
        record = deepcopy(expected)
        expected.pop("budgetAlertEmail")
        if (public != expected or request["backend"] != self.backend() or not utc(run["created_at"]) <= created <= now
                or now + timedelta(minutes=30) >= utc(public["cleanupStartAt"])
                or request["workflowRun"] != f"https://github.com/{REPOS['techx-infra']}/actions/runs/{self.run_id}"):
            raise ValueError("Inputs/window changed or insufficient time remains; approval cannot renew the deadline")
        validate_estimate(public, request["costEstimate"])
        checks = source_checks(self.githubs, sources)
        reviewer = verified_reviewer(self.githubs["techx-infra"], self.run_id, ENVIRONMENT)
        if not isinstance(reviewer, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", reviewer): raise ValueError("Invalid human opening reviewer")
        promotion = authority(request["promotionAuthority"])
        if authority(self.promotion_check()) != promotion:
            raise ValueError("Promotion authority changed after opening preview; a new review is required")
        lifecycle = app_identity(request["lifecycleIdentity"])
        if lifecycle["appId"] == promotion["appId"]: raise ValueError("Lifecycle and promotion require separate dedicated Apps")
        if app_identity(self.lifecycle_check()) != lifecycle:
            raise ValueError("Lifecycle App changed after opening preview; a new review is required")
        identity = self.clients["sts"].get_caller_identity()
        if (identity.get("Account") != ACCOUNT or identity.get("Arn") !=
                f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-{self.run_id}"):
            raise ValueError("Only the dedicated opening run's dispatcher role may start")
        self.verify_storage()
        used = self.clients["s3"].list_objects_v2(Bucket=self.bucket, Prefix=f"executions/{record['sessionId']}.json", MaxKeys=1, ExpectedBucketOwner=ACCOUNT)
        if used.get("IsTruncated") is not False or used.get("Contents") != []:
            raise ValueError("This start request is consumed or its absence is unproven; never reopen a run")
        for name, sha in sources.items(): self.checkout(self.workspace / name, sha)
        estimate = self.estimate(record)
        if estimate != request["costEstimate"]: raise ValueError("Cost assumptions changed after opening preview")
        result = self.run(["pwsh", "-NoProfile", "-File", str(self.workspace / "techx-infra/scripts/domain-vpn-inventory.ps1"),
            "-Environment", "staging", "-Region", "us-east-1"], check=True, capture_output=True, timeout=180)
        if len(result.stdout) > 2 * 1024 * 1024: raise ValueError("Inventory exceeds size policy")
        inventory = json.loads(result.stdout)
        assert_inventory(inventory)
        if not self.clock() - timedelta(minutes=5) <= utc(inventory["checkedAt"]) <= self.clock():
            raise ValueError("Inventory is stale or from a future observation")
        self.extra_inventory()
        self.run(["python", str(self.workspace / "techx-infra/scripts/public-dns.py")], check=True, capture_output=True, timeout=120)
        with self.open_url("https://checkip.amazonaws.com", timeout=15) as stream:
            ip = ipaddress.ip_address(stream.read(128).decode().strip())
        if ip.version != 4 or not ip.is_global: raise ValueError("Opening runner needs an observed global IPv4")
        record["sourceAdmission"] = dict(requestSha256=request_sha256, workflowRun=request["workflowRun"], reviewer=reviewer,
            previewChecks=request["sourceChecks"], startChecks=checks, inventorySha256=checksum(inventory), promotionAuthority=promotion, lifecycleIdentity=lifecycle)
        with tempfile.TemporaryDirectory(prefix="techx-start-") as directory:
            bundle = Path(directory) / "source.tar.gz"
            digest = self.pack({name: (self.workspace / name, sha) for name, sha in sources.items()}, bundle)
            if self.extract(bundle, Path(directory) / "verified", digest) != sources: raise ValueError("Bundle does not match the reviewed sources")
            # Last read-only checks precede reservation; CodeBuild cannot start
            # until Coordinator has versioned inputs and a verified AWS schedule.
            source_checks(self.githubs, sources)
            if authority(self.promotion_check()) != promotion:
                raise ValueError("Promotion authority changed before reservation; no session may start")
            if app_identity(self.lifecycle_check()) != lifecycle:
                raise ValueError("Lifecycle App changed before reservation; no session may start")
            if self.clock() + timedelta(minutes=30) >= utc(record["cleanupStartAt"]): raise ValueError("Opening window expired during preparation")
            return Coordinator(self.clients, self.store, self.bucket).start(record, bundle, inventory, estimate, str(ip))
