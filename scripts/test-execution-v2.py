"""Cross-session, spoofed-identity and legacy isolation regression tests."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest

from automation_session import claim, validate as validate_legacy
from execution_v2 import (ACCOUNT, BUCKET, decode_execution, execution_reference, read_legacy,
                          validate_execution, validate_session)
from worker_identity import launch_token, manager_identity, validate_handle, worker_identity


class V2Contracts(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 1, tzinfo=timezone.utc)
        self.execution = dict(schemaVersion=2, executorBackend="ec2-ssm", account=ACCOUNT, region="us-east-1",
                              environment="staging", sessionId="staging-v2-contract-test",
                              sourceRevisions={"techx-infra": "a" * 40, "techx-chart": "b" * 40, "techx-platform": "c" * 40},
                              approvedBudgetUsd="60.00", startedAt="2026-11-22T01:00:00Z",
                              cleanupStartAt="2026-11-22T09:00:00Z", deadline="2026-11-22T11:00:00Z", initialEpoch=1)
        self.body = json.dumps(self.execution).encode()
        self.ref = dict(bucket=BUCKET, key="automation/v2/staging/executions/staging-v2-contract-test.json",
                        versionId="exact-object-version", sha256=hashlib.sha256(self.body).hexdigest())
        self.session = dict(schemaVersion=2, executorBackend="ec2-ssm", sessionId=self.execution["sessionId"],
                            executionReference=deepcopy(self.ref), status="PLANNING", leaseEpoch=1)
        self.context = dict(repositoryId="1322975868", ownerId="328679702", workflow="staging-lifecycle.yml",
                            job="manager", runId="123456", runAttempt=1, environment="staging-lifecycle", headSha="a" * 40)
        self.claims = dict(repository="cloud-computing-microservice/techx-infra", repository_id="1322975868",
                           repository_owner_id="328679702", ref="refs/heads/main", sha="a" * 40, run_id="123456",
                           run_attempt="1", environment="staging-lifecycle", aud="sts.amazonaws.com",
                           workflow_ref="cloud-computing-microservice/techx-infra/.github/workflows/staging-lifecycle.yml@refs/heads/main",
                           workflow_sha="a" * 40,
                           sub="repo:cloud-computing-microservice@328679702/techx-infra@1322975868:environment:staging-lifecycle")
        self.manager_caller = dict(Account=ACCOUNT,
                                   Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-manager/techx-v2-123456-1")
        self.intent = dict(schemaVersion=2, executionReference=deepcopy(self.ref), leaseEpoch=1, purpose="deployment",
                           attempt=1, clientToken="d" * 64, createdAt="2026-11-22T01:00:00Z", launchTemplateId="lt-" + "1" * 17,
                           launchTemplateVersion="3", imageId="ami-" + "2" * 17,
                           instanceProfileArn=f"arn:aws:iam::{ACCOUNT}:instance-profile/techx-automation-v2-deployment-executor",
                           subnetId="subnet-" + "3" * 17, securityGroupId="sg-" + "4" * 17)
        self.intent["clientToken"] = launch_token(self.intent)
        self.instance = dict(InstanceId="i-" + "5" * 17, ClientToken=self.intent["clientToken"], State=dict(Name="running"),
                             LaunchTemplate=dict(LaunchTemplateId=self.intent["launchTemplateId"], Version="3"),
                             ImageId=self.intent["imageId"], SubnetId=self.intent["subnetId"],
                             IamInstanceProfile=dict(Arn=self.intent["instanceProfileArn"]),
                             MetadataOptions=dict(HttpTokens="required", State="applied"),
                             SecurityGroups=[dict(GroupId=self.intent["securityGroupId"], GroupName="any-service-name")],
                             LaunchTime=self.now, PublicIpAddress="8.8.8.8",
                             Tags=[dict(Key=k, Value=v) for k, v in dict(Project="TechX", Environment="automation",
                                   Role="deployment-executor", SessionId=self.execution["sessionId"],
                                   ExecutionSha256=self.ref["sha256"], LeaseEpoch="1").items()])
        self.worker_caller = dict(Account=ACCOUNT, Arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-deployment-executor/{self.instance['InstanceId']}")
        self.task = dict(schemaVersion=2, taskId="6" * 32, sessionId=self.execution["sessionId"],
                         executionReference=deepcopy(self.ref), leaseEpoch=1, instanceId=self.instance["InstanceId"],
                         purpose="deployment", operation="plan", attempt=1, documentName="techx-automation-v2-deployment-task",
                         documentVersion="3", documentSha256="7" * 64, createdAt="2026-11-22T01:00:00Z", expiresAt="2026-11-22T01:30:00Z")
        self.handle = {k: deepcopy(v) for k, v in self.task.items() if k not in {"sessionId", "purpose", "createdAt", "expiresAt"}}
        self.handle["commandId"] = "12345678-1234-1234-1234-123456789abc"
        self.command = dict(CommandId=self.handle["commandId"], InstanceIds=[self.task["instanceId"]],
                            DocumentName=self.task["documentName"], DocumentVersion=self.task["documentVersion"],
                            Parameters={"TaskId": [self.task["taskId"]]}, Status="InProgress", RequestedDateTime=self.now)

    def worker(self, intent=None, instance=None, caller=None, epoch=1):
        return worker_identity(intent if intent is not None else self.intent,
                               instance if instance is not None else self.instance,
                               caller if caller is not None else self.worker_caller, self.execution, self.ref, epoch)

    def test_exact_execution_bytes_and_copy_isolation(self):
        value = decode_execution(self.body, self.ref, self.execution["sessionId"])
        self.assertEqual(value, self.execution)
        value["sourceRevisions"]["techx-infra"] = "f" * 40
        self.assertEqual(self.execution["sourceRevisions"]["techx-infra"], "a" * 40)
        with self.assertRaises(ValueError):
            decode_execution(json.dumps(self.execution, indent=2).encode(), self.ref, self.execution["sessionId"])

    def test_execution_rejects_scope_backend_version_and_extra_fields(self):
        for key, value in [("schemaVersion", True), ("schemaVersion", 1), ("executorBackend", "codebuild"),
                           ("account", "111111111111"), ("region", "us-west-2"), ("environment", "production"),
                           ("initialEpoch", True), ("initialEpoch", 2), ("sessionId", "staging-gh-123"), ("buildId", "fake")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_execution(dict(self.execution, **{key: value}))

    def test_budget_and_cleanup_reserve_are_fail_closed(self):
        for value in [0, True, 60, 60.0, "0", "60.01", "NaN", "Infinity", "1e1", "-1", "060", "10.001"]:
            with self.subTest(budget=value), self.assertRaises(ValueError):
                validate_execution(dict(self.execution, approvedBudgetUsd=value))
        for key, value in [("deadline", "2026-11-22T11:00:01Z"), ("cleanupStartAt", "2026-11-22T09:00:01Z"),
                           ("startedAt", "2026-11-22T09:00:00Z"), ("deadline", "2026-11-22T11:00:00"),
                           ("deadline", "2026-11-32T11:00:00Z"), ("deadline", "2026-11-22T11:00:00+00:00")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_execution(dict(self.execution, **{key: value}))

    def test_exact_three_source_revisions(self):
        for revisions in [{}, dict(self.execution["sourceRevisions"], **{"techx-infra": "main"}),
                          dict(self.execution["sourceRevisions"], unreviewed="a" * 40)]:
            with self.subTest(revisions=revisions), self.assertRaises(ValueError):
                validate_execution(dict(self.execution, sourceRevisions=revisions))

    def test_reference_cannot_use_current_pointer_null_version_or_legacy_prefix(self):
        for key, value in [("bucket", "another-bucket"), ("key", "automation/staging/execution.json"),
                           ("key", "automation/v2/staging/session.json"), ("versionId", "null"), ("versionId", "latest"),
                           ("versionId", ""), ("versionId", "a\nb"), ("sha256", "A" * 64)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                execution_reference(dict(self.ref, **{key: value}), self.execution["sessionId"])

    def test_duplicate_json_and_cross_session_rejected_even_with_correct_hash(self):
        bodies = [self.body[:-1] + b', "initialEpoch": 1}', b'[]', self.body.replace(b'contract-test', b'other-session')]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                decode_execution(body, dict(self.ref, sha256=hashlib.sha256(body).hexdigest()), self.execution["sessionId"])

    def test_session_does_not_allow_implicit_upgrade_or_new_execution(self):
        self.assertEqual(validate_session(self.session, self.execution, self.ref), self.session)
        for key, value in [("leaseEpoch", True), ("leaseEpoch", 0), ("status", "OFF"), ("schemaVersion", 1),
                           ("executionReference", dict(self.ref, versionId="different-version")), ("lease", {})]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_session(dict(self.session, **{key: value}), self.execution, self.ref)

    def test_legacy_reader_preserves_calling_and_never_upgrades(self):
        old = dict(account=ACCOUNT, region="us-east-1", environment="staging", sessionId="staging-gh-42628903617",
                   status="FAILED_NEEDS_CLEANUP", cleanupStartAt="2026-11-09T22:00:00Z", deadline="2026-11-09T23:00:00Z",
                   lifecycleDispatch=dict(phase="CALLING", id="d" * 64))
        body = json.dumps(old).encode()
        copy = read_legacy(body)
        self.assertEqual(copy, old)
        copy["lifecycleDispatch"]["phase"] = "PREPARED"
        self.assertEqual(old["lifecycleDispatch"]["phase"], "CALLING")
        self.assertEqual(read_legacy(body)["lifecycleDispatch"]["phase"], "CALLING")
        for changed in [dict(old, schemaVersion=2), dict(old, executorBackend="ec2-ssm")]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                read_legacy(json.dumps(changed).encode())
            with self.assertRaises(ValueError):
                claim(changed, "fake:owner", "cleanup", self.now)
        with self.assertRaises(ValueError):
            validate_legacy(self.session)

    def test_manager_requires_matching_oidc_and_sts_service_observations(self):
        value = manager_identity(self.context, self.claims, self.manager_caller, self.execution)
        self.assertEqual(value["kind"], "github-manager")
        for key in self.claims:
            changed = dict(self.claims, **{key: "wrong"})
            with self.subTest(claim=key), self.assertRaises(ValueError):
                manager_identity(self.context, changed, self.manager_caller, self.execution)
        for caller in [{}, self.worker_caller, dict(self.manager_caller, Account="111111111111"),
                       dict(self.manager_caller, Arn=f"arn:aws:codebuild:us-east-1:{ACCOUNT}:build/techx-automation-lifecycle:fake")]:
            with self.subTest(caller=caller), self.assertRaises(ValueError):
                manager_identity(self.context, self.claims, caller, self.execution)

    def test_manager_rejects_rerun_unreviewed_job_source_and_environment(self):
        for key, value in [("runAttempt", 2), ("runAttempt", True), ("repositoryId", "1234"), ("ownerId", "1234"),
                           ("workflow", "verify.yml"), ("job", "shell;command"), ("environment", "staging-arbitrary"), ("headSha", "f" * 40)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                manager_identity(dict(self.context, **{key: value}), self.claims, self.manager_caller, self.execution)

    def test_worker_requires_positive_exact_service_identity(self):
        value = self.worker()
        self.assertEqual(value["instanceId"], self.instance["InstanceId"])
        self.assertEqual(value["publicCidr"], "8.8.8.8/32")
        for key, changed in [("ClientToken", "e" * 64), ("InstanceId", "i-" + "e" * 17), ("ImageId", "ami-" + "e" * 17),
                             ("SubnetId", "subnet-" + "e" * 17), ("State", dict(Name="terminated")),
                             ("LaunchTemplate", dict(LaunchTemplateId=self.intent["launchTemplateId"], Version="$Latest")),
                             ("IamInstanceProfile", {}), ("MetadataOptions", dict(HttpTokens="optional", State="applied")),
                             ("Tags", []), ("KeyName", "ssh-key"), ("SecurityGroups", []),
                             ("PublicIpAddress", "10.0.0.1"), ("PublicIpAddress", "::1"),
                             ("LaunchTime", self.now - timedelta(seconds=1)), ("LaunchTime", self.now.isoformat())]:
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.worker(instance=dict(self.instance, **{key: changed}))
        with self.assertRaises(ValueError):
            self.worker(caller=self.manager_caller)
        with self.assertRaises(ValueError):
            self.worker(instance={})

    def test_worker_rejects_cross_session_stale_fence_and_unpinned_intent(self):
        for key, value in [("leaseEpoch", True), ("leaseEpoch", 2), ("attempt", 2), ("purpose", "codebuild"),
                           ("executionReference", dict(self.ref, versionId="other-version")), ("launchTemplateVersion", "$Default"),
                           ("instanceProfileArn", "arn:aws:iam::058114477594:instance-profile/Administrator")]:
            with self.subTest(field=key), self.assertRaises(ValueError):
                self.worker(intent=dict(self.intent, **{key: value}))
        with self.assertRaises(ValueError):
            self.worker(epoch=2)

    def test_duplicate_identity_tag_rejected(self):
        instance = deepcopy(self.instance)
        instance["Tags"].append(dict(Key="SessionId", Value=self.execution["sessionId"]))
        with self.assertRaises(ValueError):
            self.worker(instance=instance)

    def test_observe_same_command_after_timeout_without_new_dispatch(self):
        observed = validate_handle(self.handle, self.task, self.worker(), command=self.command, now=self.now + timedelta(hours=2))
        self.assertEqual(observed["commandId"], self.handle["commandId"])
        self.assertEqual(observed, self.handle)

    def test_ssm_handle_cannot_rebind_any_field(self):
        for key in self.handle:
            if key == "commandId":
                continue  # The service-observation cases below verify the command ID.
            changed = dict(self.handle, **{key: "wrong"})
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_handle(changed, self.task, self.worker(), command=self.command, now=self.now)
        for value in ["", "new-command", "12345678-1234-1234-1234-123456789abg",
                      "abcdefab-1234-1234-1234-123456789abc"]:
            with self.subTest(command=value), self.assertRaises(ValueError):
                validate_handle(dict(self.handle, commandId=value), self.task, self.worker(), command=self.command, now=self.now)

    def test_ssm_command_bound_to_worker_and_typed_document(self):
        for key, value in [("instanceId", "i-" + "f" * 17), ("launchEpoch", 2), ("kind", "github-manager"),
                           ("executionReference", dict(self.ref, versionId="other-version"))]:
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_handle(self.handle, self.task, dict(self.worker(), **{key: value}), command=self.command, now=self.now)
        for key, value in [("documentName", "AWS-RunShellScript"), ("documentVersion", "$Latest"),
                           ("operation", "cleanup"), ("purpose", "probe"), ("attempt", True),
                           ("expiresAt", "2026-11-22T01:46:00Z"), ("expiresAt", "2026-11-22T00:59:00Z")]:
            task = dict(self.task, **{key: value})
            handle = dict(self.handle, **({key: value} if key in self.handle else {}))
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_handle(handle, task, self.worker(), command=self.command, now=self.now)

    def test_positive_ssm_observation_cannot_target_another_task_or_instance(self):
        changes = [("InstanceIds", [self.task["instanceId"], "i-" + "f" * 17]),
                   ("Targets", [dict(Key="tag:Project", Values=["TechX"])]),
                   ("DocumentName", "AWS-RunShellScript"), ("DocumentVersion", "4"),
                   ("Parameters", {"TaskId": ["f" * 32]}), ("Parameters", {"commands": ["echo fake"]}),
                   ("Status", "Unknown"), ("RequestedDateTime", self.now - timedelta(seconds=1)),
                   ("RequestedDateTime", self.now + timedelta(hours=1))]
        for key, value in changes:
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_handle(self.handle, self.task, self.worker(),
                                command=dict(self.command, **{key: value}), now=self.now)

    def test_launch_epoch_is_not_confused_with_a_later_task_fence(self):
        # Format validation allows the same warm worker after a fence. Runtime
        # CAS must still authorize the later task; observation is not authority.
        task = dict(self.task, leaseEpoch=2)
        handle = dict(self.handle, leaseEpoch=2)
        self.assertEqual(validate_handle(handle, task, self.worker(), command=self.command, now=self.now), handle)


if __name__ == "__main__":
    unittest.main()
