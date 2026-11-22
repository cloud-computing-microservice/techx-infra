from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import runpy
import unittest

from execution_v2 import ACCOUNT, BUCKET, bind_authorized_window, validate_execution
from migration_v2 import (LegacySnapshot, MigrationExecution, capture_legacy, direct_denial_binding,
                          read_closeout, read_control_execution, read_manifest)
from recovery_v2 import checksum


def pack(key, value, version="exact-version"):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return body, dict(bucket=BUCKET, key=key, versionId=version, sha256=hashlib.sha256(body).hexdigest())


class MigrationContracts(unittest.TestCase):
    def setUp(self):
        f = runpy.run_path(str(Path(__file__).with_name("test-execution-v2.py")))["V2Contracts"](); f.setUp()
        self.staging = deepcopy(f.execution)
        self.sid = "staging-gh-1001"
        self.now = datetime(2026, 11, 22, 1, 2, tzinfo=timezone.utc)
        self.common = dict(sessionId=self.sid, account=ACCOUNT, region="us-east-1", environment="staging",
                           sourceRevisions=deepcopy(f.execution["sourceRevisions"]), approvedBudgetUsd=60, maximumHours=3,
                           action="deploy-window", createdAt="2026-11-09T13:52:00Z", cleanupStartAt="2026-11-09T15:22:00Z", deadline="2026-11-09T16:52:00Z")
        execution = dict(self.common, status="PLANNING")
        eb, self.legacy_ref = pack(f"executions/{self.sid}.json", execution, "legacy-execution-version")
        self.record = dict(self.common, status="FAILED_NEEDS_CLEANUP", executionVersion=self.legacy_ref["versionId"],
                           lifecycleDispatch=dict(id="d" * 64, phase="CALLING", createdAt="2026-11-09T13:52:22.900Z",
                                                  operation=dict(mode="prepare", stage="foundation", attempt=1)))
        sb, sr = pack("automation/staging/session.json", self.record)
        pb, pr = pack("control/staging/current-execution.json", dict(sessionId=self.sid, executionVersion=self.legacy_ref["versionId"]))
        ib, ir = pack(f"private/staging/{self.sid}/inputs.json", dict(schemaVersion=1, sessionId=self.sid, terraformVars={}))
        self.objects = {k: dict(reference=r, body=b) for k, b, r in (("execution", eb, self.legacy_ref), ("session", sb, sr), ("pointer", pb, pr), ("inputs", ib, ir))}
        self.snapshot = self.capture()
        self.grant = dict(schemaVersion=2, windowId=f.execution["windowId"], account=ACCOUNT, region="us-east-1",
                          sourceRevisions=deepcopy(f.execution["sourceRevisions"]), approvedBudgetUsd="60.00",
                          startedAt="2026-11-22T01:00:00Z", cleanupStartAt="2026-11-22T09:00:00Z", deadline="2026-11-22T11:00:00Z",
                          allowedPurposes=["legacy-migration", "staging"], reviewReference=dict(bucket=BUCKET,
                              key=f"automation/v2/windows/{f.execution['windowId']}/review.json", versionId="review-version", sha256="a" * 64))
        self.grant_body, self.grant_ref = pack(f"automation/v2/windows/{f.execution['windowId']}.json", self.grant)
        self.staging["authorizationReference"] = deepcopy(self.grant_ref)
        self.control = dict(schemaVersion=2, executorBackend="ec2-ssm", purpose="legacy-migration", scope="control-only",
                            migrationId="migration-v1-test", account=ACCOUNT, region="us-east-1", windowId=self.grant["windowId"],
                            authorizationReference=deepcopy(self.grant_ref), legacyReferences=self.snapshot.as_dict()["references"],
                            legacySnapshotSha256=checksum(self.snapshot.as_dict()), sourceRevisions=deepcopy(self.grant["sourceRevisions"]),
                            approvedBudgetUsd="60.00", startedAt="2026-11-22T01:00:00Z", cleanupStartAt="2026-11-22T02:00:00Z", deadline="2026-11-22T04:00:00Z",
                            configurationReference=dict(bucket=BUCKET, key="migration/control/config/executor.json", versionId="config-version", sha256="b" * 64),
                            bundleReference=dict(bucket=BUCKET, key=f"migration/control/bundles/{'c'*64}.tar.gz", versionId="bundle-version", sha256="c" * 64))
        self.control_body, self.control_ref = pack("migration/control/migration-v1-test/execution.json", self.control)
        self.bound_control = self.bind_control()
        self.manifest = dict(schemaVersion=2, migrationId=self.control["migrationId"], controlExecutionReference=deepcopy(self.control_ref),
                             legacySnapshotSha256=checksum(self.snapshot.as_dict()), sourceRevisions=deepcopy(self.grant["sourceRevisions"]),
                             windowId=self.grant["windowId"], authorizationReference=deepcopy(self.grant_ref), policy="RETIRE_UNKNOWN_DISPATCH",
                             retiredPrincipals=[f"arn:aws:iam::{ACCOUNT}:role/techx-automation-lifecycle-dispatcher"],
                             disabledTriggers=[f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-cleanup-launcher"],
                             reviewReference=dict(bucket=BUCKET, key="migration/control/migration-v1-test/review.json", versionId="manifest-review", sha256="e" * 64))
        self.manifest_body, self.manifest_ref = pack("migration/control/migration-v1-test/manifest.json", self.manifest)
        names = ("call-history", "dispatch-binding", "authority-retirement", "actors", "workload", "recovery-owner", "worker-terminal")
        self.closeout = dict(schemaVersion=2, account=ACCOUNT, region="us-east-1", migrationId=self.control["migrationId"],
                             controlExecutionReference=deepcopy(self.control_ref), manifestReference=deepcopy(self.manifest_ref),
                             legacyReferences=self.snapshot.as_dict()["references"], legacySnapshotSha256=checksum(self.snapshot.as_dict()),
                             originalDispatchId="d" * 64, originalPhase="CALLING", outcome="UNKNOWN_DISPATCH_AUTHORITY_RETIRED",
                             evidenceReferences={n:dict(bucket=BUCKET, key=f"migration/control/migration-v1-test/evidence/{n}.json", versionId="evidence-version", sha256="f" * 64) for n in names},
                             sealedAt="2026-11-22T01:01:00Z")

    def capture(self, objects=None):
        return capture_legacy(objects if objects is not None else self.objects, session_id=self.sid, execution_ref=self.legacy_ref)

    def change_object(self, name, value):
        objects = deepcopy(self.objects)
        body, ref = pack(objects[name]["reference"]["key"], value, objects[name]["reference"]["versionId"])
        objects[name] = dict(reference=ref, body=body)
        return objects

    def bind_control(self, control=None, grant=None):
        grant_body, grant_ref = pack(self.grant_ref["key"], grant if grant is not None else self.grant)
        value = deepcopy(control if control is not None else self.control)
        if grant is not None and control is None:
            # A genuinely reviewed different grant, so purpose checks below
            # are reached instead of failing only its reference checksum.
            value["authorizationReference"] = deepcopy(grant_ref)
        body, ref = pack("migration/control/migration-v1-test/execution.json", value)
        return read_control_execution(body, ref, self.snapshot, authorization_body=grant_body, authorization_ref=grant_ref)

    def close(self, value=None, manifest=None):
        manifest = manifest if manifest is not None else self.manifest
        _, mr = pack("migration/control/migration-v1-test/manifest.json", manifest)
        body, ref = pack(f"migration/v1/{self.sid}/{self.legacy_ref['versionId']}/closeout.json", value if value is not None else self.closeout)
        return read_closeout(body, ref, self.bound_control, self.control_ref, self.snapshot, manifest, mr, now=self.now)

    def event(self):
        d = self.record["lifecycleDispatch"]
        env = dict(TECHX_SESSION_ID=self.sid, TECHX_EXECUTION_VERSION=self.legacy_ref["versionId"], TECHX_MODE="prepare", TECHX_PLAN_STAGE="foundation", TECHX_PLAN_ATTEMPT="1")
        return dict(eventSource="codebuild.amazonaws.com", eventName="StartBuild", awsRegion="us-east-1", recipientAccountId=ACCOUNT,
                    userIdentity=dict(type="AssumedRole", arn=f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-lifecycle-dispatcher/techx-lifecycle-1001",
                                      sessionContext=dict(sessionIssuer=dict(arn=f"arn:aws:iam::{ACCOUNT}:role/techx-automation-lifecycle-dispatcher"))),
                    errorCode="AccountLimitExceededException", responseElements=None, eventID="event-12345678", requestID="request-12345678",
                    eventTime="2026-11-09T13:52:23Z", requestParameters=dict(projectName="techx-automation-lifecycle", idempotencyToken=d["id"],
                        environmentVariablesOverride=[dict(name=k,value=v,type="PLAINTEXT") for k,v in env.items()]))

    def test_capture_preserves_original_bytes_and_calling(self):
        before = deepcopy(self.objects)
        snapshot = self.capture()
        self.assertEqual(snapshot.as_dict()["originalDispatch"]["phase"], "CALLING")
        self.assertEqual(self.objects, before)
        exposed = snapshot.as_dict(); exposed["originalDispatch"]["phase"] = "PREPARED"
        self.assertEqual(snapshot.as_dict()["originalDispatch"]["phase"], "CALLING")
        with self.assertRaises(ValueError): LegacySnapshot(snapshot.as_dict())

    def test_legacy_snapshot_requires_all_original_exact_objects(self):
        for name in self.objects:
            objects = deepcopy(self.objects); del objects[name]
            with self.subTest(name=name), self.assertRaises(ValueError): self.capture(objects)
            objects = deepcopy(self.objects); objects[name]["reference"]["versionId"] = "null"
            with self.assertRaises(ValueError): self.capture(objects)
        changed = dict(self.record, approvedBudgetUsd=61)
        with self.assertRaises(ValueError): self.capture(self.change_object("session", changed))
        with self.assertRaises(ValueError): self.capture(self.change_object("pointer", dict(sessionId=self.sid, executionVersion="other")))
        with self.assertRaises(ValueError): self.capture(self.change_object("inputs", dict(schemaVersion=2, sessionId=self.sid)))

    def test_control_only_migration_is_separate_from_staging_admission(self):
        self.assertEqual(self.bound_control.as_dict()["scope"], "control-only")
        with self.assertRaises(ValueError): validate_execution(self.control)
        with self.assertRaises(ValueError): MigrationExecution(self.control, self.control_ref)
        for key, value in [("scope", "staging"), ("purpose", "deploy"), ("executorBackend", "codebuild"),
                           ("schemaVersion", True), ("approvedBudgetUsd", "60.01"), ("enable_domain_vpn_foundation", True)]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.bind_control(dict(self.control, **{key:value}))

    def test_control_cannot_change_legacy_scope_or_bundle(self):
        for key, value in [("legacySnapshotSha256", "a"*64), ("legacyReferences", {}),
                           ("bundleReference", dict(self.control["bundleReference"], versionId="null")),
                           ("configurationReference", dict(self.control["configurationReference"], key="staging/config.json"))]:
            with self.subTest(key=key), self.assertRaises(ValueError): self.bind_control(dict(self.control, **{key:value}))

    def test_window_binding_is_shared_scoped_and_cannot_extend_budget_or_time(self):
        self.assertEqual(bind_authorized_window(self.staging, self.grant_body, self.grant_ref, purpose="staging"), self.grant)
        self.assertEqual(validate_execution(self.staging), self.staging)
        for key, value in [("approvedBudgetUsd", "59"), ("deadline", "2026-11-22T11:00:01Z"),
                           ("cleanupStartAt", "2026-11-22T09:00:01Z"), ("startedAt", "2026-11-22T00:59:59Z")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                bind_authorized_window(dict(self.staging, **{key:value}), self.grant_body, self.grant_ref, purpose="staging")
        with self.assertRaisesRegex(ValueError, "outside the approved window purpose"):
            self.bind_control(grant=dict(self.grant, allowedPurposes=["staging"]))
        with self.assertRaises(ValueError): validate_execution(dict(self.staging, authorizationReference=dict(self.grant_ref, versionId="null")))

    def test_window_policy_checks_are_not_just_reference_checks(self):
        restricted = dict(self.grant, allowedPurposes=["legacy-migration"])
        body, ref = pack(self.grant_ref["key"], restricted)
        value = dict(self.staging, authorizationReference=ref)
        with self.assertRaisesRegex(ValueError, "outside the approved window purpose"):
            bind_authorized_window(value, body, ref, purpose="staging")
        # This sub-window obeys its own ten-hour/reserve constraints but extends
        # the already approved parent window. The parent binding must reject it.
        value = dict(self.staging, startedAt="2026-11-22T02:00:00Z", cleanupStartAt="2026-11-22T10:00:00Z", deadline="2026-11-22T12:00:00Z")
        with self.assertRaisesRegex(ValueError, "extends the approved cloud window"):
            bind_authorized_window(value, self.grant_body, self.grant_ref, purpose="staging")
        with self.assertRaises(ValueError):
            bind_authorized_window(self.staging, self.grant_body, dict(self.grant_ref, versionId="another-version"), purpose="staging")

    def test_direct_denial_binds_exact_token_environment_service_and_run(self):
        result = direct_denial_binding(self.event(), self.snapshot, now=self.now)
        self.assertEqual(result["classification"], "EXACT_SERVICE_DENIAL_ONLY")
        self.assertNotIn("noBuildExists", result)
        event = self.event(); event["eventTime"] = "2026-11-09T13:52:22Z"
        self.assertEqual(direct_denial_binding(event, self.snapshot, now=self.now)["dispatchId"], "d"*64)

    def test_redacted_or_mismatched_fields_never_prove_rejected_creation(self):
        for key, value in [("idempotencyToken", "HIDDEN_DUE_TO_SECURITY_REASONS"), ("projectName", "other-project"),
                           ("environmentVariablesOverride", [dict(name="HIDDEN_DUE_TO_SECURITY_REASONS",value="HIDDEN_DUE_TO_SECURITY_REASONS",type="PLAINTEXT")])]:
            event = self.event(); event["requestParameters"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): direct_denial_binding(event, self.snapshot, now=self.now)
        for key, value in [("awsRegion", "us-west-2"), ("recipientAccountId", "111111111111"), ("errorCode", "AccessDenied"),
                           ("responseElements", {"build":{}}), ("eventTime", "2026-11-22T01:02:01Z")]:
            with self.subTest(key=key), self.assertRaises(ValueError): direct_denial_binding(dict(self.event(), **{key:value}), self.snapshot, now=self.now)
        event = self.event()
        event["requestParameters"]["environmentVariablesOverride"][0]["value"] = "HIDDEN_DUE_TO_SECURITY_REASONS"
        with self.assertRaisesRegex(ValueError, "mismatched environment"):
            direct_denial_binding(event, self.snapshot, now=self.now)

    def test_manifest_requires_explicit_reviewed_unknown_retirement_policy(self):
        self.assertEqual(read_manifest(self.manifest_body, self.manifest_ref, self.bound_control, self.control_ref, self.snapshot), self.manifest)
        for key, value in [("policy", "force-complete"), ("retiredPrincipals", []),
                           ("retiredPrincipals", [f"arn:aws:iam::{ACCOUNT}:role/techx-automation-v2-finalizer"]),
                           ("retiredPrincipals", [f"arn:aws:iam::{ACCOUNT}:role/techx-automation-v2"]),
                           ("disabledTriggers", [f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:techx-automation-v2-cleanup"]),
                           ("legacySnapshotSha256", "f"*64)]:
            body, ref = pack(self.manifest_ref["key"], dict(self.manifest, **{key:value}))
            with self.subTest(key=key), self.assertRaises(ValueError): read_manifest(body, ref, self.bound_control, self.control_ref, self.snapshot)
        with self.assertRaises(ValueError): read_manifest(self.manifest_body, self.manifest_ref, self.control, self.control_ref, self.snapshot)

    def test_closeout_preserves_unknown_as_unknown_and_does_not_complete_v1(self):
        before = deepcopy(self.objects)
        value = self.close()
        self.assertEqual(value["outcome"], "UNKNOWN_DISPATCH_AUTHORITY_RETIRED")
        self.assertEqual(value["originalPhase"], "CALLING")
        self.assertEqual(self.objects, before)
        for changes in [dict(originalPhase="PREPARED"), dict(originalDispatchId="f"*64), dict(originalStatus="DESTROYED"),
                        dict(outcome="REJECTED_BEFORE_BUILD_CREATION"), dict(sealedAt="2026-11-22T01:02:01Z")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.close(dict(self.closeout, **changes))

    def test_closeout_requires_all_scoped_private_evidence_references(self):
        for name in self.closeout["evidenceReferences"]:
            changed = deepcopy(self.closeout); del changed["evidenceReferences"][name]
            with self.subTest(name=name), self.assertRaises(ValueError): self.close(changed)
        changed = deepcopy(self.closeout); changed["evidenceReferences"]["actors"]["versionId"] = "null"
        with self.assertRaises(ValueError): self.close(changed)
        changed = deepcopy(self.closeout); changed["evidenceReferences"]["workload"]["key"] = "other-project/zero.json"
        with self.assertRaises(ValueError): self.close(changed)


if __name__ == "__main__":
    unittest.main()
