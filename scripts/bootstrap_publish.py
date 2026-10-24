"""Cold-start image publishing from the exact admitted archive, without Git.

Run on a disposable Docker-capable publisher runner under bootstrap-publish's
heartbeat lease. This role can push existing ECR repositories, never create
infrastructure; regular READY releases retain their separate release guard.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

from automation_budget import validate_estimate
from automation_session import utc, validate
from cleanup_schedule import verify_existing
from plan_review_store import checksum, immutable_json

ACCOUNT = "058114477594"
OWNER = r"cloud-computing-microservice/techx-platform:[0-9]+:[0-9]+"
ROLE = rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-bootstrap-publisher/techx-bootstrap-[0-9]+"
CI = (("npm", "ci", "--ignore-scripts"), ("pwsh", "-NoProfile", "-File", "scripts/ui-hardcode-audit.ps1"),
      ("python", "scripts/test-prepare-chart-release.py"), ("python", "scripts/test-release-session.py"),
      ("pwsh", "-NoProfile", "-File", "scripts/test-publish-ecr-images.ps1"),
      ("npm", "audit", "--audit-level=high"), ("npm", "run", "check"), ("npm", "run", "format:check"),
      ("npm", "run", "lint"), ("npm", "test"), ("npm", "run", "build"))


def archive_loader(client, bucket, session, execution_version, directory):
    sys.path.insert(0, str(Path(__file__).parents[1] / "bootstrap/automation"))
    from cleanup_loader import load
    return load(client, bucket, session, execution_version, directory)


class ColdPublisher:
    def __init__(self, clients, store, bucket, execution, execution_version, owner, epoch, *,
                 clock=None, run=subprocess.run, load=archive_loader):
        if not re.fullmatch(OWNER, owner) or not execution_version or type(epoch) is not int or epoch < 1:
            raise ValueError("Cold publishing needs a fixed GitHub runner owner and lease epoch")
        validate(execution)
        self.clients, self.store, self.bucket = clients, store, bucket
        self.execution, self.version, self.owner, self.epoch = deepcopy(execution), execution_version, owner, epoch
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run, self.load = run, load

    def guard(self):
        identity = self.clients["sts"].get_caller_identity()
        if (identity.get("Account") != ACCOUNT or not re.fullmatch(ROLE, identity.get("Arn", ""))
                or not identity["Arn"].endswith(f"/techx-bootstrap-{self.owner.split(':')[1]}")):
            raise ValueError("Cold publishing requires its dedicated OIDC role")
        record, etag = self.store.read()
        validate(record)
        for key in ("sessionId", "account", "region", "environment", "sourceRevisions", "deadline", "cleanupStartAt", "approvedBudgetUsd", "maximumHours", "action"):
            if record.get(key) != self.execution.get(key):
                raise ValueError("Current session differs from the immutable cold-start execution")
        lease = record.get("lease", {})
        if (record.get("executionVersion") != self.version or record["status"] != "PROVISIONING"
                or lease.get("operation") != "bootstrap-publish" or lease.get("owner") != self.owner or lease.get("epoch") != self.epoch
                or utc(lease["expiresAt"]) <= self.clock() or self.clock() >= utc(record["cleanupStartAt"])
                or record.get("pendingPlan") is not None or record.get("stages", {}).get("edge")):
            raise ValueError("Cold publishing is not the owned pre-edge bootstrap operation")
        foundation = record.get("stages", {}).get("foundation", {})
        task = record.get("deploymentTask", {})
        if (foundation.get("state") != "APPLIED" or type(foundation.get("stateSerial")) is not int or foundation["stateSerial"] < 1
                or not re.fullmatch(r"[a-f0-9-]{36}", foundation.get("stateLineage", ""))
                or not re.fullmatch(r"[a-f0-9]{64}", foundation.get("savedPlanSha256", ""))
                or (task.get("state"), task.get("mode"), task.get("stage"), task.get("attempt")) !=
                   ("SUCCEEDED", "apply", "foundation", foundation.get("attempt"))):
            raise ValueError("Foundation apply has no matching committed/terminal task receipt")
        result = self.clients["ssm"].get_command_invocation(CommandId=task["commandId"], InstanceId=task["instanceId"])
        if (result.get("CommandId") != task["commandId"] or result.get("InstanceId") != task["instanceId"]
                or result.get("Status") != "Success" or result.get("ResponseCode") != 0):
            raise ValueError("Foundation SSM command is not confirmed terminal and successful")
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        validate_estimate(record, self.execution["costEstimate"])
        return record, etag

    def source(self, directory):
        self.guard()
        infra = self.load(self.clients["s3"], self.bucket, self.execution["sessionId"], self.version, directory)
        actual = json.loads((directory / "execution.json").read_bytes())
        source = directory / "source"
        if (Path(infra).resolve() != (source / "techx-infra").resolve() or actual != self.execution
                or json.loads((source / "source.json").read_bytes()) != self.execution["sourceRevisions"]):
            raise ValueError("Fresh archive does not match the admitted execution/source revisions")
        platform = source / "techx-platform"
        if (platform / ".git").exists() or not (platform / "scripts/ecr-publisher-core.ps1").is_file():
            raise ValueError("Cold publish requires the reviewed archive publisher, not an ambient Git checkout")
        # Equivalent to platform's tracked-source audit, without consulting an
        # ancestor checkout's .git when this source is an authenticated archive.
        for path in platform.rglob("*"):
            relative = path.relative_to(platform)
            if (path.is_symlink() or any(part.lower() in ("node_modules", ".next", "dist", "coverage", ".git") for part in relative.parts)
                    or path.name.lower().endswith(".tsbuildinfo")):
                raise ValueError("Generated or linked content is forbidden in archived application source")
        return platform

    def perform(self, directory):
        record, _ = self.guard()
        if self.clock() + timedelta(minutes=30) >= utc(record["cleanupStartAt"]):
            raise ValueError("Insufficient bounded CI/publish time before cleanup")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        ci_root = self.source(directory / "ci")
        for command in CI:
            self.guard()
            self.run(list(command), cwd=ci_root, check=True)
        # npm/Next may generate ignored output or rewrite generated declarations.
        # Build images from a second fresh extraction of the same verified bytes,
        # never from the CI-mutated tree or a caller-supplied skip-clean flag.
        publish_root = self.source(directory / "publish")
        driver = directory / "publish.ps1"
        driver.write_text("""param([string]$SourceRoot,[string]$SourceSha,[string]$ManifestPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $SourceRoot 'scripts/ecr-publisher-core.ps1')
