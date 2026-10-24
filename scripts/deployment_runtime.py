"""Observe and execute Terraform stages on the session's persistent EC2 executor.

Only an immutable-bundle worker holding the deployment lease may call this
runtime. It never derives approval context from the previous review request.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import urlopen

from automation_inputs import validate_variables
from automation_session import validate
from lifecycle_stage import apply_saved_plan
from plan_approval import digest
from plan_review_store import summarize_changes
from workload_state import MAX_STATE_BYTES, validate_state

ACCOUNT = "058114477594"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/techx-automation-lifecycle-executor"
PROFILE = f"arn:aws:iam::{ACCOUNT}:instance-profile/techx-automation-lifecycle-executor"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class TerraformRuntime:
    def __init__(self, source, execution, private, bucket, kms_key, clients, work, *, run=subprocess.run, open_url=urlopen):
        validate(execution)
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("Invalid control bucket")
        if not re.fullmatch(rf"arn:aws:kms:us-east-1:{ACCOUNT}:key/[a-f0-9-]{{36}}", kms_key):
            raise ValueError("Invalid control encryption key")
        self.source = Path(source).resolve()
        if json.loads((self.source / "source.json").read_text()) != execution["sourceRevisions"]:
            raise ValueError("Extracted source manifest differs from immutable execution")
        if private.get("sessionId") != execution["sessionId"] or private.get("schemaVersion") != 1:
            raise ValueError("Private input snapshot belongs to another execution")
        validate_variables(private["terraformVars"])
        if private["terraformVars"].get("workload_permissions_boundary_arn") != "arn:aws:iam::058114477594:policy/techx-automation-workload-boundary":
            raise ValueError("Automation requires the reviewed workload IAM boundary")
        if private["terraformVars"].get("budget_limit_usd") != execution.get("approvedBudgetUsd"):
            raise ValueError("Private Terraform budget differs from the approved session")
        self.execution, self.private = deepcopy(execution), deepcopy(private)
        self.bucket, self.kms, self.clients = bucket, kms_key, clients
        self.infra = self.source / "techx-infra"
        self.root = self.infra / "environments/staging"
        self.work = Path(work).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.work.chmod(0o700)
        self.run, self.open_url = run, open_url
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("TF_VAR_")}
        overrides = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
                     "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
                     "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME", "AWS_WEB_IDENTITY_TOKEN_FILE",
                     "AWS_EC2_METADATA_SERVICE_ENDPOINT", "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
                     "AWS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
        if (any(key.upper().startswith(("TF_CLI_ARGS", "AWS_ENDPOINT_URL", "AWS_CONTAINER_")) for key in self.env)
                or any(key.startswith("TF_") and key not in ("TF_DATA_DIR", "TF_IN_AUTOMATION", "TF_INPUT", "TF_WORKSPACE") for key in self.env)
                or any(self.env.get(key) or self.env.get(key.lower()) for key in overrides)
                or self.env.get("TF_WORKSPACE", "default") != "default"
                or self.env.get("AWS_EC2_METADATA_DISABLED", "false").lower() != "false"):
            raise ValueError("Ambient Terraform/network/credential overrides are forbidden in automation")
        # Do not load a host's ~/.terraformrc or ~/.aws files. Only the EC2 role
        # supplies credentials; init still enforces the bundled provider lock.
        cli_config = self.work / "terraform.rc"
        cli_config.write_text("disable_checkpoint = true\n", encoding="utf-8")
        self.env.update(TF_DATA_DIR=str(self.work / ".terraform"), TF_IN_AUTOMATION="true", TF_INPUT="false",
                        TF_WORKSPACE="default", TF_CLI_CONFIG_FILE=str(cli_config),
                        AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                        AWS_DEFAULT_REGION="us-east-1", AWS_REGION="us-east-1")
        self.variables = None
        self.stage = None

    def executor(self):
        identity = self.clients["sts"].get_caller_identity()
        match = re.fullmatch(rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-executor/(i-[a-f0-9]{{8,17}})", identity.get("Arn", ""))
        if identity.get("Account") != ACCOUNT or not match:
            raise ValueError("Terraform must run under the dedicated EC2 instance role")
        instance_id = match[1]
        expected = self.private.get("deploymentExecutor", {})
        if expected.get("instanceId") != instance_id or not re.fullmatch(r"ami-[a-f0-9]{8,17}", expected.get("imageId", "")):
            raise ValueError("EC2 role session differs from the recorded deployment executor")
        response = self.clients["ec2"].describe_instances(InstanceIds=[instance_id])
        reservations = response.get("Reservations", [])
        if len(reservations) != 1 or reservations[0].get("OwnerId") != ACCOUNT or len(reservations[0].get("Instances", [])) != 1:
            raise ValueError("Deployment executor inventory is ambiguous")
        instance = reservations[0]["Instances"][0]
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        wanted = dict(Project="techx", Environment="staging", Role="deployment-executor", SessionId=self.execution["sessionId"])
        metadata = instance.get("MetadataOptions", {})
        if (instance.get("InstanceId") != instance_id or instance.get("ImageId") != expected["imageId"]
                or instance.get("InstanceType") != "t3.small"
                or instance.get("State", {}).get("Name") != "running"
                or instance.get("IamInstanceProfile", {}).get("Arn") != PROFILE
                or any(tags.get(key) != value for key, value in wanted.items())
                or metadata.get("HttpTokens") != "required" or metadata.get("HttpPutResponseHopLimit") != 1):
            raise ValueError("Deployment executor identity, ownership or IMDS controls changed")
        public = ipaddress.ip_address(instance.get("PublicIpAddress", ""))
        if public.version != 4 or not public.is_global:
            raise ValueError("Deployment executor needs its own global public IPv4")
        with self.open_url("https://checkip.amazonaws.com", timeout=15) as stream:
            egress = stream.read(128).decode().strip()
        if egress != str(public):
            raise ValueError("Observed egress differs from the persistent executor public IP")
        return f"{public}/32"

    def command(self, *args):
        return self.run(["terraform", f"-chdir={self.root}", *args], env=self.env, check=True, capture_output=True)

    def initialize(self):
        self.executor()
        self.toolchain()
        self.source_files()
        lock = self.root / ".terraform.lock.hcl"
        if not lock.is_file():
            raise ValueError("Bundled Terraform provider lock is missing")
        self.command("init", "-input=false", "-reconfigure", "-lockfile=readonly",
                     f"-backend-config=bucket={self.bucket}", f"-backend-config=kms_key_id={self.kms}")
        self.backend()

    def backend(self):
        metadata = json.loads((self.work / ".terraform/terraform.tfstate").read_text())
        backend = metadata.get("backend", {})
        cfg = backend.get("config", {})
        expected = dict(bucket=self.bucket, key="staging/terraform.tfstate", region="us-east-1", encrypt=True,
                        use_lockfile=True, kms_key_id=self.kms, allowed_account_ids=[ACCOUNT])
        if backend.get("type") != "s3" or any(cfg.get(key) != value for key, value in expected.items()):
            raise ValueError("Initialized Terraform backend differs from approved control storage")
        if any(cfg.get(key) for key in ("profile", "access_key", "secret_key", "token", "assume_role",
                "assume_role_with_web_identity", "shared_credentials_file", "shared_credentials_files", "shared_config_files",
                "endpoints", "endpoint", "iam_endpoint", "sts_endpoint", "ec2_metadata_service_endpoint",
                "skip_credentials_validation", "skip_region_validation", "skip_requesting_account_id", "skip_metadata_api_check")):
            raise ValueError("Backend credentials must use the deployment executor identity")
        if self.command("workspace", "show").stdout.strip() not in (b"default", "default"):
            raise ValueError("Only the default staging state workspace is allowed")

    def toolchain(self):
        tool = json.loads(self.command("version", "-json").stdout)
        if tool.get("terraform_version") != "1.15.5":
            raise ValueError("Runtime Terraform version differs from the reviewed toolchain")
        return tool

    def source_files(self):
        # Terraform loads these implicitly, before -var-file. Reject hidden
        # local inputs rather than silently merging them into a reviewed plan.
        if any(self.root.glob("*.auto.tfvars*")) or any((self.root / name).exists() for name in ("terraform.tfvars", "terraform.tfvars.json")):
            raise ValueError("Automatic Terraform variable files are forbidden in the immutable staging root")
        files = sorted(set(self.root.glob("*.tf")) | set(self.root.glob("*.tf.json"))
                       | set((self.infra / "modules").rglob("*.tf")) | set((self.infra / "modules").rglob("*.tf.json")))
        if not files:
            raise ValueError("Bundled Terraform configuration is missing")
        for file in files:
            if file.is_symlink() or not file.resolve().is_relative_to(self.infra):
                raise ValueError("Terraform source must stay inside the immutable bundle")
        return files

    def configure(self, stage, *, planning):
        if stage not in ("foundation", "edge"):
            raise ValueError("Unsupported deployment stage")
        variables = deepcopy(self.private["terraformVars"])
        cidr = self.executor()
        if planning:
            variables.update(public_access_cidrs=[cidr], enable_domain_vpn_foundation=True, enable_domain_vpn_edge=stage == "edge")
        elif variables.get("enable_domain_vpn_foundation") is not True or variables.get("enable_domain_vpn_edge") != (stage == "edge"):
            raise ValueError("Persisted stage flags differ from the approved saved plan")
        if not planning and variables.get("public_access_cidrs") != [cidr]:
            raise ValueError("Executor egress changed since planning; a new review is required")
        if not variables.get("public_certificate_arn"):
            raise ValueError("An issued storefront certificate is required before foundation planning")
        if stage == "edge" and any(not variables.get(key) for key in (
                "internal_alb_arn", "client_vpn_server_certificate_arn", "client_vpn_root_certificate_arn")):
            raise ValueError("Edge requires the observed ALB and persisted VPN certificates")
        validate_variables(variables)
        self.variables, self.stage = variables, stage
        self.varfile = self.work / "stage.private.tfvars.json"
        self.varfile.write_bytes(encoded(variables))
        self.varfile.chmod(0o600)

    def persist_variables(self):
        if self.variables is None:
            raise ValueError("Configure a deployment stage first")
        key = f"private/staging/{self.execution['sessionId']}/inputs.json"
        response = self.clients["s3"].get_object(Bucket=self.bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
        current = json.loads(response["Body"].read(256 * 1024 + 1))
        if current != self.private or not response.get("ETag"):
            raise ValueError("Private inputs changed during stage planning")
        updated = dict(current, terraformVars=deepcopy(self.variables))
        self.clients["s3"].put_object(Bucket=self.bucket, Key=key, ExpectedBucketOwner=ACCOUNT,
                                      IfMatch=response["ETag"], Body=encoded(updated), ContentType="application/json")
        self.private = updated

    def config_hash(self):
        if self.variables is None:
            raise ValueError("Stage variables are not configured")
        hasher = hashlib.sha256()
        for file in self.source_files():
            hasher.update(file.relative_to(self.infra).as_posix().encode() + b"\0")
            hasher.update(hashlib.sha256(file.read_bytes()).digest())
        hasher.update(encoded(self.variables))
        return hasher.hexdigest()

    def observe(self):
        self.backend()
        cidr = self.executor()
        tool = self.toolchain()
        payload = self.command("state", "pull").stdout
        if len(payload) > MAX_STATE_BYTES:
            raise ValueError("Observed state exceeds the staging size limit")
        state = validate_state(json.loads(payload))
        if state["lineage"] != self.execution.get("initialState", {}).get("lineage"):
            raise ValueError("Workload state lineage changed since admission")
        return dict(sourceRevisions=deepcopy(self.execution["sourceRevisions"]), approvedBudgetUsd=self.execution["approvedBudgetUsd"],
                    deadline=self.execution["deadline"], cleanupStartAt=self.execution["cleanupStartAt"],
                    configSha256=self.config_hash(), providerLockSha256=hashlib.sha256((self.root / ".terraform.lock.hcl").read_bytes()).hexdigest(),
                    terraformVersion=tool["terraform_version"], backendBucket=self.bucket, backendKey="staging/terraform.tfstate",
                    stateLineage=state["lineage"], stateSerial=state["serial"], executorRoleArn=ROLE, callerPublicCidr=cidr)

    def plan(self):
        before = self.observe()
        path = self.work / f"{self.stage}.private.tfplan"
        args = ["plan", "-input=false", "-no-color", "-lock-timeout=60s", f"-out={path}", f"-var-file={self.varfile}"]
        if self.stage == "foundation": args.append("-refresh=false")
        self.command(*args)
        output = self.command("show", "-json", str(path)).stdout
        plan = json.loads(output)
        if plan.get("variables", {}).get("budget_limit_usd", {}).get("value") != self.execution["approvedBudgetUsd"]:
            raise ValueError("Saved-plan budget differs from the approved session")
        self.run([sys.executable, str(self.infra / "scripts/review-plan.py"), self.stage], input=output,
                 check=True, capture_output=True, env=self.env)
        if self.observe() != before:
            raise ValueError("State, source or executor context changed while creating the plan")
        payload = path.read_bytes()
        digest(payload)
        return payload, before, summarize_changes(plan)

    def apply(self, path, stage):
        if stage != self.stage:
            raise ValueError("Saved plan stage differs from the configured runtime")
        def private_run(command, **kwargs):
            kwargs.setdefault("capture_output", True)
            return self.run(command, env=self.env, **kwargs)
        apply_saved_plan(self.root, path, stage, run=private_run)
