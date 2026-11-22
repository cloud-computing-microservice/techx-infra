"""Typed v2 identities and observation handles, never an authorization bridge.

The trusted bridge supplies verified OIDC claims, STS GetCallerIdentity and a
GitHub API context independently of task payloads. These pure validators do not
verify JWT signatures or turn caller-supplied JSON into service evidence.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json

from execution_v2 import ACCOUNT, REGION, PREFIX, execution_reference, fields, integer, reference, text, timestamp, validate_execution

ORG = "cloud-computing-microservice"
REPO = f"{ORG}/techx-infra"
WORKFLOWS = {"staging-lifecycle.yml", "staging-continue.yml", "staging-orchestrate.yml", "staging-acceptance.yml",
             "staging-destroy.yml", "staging-plan-review.yml"}
ENVIRONMENTS = {"staging-lifecycle.yml": {"staging-start", "staging-lifecycle"},
                "staging-continue.yml": {"staging-lifecycle"}, "staging-orchestrate.yml": {"staging-lifecycle"},
                "staging-acceptance.yml": {"staging-acceptance"}, "staging-destroy.yml": {"staging-destroy"},
                "staging-plan-review.yml": {"staging-plan-read", "staging-apply"}}
PURPOSES = {"deployment", "cleanup", "probe"}
OPERATIONS = {("prepare", "foundation"), ("plan", "foundation"), ("apply", "foundation"),
              ("bootstrap", "gitops"), ("promote", "chart"), ("plan", "edge"), ("apply", "edge"),
              ("activate", "exposure"), ("verify", "private"), ("verify", "active"), ("verify", "rollback"),
              ("verify", "public"), ("release", "active"), ("rollback", "prepare"), ("rollback", "rollout"),
              ("cleanup", "all")}


def launch_token(intent):
    """Deterministic EC2 idempotency key includes the exact execution version."""
    context = {k: v for k, v in intent.items() if k != "clientToken"}
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def manager_identity(context, claims, caller, execution):
    validate_execution(execution)
    fields(context, {"repositoryId", "ownerId", "workflow", "job", "runId", "runAttempt", "environment", "headSha"})
    for key in ("repositoryId", "ownerId", "runId"):
        text(context[key], r"[1-9][0-9]{0,19}")
    if (context["repositoryId"], context["ownerId"]) != ("1322975868", "328679702"):
        raise ValueError("Repository or owner differs from the reviewed immutable IDs")
    if type(context["runAttempt"]) is not int or context["runAttempt"] != 1:
        raise ValueError("Only the first workflow attempt may mutate a session")
    if context["workflow"] not in WORKFLOWS:
        raise ValueError("Workflow is outside the reviewed manager allowlist")
    text(context["job"], r"[a-zA-Z0-9_-]{1,64}")
    if context["environment"] not in ENVIRONMENTS[context["workflow"]]:
        raise ValueError("Environment differs from the reviewed workflow binding")
    if context["headSha"] != execution["sourceRevisions"]["techx-infra"]:
        raise ValueError("Manager source differs from the admitted infra source")
    expected = {"repository": REPO, "repository_id": context["repositoryId"], "repository_owner_id": context["ownerId"],
                "ref": "refs/heads/main", "sha": context["headSha"], "run_id": context["runId"], "run_attempt": "1",
                "environment": context["environment"], "aud": "sts.amazonaws.com",
                "workflow_ref": f"{REPO}/.github/workflows/{context['workflow']}@refs/heads/main",
                "workflow_sha": context["headSha"]}
    if not isinstance(claims, dict) or any(claims.get(k) != v for k, v in expected.items()):
        raise ValueError("Verified OIDC claims differ from the trusted GitHub context")
    # Subject uses the immutable owner/repository IDs already required by the
    # organization OIDC customization. Never accept the default name-only sub.
    expected_sub = (f"repo:{ORG}@{context['ownerId']}/techx-infra@{context['repositoryId']}:"
                    f"environment:{context['environment']}")
    if claims.get("sub") != expected_sub:
        raise ValueError("OIDC subject differs from the immutable environment binding")
    role_session = f"techx-v2-{context['runId']}-1"
    arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-manager/{role_session}"
    if not isinstance(caller, dict) or caller.get("Account") != ACCOUNT or caller.get("Arn") != arn:
        raise ValueError("Manager requires its exact OIDC STS role session")
    return dict(kind="github-manager", **deepcopy(context), principalArn=arn)


def validate_launch_intent(intent, execution, ref, epoch):
    """Validate immutable intent only; pinned configuration authorization is separate."""
    validate_execution(execution)
    execution_reference(ref, execution["sessionId"])
    integer(epoch)
    fields(intent, {"schemaVersion", "executionReference", "leaseEpoch", "purpose", "attempt", "clientToken",
                    "createdAt", "launchTemplateId", "launchTemplateVersion", "imageId", "instanceProfileArn",
                    "subnetId", "securityGroupId", "configurationReference"})
    if (type(intent["schemaVersion"]) is not int or intent["schemaVersion"] != 2
            or intent["executionReference"] != ref or type(intent["leaseEpoch"]) is not int
            or intent["leaseEpoch"] != epoch or intent["purpose"] not in PURPOSES):
        raise ValueError("Launch intent belongs to another execution or fence")
    integer(intent["attempt"], maximum=3 if intent["purpose"] == "cleanup" else 1)
    text(intent["clientToken"], r"[a-f0-9]{64}")
    if intent["clientToken"] != launch_token(intent):
        raise ValueError("Launch token is not bound to the immutable launch context")
    text(intent["launchTemplateId"], r"lt-[a-f0-9]{17}")
    text(intent["launchTemplateVersion"], r"[1-9][0-9]{0,8}")
    text(intent["imageId"], r"ami-[a-f0-9]{17}")
    text(intent["subnetId"], r"subnet-[a-f0-9]{17}")
    text(intent["securityGroupId"], r"sg-[a-f0-9]{17}")
    profile = f"arn:aws:iam::{ACCOUNT}:instance-profile/techx-automation-v2-{intent['purpose']}-executor"
    if intent["instanceProfileArn"] != profile:
        raise ValueError("Unexpected v2 worker profile")
    timestamp(intent["createdAt"])
    reference(intent["configurationReference"], key=f"{PREFIX}config/{intent['purpose']}.json")
    return deepcopy(intent)


def worker_identity(intent, instance, caller, execution, ref, epoch):
    """Bind a trusted DescribeInstances/STS response to the admitted launch intent.

    Positive observation is required; an EC2-looking ARN alone is insufficient.
    The lifecycle launcher separately validates intent against pinned config and
    CAS before RunInstances. `epoch` is the registered launch epoch, not a new
    task lease. A warm cleanup worker survives the cleanup fence; authorization
    of its later task epoch belongs to the live CAS registry. This function
    cannot authorize arbitrary config or prove a resumed VM is the same boot.
    """
    validate_launch_intent(intent, execution, ref, epoch)
    profile = intent["instanceProfileArn"]
    if not isinstance(instance, dict):
        raise ValueError("Positive EC2 observation required")
    instance_id = text(instance.get("InstanceId"), r"i-[a-f0-9]{17}")
    template = instance.get("LaunchTemplate", {})
    observed_tags = instance.get("Tags", [])
    if (not isinstance(observed_tags, list) or any(not isinstance(tag, dict) or set(tag) != {"Key", "Value"} for tag in observed_tags)
            or len({tag["Key"] for tag in observed_tags}) != len(observed_tags)):
        raise ValueError("Invalid or duplicate instance tags")
    tags = {tag["Key"]: tag["Value"] for tag in observed_tags}
    expected_tags = dict(Project="TechX", Environment="automation", Role=f"{intent['purpose']}-executor",
                         SessionId=execution["sessionId"], ExecutionSha256=ref["sha256"], LeaseEpoch=str(epoch))
    groups = instance.get("SecurityGroups")
    if (any(tags.get(k) != v for k, v in expected_tags.items())
            or instance.get("ClientToken") != intent["clientToken"]
            or instance.get("State", {}).get("Name") != "running"
            or instance.get("ImageId") != intent["imageId"] or instance.get("SubnetId") != intent["subnetId"]
            or template.get("LaunchTemplateId") != intent["launchTemplateId"]
            or template.get("Version") != intent["launchTemplateVersion"]
            or instance.get("IamInstanceProfile", {}).get("Arn") != profile
            or instance.get("MetadataOptions", {}).get("HttpTokens") != "required"
            or instance.get("MetadataOptions", {}).get("State") != "applied"
            or instance.get("KeyName") or not isinstance(groups, list) or len(groups) != 1
            or not isinstance(groups[0], dict) or groups[0].get("GroupId") != intent["securityGroupId"]):
        raise ValueError("Observed instance differs from the admitted launch intent")
    launched = instance.get("LaunchTime")
    # boto3 returns an aware datetime. Do not trust a timestamp copied from task JSON.
    from datetime import datetime
    if not isinstance(launched, datetime) or launched.tzinfo is None or launched < timestamp(intent["createdAt"]):
        raise ValueError("Instance predates the launch intent or has no service launch time")
    arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-{intent['purpose']}-executor/{instance_id}"
    if not isinstance(caller, dict) or caller.get("Account") != ACCOUNT or caller.get("Arn") != arn:
        raise ValueError("Worker requires its exact instance role session")
    try:
        address = ipaddress.ip_address(instance.get("PublicIpAddress", ""))
    except ValueError as error:
        raise ValueError("Worker requires an observed stable public IPv4") from error
    if address.version != 4 or not address.is_global:
        raise ValueError("Worker public IPv4 must be globally routable")
    return dict(kind="ec2-worker", purpose=intent["purpose"], instanceId=instance_id,
                executionReference=deepcopy(ref), launchEpoch=epoch, launchToken=intent["clientToken"], attempt=intent["attempt"],
                configurationReference=deepcopy(intent["configurationReference"]),
                launchedAt=launched.isoformat(), principalArn=arn, publicCidr=f"{address}/32")


def validate_handle(handle, task, worker, *, command, now):
    """Observe a single consumed SSM command; expiry never permits resend.

    `task` is read from the private immutable task version, `worker` comes from
    worker_identity and `command` is an AWS Get/ListCommands observation. The
    dispatcher owns CAS consumption and receipt persistence, not this validator.
    """
    fields(task, {"schemaVersion", "taskId", "sessionId", "executionReference", "leaseEpoch", "instanceId",
                  "purpose", "operation", "stage", "attempt", "documentName", "documentVersion", "documentSha256", "createdAt", "expiresAt"})
    fields(handle, {"schemaVersion", "taskId", "executionReference", "leaseEpoch", "instanceId", "operation", "stage", "attempt",
                    "documentName", "documentVersion", "documentSha256", "commandId"})
    if (type(task["schemaVersion"]) is not int or task["schemaVersion"] != 2
            or type(handle["schemaVersion"]) is not int or handle["schemaVersion"] != 2):
        raise ValueError("SSM task requires the v2 contract")
    execution_reference(task["executionReference"], task["sessionId"])
    text(task["taskId"], r"[a-f0-9]{32}")
    text(task["instanceId"], r"i-[a-f0-9]{17}")
    integer(task["leaseEpoch"])
    integer(task["attempt"], maximum=999999 if task["operation"] in ("plan", "apply") else 1)
    if task["purpose"] not in PURPOSES or (task["operation"], task["stage"]) not in OPERATIONS:
        raise ValueError("Unknown typed task operation")
    if ((task["purpose"] == "cleanup") != (task["operation"] == "cleanup")
            or (task["purpose"] == "probe") != ((task["operation"], task["stage"]) == ("verify", "public"))):
        raise ValueError("Task operation is outside worker purpose")
    if task["documentName"] != f"techx-automation-v2-{task['purpose']}-task":
        raise ValueError("Unexpected SSM document")
    text(task["documentVersion"], r"[1-9][0-9]{0,8}")
    text(task["documentSha256"], r"[a-f0-9]{64}")
    text(handle["commandId"], r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}")
    if (not isinstance(worker, dict) or worker.get("kind") != "ec2-worker"
            or type(worker.get("launchEpoch")) is not int or not 1 <= worker["launchEpoch"] <= task["leaseEpoch"]
            or any(worker.get(k) != task[k] for k in ("instanceId", "purpose", "executionReference"))
            or any(handle[k] != task[k] or type(handle[k]) is not type(task[k]) for k in set(handle) - {"commandId"})):
        raise ValueError("Observation handle differs from the exact worker/task binding")
    created, expires = timestamp(task["createdAt"]), timestamp(task["expiresAt"])
    from datetime import datetime, timedelta
    if now.tzinfo is None or now < created or not created < expires or expires - created > timedelta(minutes=45):
        raise ValueError("Invalid SSM task time window")
    if (not isinstance(command, dict) or command.get("CommandId") != handle["commandId"]
            or command.get("InstanceIds") != [task["instanceId"]] or command.get("Targets")
            or command.get("DocumentName") != task["documentName"]
            or command.get("DocumentVersion") != task["documentVersion"]
            or command.get("Parameters") != {"TaskId": [task["taskId"]]}
            or command.get("Status") not in {"Pending", "InProgress", "Success", "Cancelled", "Failed", "TimedOut", "Cancelling"}):
        raise ValueError("SSM service observation differs from the single typed command")
    requested = command.get("RequestedDateTime")
    if (not isinstance(requested, datetime) or requested.tzinfo is None
            or not created <= requested < expires or requested > now):
        raise ValueError("SSM command was not dispatched during its admitted window")
    # The original dispatch deadline limits SendCommand admission. Observation
    # of an already consumed command remains valid after timeout/expiry.
    return deepcopy(handle)