Invoke-TechxEcrPublish -SourceRoot $SourceRoot -SourceSha $SourceSha -ExpectedAccountId '058114477594' -Region 'us-east-1' -ManifestPath $ManifestPath | Out-Null
""", encoding="utf-8")
        manifest_path = directory / "release.json"
        sha = self.execution["sourceRevisions"]["techx-platform"]
        self.guard()
        self.run(["pwsh", "-NoProfile", "-File", str(driver), "-SourceRoot", str(publish_root),
            "-SourceSha", sha, "-ManifestPath", str(manifest_path)], cwd=publish_root, check=True)
        if manifest_path.stat().st_size > 64 * 1024:
            raise ValueError("Cold publish manifest exceeds the size policy")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        spec = importlib.util.spec_from_file_location("cold_manifest", publish_root / "scripts/prepare-chart-release.py")
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)
        validator.validate_manifest(manifest)
        if manifest["commit"] != sha:
            raise ValueError("Published manifest does not identify the admitted platform source")
        artifact = dict(schemaVersion=1, sessionId=self.execution["sessionId"], executionVersion=self.version,
            sourceRevisions=self.execution["sourceRevisions"], manifest=manifest, sourceCiPassed=True)
        self.guard()
        key = f"releases/staging/{self.execution['sessionId']}/bootstrap.json"
        ref = immutable_json(self.clients["s3"], self.bucket, key, artifact)
        ref["sha256"] = checksum(artifact)
        record, etag = self.guard()
        if record.get("bootstrapRelease") not in (None, ref):
            raise ValueError("An existing cold release reference cannot be replaced")
        updated = deepcopy(record)
        updated["bootstrapRelease"] = ref
        self.store.write(updated, etag)
        return ref
