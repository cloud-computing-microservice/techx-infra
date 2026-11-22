from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest

from legacy_reconciliation import (ACCOUNT, BUCKET, ENDPOINTS, KMS_KEY, PROJECTS, REGION,
                                   CollectedLegacy, IncompleteAudit, LegacyCollector, PrivateEvidence, ReadOnlySDK)


class Client:
    def __init__(self, service, owner):
        self.service, self.owner = service, owner
        self.meta = SimpleNamespace(endpoint_url=ENDPOINTS[service], region_name=REGION)

    def __getattr__(self, operation):
        def invoke(**params):
            owner = self.owner
            owner.calls.append((self.service, operation, params))
            owner.sequence += 1
            result = owner.respond(self.service, operation, params)
            result.setdefault("ResponseMetadata", dict(HTTPStatusCode=200, RequestId=f"request-{owner.sequence}"))
            return result
        return invoke


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runpy.run_path(str(Path(__file__).with_name("test-migration-v2.py")))["MigrationContracts"]()
        self.fixture.setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = PrivateEvidence(Path(self.temp.name).resolve())
        self.calls, self.sequence = [], 0
        self.override = None
        self.clients = {name: Client(name, self) for name in ENDPOINTS}
        self.sdk = ReadOnlySDK(self.clients, self.evidence, clock=lambda: self.fixture.now)
        self.collector = LegacyCollector(self.sdk, sleep=lambda _: None)

    def respond(self, service, operation, params):
        if self.override:
            value = self.override(service, operation, params)
            if value is not None: return value
        if service == "sts": return dict(Account=ACCOUNT, Arn=f"arn:aws:iam::{ACCOUNT}:user/test")
        if operation in ("get_object", "head_object"):
            obj = next(o for o in self.fixture.objects.values() if o["reference"]["key"] == params["Key"])
            value = dict(VersionId=obj["reference"]["versionId"], ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY)
            if operation == "get_object": value["Body"] = io.BytesIO(obj["body"])
            return value
        if operation == "list_object_versions":
            ref = self.fixture.objects["session"]["reference"]
            return dict(IsTruncated=False, Versions=[dict(Key=ref["key"], VersionId=ref["versionId"],
                         IsLatest=True, LastModified=self.fixture.now)])
        if operation == "list_builds_for_project": return dict(ids=[])
        if operation == "lookup_events": return dict(Events=[])
        if operation == "list_roles": return dict(Roles=[dict(RoleName="techx-automation-lifecycle-dispatcher"),
                        dict(RoleName="techx-automation-v2-manager")], IsTruncated=False)
        if operation == "list_functions": return dict(Functions=[dict(FunctionName="techx-automation-cleanup-launcher")])
        if operation == "list_rules": return dict(Rules=[dict(Name="techx-automation-watchdog", State="ENABLED")])
        raise AssertionError((service, operation))

    def collect(self):
        return self.collector.collect(self.fixture.sid, self.fixture.legacy_ref)

    def test_real_call_chain_produces_only_non_attesting_assessment(self):
        result = self.collect()
        self.assertEqual(result.report["collectionStatus"], "COMPLETE")
        self.assertEqual(result.report["migrationStatus"], "NOT_VERIFIED")
        self.assertFalse(result.report["attestationEligible"])
        self.assertEqual(result.report["originalPhase"], "CALLING")
        self.assertEqual(result.report["roleCandidateCount"], 1)
        self.assertEqual(result.report["enabledRuleCount"], 1)
        self.assertEqual(sum(op == "head_object" for _, op, _ in self.calls), 6)
        self.assertTrue(list(self.evidence.path.glob("*.private.blob")))

    def test_decoded_json_cannot_construct_collected_result(self):
        with self.assertRaises(ValueError): CollectedLegacy({}, {}, "private")

    def test_rejects_git_output(self):
        root = Path(self.temp.name) / "checkout"; root.mkdir(); (root / ".git").mkdir()
        with self.assertRaises(ValueError): PrivateEvidence(root / "evidence")

    def test_mutation_rejected_before_sdk(self):
        with self.assertRaisesRegex(IncompleteAudit, "MUTATION"):
            self.sdk.call("s3", "put_object", Bucket=BUCKET)
        self.assertEqual(self.calls, [])

    def test_s3_wrong_owner_rejected_before_sdk(self):
        with self.assertRaisesRegex(IncompleteAudit, "S3_SCOPE"):
            self.sdk.call("s3", "get_object", Bucket=BUCKET, ExpectedBucketOwner="wrong")
        self.assertEqual(self.calls, [])

    def test_endpoint_and_region_must_be_fixed(self):
        for field, value in (("endpoint_url", "http://localhost:4566"), ("region_name", "us-west-2")):
            old = getattr(self.clients["s3"].meta, field)
            setattr(self.clients["s3"].meta, field, value)
            with self.assertRaisesRegex(IncompleteAudit, "UNTRUSTED"):
                ReadOnlySDK(self.clients, self.evidence)
            setattr(self.clients["s3"].meta, field, old)

    def test_wrong_identity_fails_before_reading_source(self):
        self.override = lambda s, o, p: dict(Account="000000000000") if s == "sts" else None
        with self.assertRaisesRegex(IncompleteAudit, "WRONG_CALLER"): self.collect()
        self.assertEqual(len(self.calls), 1)

    def test_missing_positive_metadata_is_not_empty_success(self):
        self.override = lambda s, o, p: dict(Account=ACCOUNT, ResponseMetadata={}) if s == "sts" else None
        with self.assertRaisesRegex(IncompleteAudit, "POSITIVE_RESPONSE"): self.collect()
        self.assertEqual(len(self.evidence.index), 1)

    def test_missing_result_list_fails(self):
        self.override = lambda s, o, p: {} if o == "list_builds_for_project" else None
        with self.assertRaisesRegex(IncompleteAudit, "EXPLICIT_RESULT_LIST"): self.collector.builds()

    def test_iam_truncation_without_marker_fails(self):
        self.override = lambda s, o, p: dict(Roles=[], IsTruncated=True) if s == "iam" else None
        with self.assertRaisesRegex(IncompleteAudit, "IAM_PAGINATION"): self.collector.controls()

    def test_iam_marker_without_truncation_fails(self):
        self.override = lambda s, o, p: dict(Roles=[], IsTruncated=False, Marker="next") if s == "iam" else None
        with self.assertRaisesRegex(IncompleteAudit, "IAM_PAGINATION"): self.collector.controls()

    def test_repeated_page_token_fails(self):
        self.override = lambda s, o, p: dict(ids=[], nextToken="repeat") if s == "codebuild" else None
        with self.assertRaisesRegex(IncompleteAudit, "REPEATED_PAGE_TOKEN"): self.collector.builds()

    def test_valid_iam_pages_are_all_collected(self):
        def override(s, o, p):
            if s != "iam": return None
            if "Marker" not in p:
                return dict(Roles=[dict(RoleName="techx-automation-first")], IsTruncated=True, Marker="next-page")
            self.assertEqual(p["Marker"], "next-page")
            return dict(Roles=[dict(RoleName="techx-automation-second")], IsTruncated=False)
        self.override = override
        self.assertEqual(len(self.collector.controls()["roles"]), 2)

    def test_object_bound_is_enforced_after_preserving_response(self):
        self.override = lambda s, o, p: dict(Body=io.BytesIO(b"x" * (1024 * 1024 + 1))) if o == "get_object" else None
        with self.assertRaisesRegex(IncompleteAudit, "OBJECT_TOO_LARGE"):
            self.collector.object("automation/staging/session.json")
        self.assertEqual(len(self.evidence.index), 1)

    def test_incomplete_cloudtrail_retention_does_not_scan_or_succeed(self):
        self.sdk.clock = lambda: datetime(2027, 3, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(IncompleteAudit, "RETENTION_CANNOT_COVER"): self.collect()
        self.assertFalse(any(s == "cloudtrail" for s, _, _ in self.calls))

    def test_head_race_fails(self):
        self.override = lambda s, o, p: dict(VersionId="new-version", ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY) if o == "head_object" else None
        with self.assertRaisesRegex(IncompleteAudit, "SOURCE_CHANGED"): self.collect()

    def test_wrong_kms_and_version_fail(self):
        for changes in (dict(SSEKMSKeyId="other-key"), dict(VersionId="other-version")):
            def override(s, o, p):
                if o != "get_object": return None
                return dict(Body=io.BytesIO(b"{}"), VersionId="legacy-execution-version", ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY, **{}) | changes
            self.override = override
            with self.assertRaisesRegex(IncompleteAudit, "VERSION_OR_KMS"):
                self.collector.object(self.fixture.legacy_ref["key"], object_version="legacy-execution-version")

    def test_duplicate_history_is_incomplete(self):
        row = dict(Key="automation/staging/session.json", VersionId="exact-version", IsLatest=True, LastModified=self.fixture.now)
        self.override = lambda s, o, p: dict(IsTruncated=False, Versions=[row, row]) if o == "list_object_versions" else None
        with self.assertRaisesRegex(IncompleteAudit, "DUPLICATE_VERSION"): self.collector.session_history(self.fixture.sid)

    def test_history_missing_service_time_is_incomplete(self):
        row = dict(Key="automation/staging/session.json", VersionId="exact-version", IsLatest=True)
        self.override = lambda s, o, p: dict(IsTruncated=False, Versions=[row]) if o == "list_object_versions" else None
        with self.assertRaisesRegex(IncompleteAudit, "VERSION_METADATA"): self.collector.session_history(self.fixture.sid)

    def test_active_build_is_reported_without_attestation(self):
        bid = PROJECTS[0] + ":aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        def override(s, o, p):
            if o == "list_builds_for_project" and p["projectName"] == PROJECTS[0]: return dict(ids=[bid])
            if o == "batch_get_builds": return dict(builds=[dict(id=bid, projectName=PROJECTS[0],
                arn=f"arn:aws:codebuild:{REGION}:{ACCOUNT}:build/{bid}", buildStatus="IN_PROGRESS")])
        self.override = override
        result = self.collect()
        self.assertEqual(result.report["activeBuildCount"], 1)
        self.assertFalse(result.report["attestationEligible"])

    def test_build_disappeared_is_incomplete(self):
        def override(s, o, p):
            if o == "list_builds_for_project": return dict(ids=[PROJECTS[0] + ":aaaa-bbbb"])
            if o == "batch_get_builds": return dict(builds=[], buildsNotFound=p["ids"])
        self.override = override
        with self.assertRaisesRegex(IncompleteAudit, "BUILD_STATE_UNKNOWN"): self.collector.builds()

    def test_redacted_denial_stays_unbound_and_preserved(self):
        event = self.fixture.event(); event["requestParameters"]["idempotencyToken"] = "HIDDEN_DUE_TO_SECURITY_REASONS"
        self.override = lambda s, o, p: dict(Events=[dict(CloudTrailEvent=json.dumps(event))]) if s == "cloudtrail" else None
        result = self.collect()
        self.assertEqual(result.report["directDenialBindingCount"], 0)
        private = b"".join(p.read_bytes() for p in self.evidence.path.glob("*.private.blob"))
        self.assertIn(b"HIDDEN_DUE_TO_SECURITY_REASONS", private)
        self.assertNotIn("HIDDEN_DUE_TO_SECURITY_REASONS", json.dumps(result.report))

    def test_identical_cloudtrail_replay_is_deduplicated_with_raw_pages_retained(self):
        event = self.fixture.event()
        self.override = lambda s, o, p: dict(Events=[dict(CloudTrailEvent=json.dumps(event))] * 2) if s == "cloudtrail" else None
        trail = self.collector.cloudtrail(self.fixture.snapshot, datetime(2026, 11, 9, tzinfo=timezone.utc), self.fixture.now)
        self.assertEqual(len(trail["events"]), 1)
        self.assertEqual(len(trail["directDenialBindings"]), 1)

    def test_conflicting_cloudtrail_replay_is_incomplete(self):
        event = self.fixture.event(); other = deepcopy(event); other["requestID"] = "different-request"
        self.override = lambda s, o, p: dict(Events=[dict(CloudTrailEvent=json.dumps(e)) for e in (event, other)]) if s == "cloudtrail" else None
        with self.assertRaisesRegex(IncompleteAudit, "CONFLICTING_TRAIL_EVENT"):
            self.collector.cloudtrail(self.fixture.snapshot, datetime(2026, 11, 9, tzinfo=timezone.utc), self.fixture.now)

    def test_sdk_error_is_retained_but_public_reason_is_sanitized(self):
        class Failure(Exception):
            response = dict(Error=dict(Code="AccessDenied", Message="sensitive-marker"))
        def override(s, o, p): raise Failure("sensitive-marker")
        self.override = override
        with self.assertRaises(IncompleteAudit) as caught: self.collect()
        self.assertNotIn("sensitive-marker", str(caught.exception))
        self.assertIn(b"sensitive-marker", b"".join(p.read_bytes() for p in self.evidence.path.glob("*.private.blob")))


if __name__ == "__main__": unittest.main()
