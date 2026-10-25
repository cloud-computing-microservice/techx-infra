"""Bounded SSM dispatch/observation; the remote EC2 worker owns the deploy lease.

An ambiguous SendCommand is never retried. Only a completed SSM invocation plus
the durable remote result permits the next task on the same persistent VM.
"""
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import re
import time
import uuid

from automation_session import claim, release, utc, validate
from cleanup_schedule import verify_existing
from deployment_tasks import (ACCOUNT, DOCUMENT, MANAGER, create_task, executor_config, validate_result)

MANAGER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/techx-automation-lifecycle-manager"
PROFILE = f"arn:aws:iam::{ACCOUNT}:instance-profile/techx-automation-lifecycle-executor"
ACTIVE = {"Pending", "InProgress", "Delayed", "Cancelling"}
TERMINAL = {"Success", "Cancelled", "TimedOut", "Failed"}


def observe_command(ssm, config, pointer):
    command_id = pointer.get("commandId", "")
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", command_id):
        raise ValueError("SSM dispatch result is unknown; never resend this task")
    try:
        result = ssm.get_command_invocation(CommandId=command_id, InstanceId=pointer["instanceId"], PluginName="runTask")
    except ssm.exceptions.InvocationDoesNotExist:
        return None
    if (result.get("CommandId") != command_id or result.get("InstanceId") != pointer["instanceId"]
            or result.get("DocumentName") != DOCUMENT or result.get("DocumentVersion") != config["documentVersion"]
            or result.get("PluginName") != "runTask" or result.get("Status") not in ACTIVE | TERMINAL):
        raise ValueError("Remote command observation is outside the expected invocation")
    return result


def previous_terminal(ssm, config, record):
    previous = record.get("deploymentTask")
    if previous is None:
        return
    result = observe_command(ssm, config, previous)
    if (previous.get("state") != "SUCCEEDED" or result is None or result["Status"] != "Success"
            or result.get("ResponseCode") != 0):
        raise ValueError("Previous remote task is not verified successful/terminal; resume observation or cleanup")
    validate_result(record, previous)


def inspect_instance(ec2, execution, private, *, running=True):
    config = executor_config(execution["deploymentConfig"])
    expected = private.get("deploymentExecutor", {})
    instance_id = expected.get("instanceId", "")
    if not re.fullmatch(r"i-[a-f0-9]{8,17}", instance_id) or expected.get("imageId") != config["imageId"]:
        raise ValueError("Recorded executor is missing or differs from admitted configuration")
    response = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = response.get("Reservations", [])
    if (response.get("NextToken") or len(reservations) != 1 or reservations[0].get("OwnerId") != ACCOUNT
            or len(reservations[0].get("Instances", [])) != 1):
        raise ValueError("Executor inventory is ambiguous")
    instance = reservations[0]["Instances"][0]
    tags = {row["Key"]: row["Value"] for row in instance.get("Tags", [])}
    wanted = dict(Project="techx", Environment="staging", Role="deployment-executor", SessionId=execution["sessionId"])
    if (instance.get("InstanceId") != instance_id or instance.get("ImageId") != config["imageId"]
            or instance.get("InstanceType") != "t3.small" or instance.get("SubnetId") != config["subnetId"]
            or [row.get("GroupId") for row in instance.get("SecurityGroups", [])] != [config["securityGroupId"]]
            or instance.get("IamInstanceProfile", {}).get("Arn") != PROFILE
            or instance.get("MetadataOptions", {}).get("HttpTokens") != "required"
            or instance.get("MetadataOptions", {}).get("HttpPutResponseHopLimit") != 1
            or any(tags.get(key) != value for key, value in wanted.items())
            or instance.get("State", {}).get("Name") not in (("running",) if running else ("pending", "running"))):
        raise ValueError("Executor instance ownership, placement or launch controls changed")
    if running:
        ip = ipaddress.ip_address(instance.get("PublicIpAddress", ""))
        if ip.version != 4 or not ip.is_global:
            raise ValueError("Persistent executor has no stable global IPv4")
    return instance


class DeploymentManager:
    def __init__(self, clients, store, execution, execution_version, bucket, owner, *, clock=None, sleep=time.sleep):
        validate(execution)
        executor_config(execution["deploymentConfig"])
        if not re.fullmatch(MANAGER, owner):
            raise ValueError("Dedicated lifecycle CodeBuild identity required")
        self.clients, self.store, self.execution = clients, store, deepcopy(execution)
        self.version, self.bucket, self.owner = execution_version, bucket, owner
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep

    def current(self):
        identity = self.clients["sts"].get_caller_identity()
        if identity.get("Account") != ACCOUNT or not re.fullmatch(
                rf"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-manager/[^/]+", identity.get("Arn", "")):
            raise ValueError("Only the scoped lifecycle manager role may dispatch deployment work")
        record, etag = self.store.read()
        validate(record)
        for key in ("sessionId", "sourceRevisions", "approvedBudgetUsd", "deadline", "cleanupStartAt", "maximumHours", "action"):
            if record.get(key) != self.execution.get(key):
                raise ValueError("Current session differs from admitted execution")
        if (record.get("executionVersion") != self.version or record["status"] not in ("PLANNING", "PROVISIONING")
                or self.clock() >= utc(record["cleanupStartAt"])):
            raise ValueError("Session is closed for deployment")
        verify_existing(self.clients["scheduler"], record, self.version, self.clock())
        return record, etag

    def verify_document(self):
        config = self.execution["deploymentConfig"]
        response = self.clients["ssm"].describe_document(Name=DOCUMENT, DocumentVersion=config["documentVersion"])
        document = response["Document"]
        if (document.get("Name") != DOCUMENT or document.get("Owner") != ACCOUNT or document.get("Status") != "Active"
                or document.get("DocumentVersion") != config["documentVersion"] or document.get("DocumentType") != "Command"
                or document.get("HashType") != "Sha256" or document.get("Hash") != config["documentSha256"]):
            raise ValueError("SSM document differs from the admitted immutable command")

    def observe(self, pointer):
        return observe_command(self.clients["ssm"], self.execution["deploymentConfig"], pointer)

    def previous_terminal(self, record):
        return previous_terminal(self.clients["ssm"], self.execution["deploymentConfig"], record)

    def request_cleanup(self):
        self.clients["lambda"].invoke(FunctionName=f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher",
            InvocationType="Event", Payload=json.dumps(dict(sessionId=self.execution["sessionId"], executionVersion=self.version,
                                                                       requestedEarly=True)).encode())

    def fail_dispatch(self, task_id):
        record, etag = self.store.read()
        if (record.get("sessionId") == self.execution["sessionId"] and record.get("executionVersion") == self.version
                and record.get("deploymentTask", {}).get("taskId") == task_id
                and record["status"] not in ("DESTROYING", "DESTROYED")):
            updated = deepcopy(record)
            # Preserve a remote terminal failure; no retry may clear an unknown
            # in-flight command simply because SendCommand's response was lost.
            if updated["deploymentTask"].get("state") in ("DISPATCHING", "RUNNING"):
                updated["deploymentTask"]["state"] = "UNCERTAIN"
            updated["status"] = "FAILED_NEEDS_CLEANUP"
            self.store.write(updated, etag)

    def record_command(self, task_id, command_id):
        for attempt in range(4):
            current, etag = self.current()
            pointer = current.get("deploymentTask", {})
            if pointer.get("taskId") != task_id or pointer.get("commandId") not in (None, command_id):
                raise ValueError("Dispatch pointer was superseded")
            if pointer.get("commandId") == command_id:
                return pointer  # Recover a lost conditional-write response.
            updated = deepcopy(current)
            updated["deploymentTask"]["commandId"] = command_id
            try:
                self.store.write(updated, etag)
                return updated["deploymentTask"]
            except Exception:
                if attempt == 3: raise
                # Reread and merge only this field. Never reset RUNNING or a
                # remote result while retrying against a heartbeat/CAS race.

    def dispatch(self, private, mode, stage, attempt):
        record, etag = self.current()
        self.previous_terminal(record)
        instance = inspect_instance(self.clients["ec2"], self.execution, private)
        self.verify_document()
        from lifecycle_stage import validate_planning
        if mode == "plan":
            validate_planning(record, stage, attempt)
            if stage == "edge":
                from deployment_tasks import validate_gitops_checkpoint
                validate_gitops_checkpoint(record)
        elif mode == "bootstrap" and stage == "gitops":
            from deployment_tasks import validate_bootstrap
            validate_bootstrap(record, attempt)
        elif mode == "promote" and stage == "chart":
            from deployment_tasks import validate_promotion
            validate_promotion(record, attempt)
        elif (mode != "apply" or record.get("stages", {}).get(stage, {}).get("state") != "AWAITING_APPROVAL"
                or record["stages"][stage].get("attempt") != attempt or record.get("pendingPlan", {}).get("stage") != stage):
            raise ValueError("Task requires a supported plan or pending apply stage")
        owned = claim(record, self.owner, "deploy", now=self.clock())
        self.store.write(owned, etag)
        epoch, task_id = owned["lease"]["epoch"], uuid.uuid4().hex
        persisted = False
        try:
            current, etag = self.current()
            if current.get("lease", {}).get("owner") != self.owner or current["lease"].get("epoch") != epoch:
                raise ValueError("Manager lost its dispatch preparation lease")
            _, pointer = create_task(self.clients["s3"], self.bucket, current, self.version, instance["InstanceId"],
                self.owner, mode, stage, attempt, task_id, self.clock())
            # One CAS both publishes immutable intent and hands off the lease.
            updated = deepcopy(current)
            updated["deploymentTask"] = pointer
            persisted = True  # CAS may succeed even if its response is lost.
            self.store.write(release(updated, self.owner, epoch), etag)
            self.current()  # Refuse dispatch after cleanup closed the session.
            config = self.execution["deploymentConfig"]
            result = self.clients["ssm"].send_command(InstanceIds=[instance["InstanceId"]], DocumentName=DOCUMENT,
                DocumentVersion=config["documentVersion"], DocumentHash=config["documentSha256"], DocumentHashType="Sha256",
                Parameters=dict(TaskId=[task_id]), Comment=f"techx:{task_id}", TimeoutSeconds=120,
                MaxConcurrency="1", MaxErrors="0", CloudWatchOutputConfig=dict(CloudWatchOutputEnabled=False))
            command = result["Command"]
            command_id = command.get("CommandId", "")
            if (not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", command_id)
                    or command.get("InstanceIds") != [instance["InstanceId"]] or command.get("DocumentName") != DOCUMENT
                    or command.get("DocumentVersion") != config["documentVersion"] or command.get("Parameters") != dict(TaskId=[task_id])):
                raise ValueError("SendCommand did not return the expected fixed task invocation")
            # The worker may already be RUNNING/SUCCEEDED. Merge only commandId;
            # a heartbeat/CAS race must not reset its single-consumption state.
            return self.record_command(task_id, command_id)
        except BaseException:
            if persisted:
                try: self.fail_dispatch(task_id)
                finally: self.request_cleanup()
            raise
        finally:
            current, etag = self.store.read()
            if (current.get("sessionId") == self.execution["sessionId"] and current.get("lease", {}).get("owner") == self.owner
                    and current["lease"].get("epoch") == epoch):
                self.store.write(release(current, self.owner, epoch), etag)

    def wait(self, pointer, *, max_observations=1080):
        if type(max_observations) is not int or not 1 <= max_observations <= 1080:
            raise ValueError("Bounded task observation is required")
        try:
            for _ in range(max_observations):
                record, _ = self.current()
                active = record.get("deploymentTask", {})
                if active.get("taskId") != pointer["taskId"] or active.get("commandId") != pointer.get("commandId"):
                    raise ValueError("Task pointer changed while observing remote work")
                result = self.observe(pointer)
                if result is not None and result["Status"] in TERMINAL:
                    # The remote process can finish between the session read
                    # and this SSM observation. Fetch its terminal checkpoint.
                    record, _ = self.current()
                    active = record.get("deploymentTask", {})
                    if active.get("taskId") != pointer["taskId"] or active.get("commandId") != pointer.get("commandId"):
                        raise ValueError("Task pointer changed before terminal reconciliation")
                    if result["Status"] != "Success" or result.get("ResponseCode") != 0 or active.get("state") != "SUCCEEDED":
                        raise ValueError("Remote task failed or its durable result is unproven")
                    # Do not read apply intent against pendingPlan after it was
                    # consumed. Its already-authenticated pointer supplies only
                    # the expected action/attempt for terminal result validation.
                    validate_result(record, pointer)
                    return active
                self.sleep(5)
            raise TimeoutError("Remote task observation timed out; the same command may still be running")
        except BaseException:
            self.request_cleanup()
            raise
