from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qsl

from execution_v2 import ACCOUNT, BUCKET, PREFIX, REGION
from session_runtime_v2 import (CASConflict, ENDPOINTS, KMS_KEY, MutationConsumed, MutationUncertain, PreparedMutation,
                                SessionObservation, SessionStore, WriteSuperseded, encoded, session_tags)


class ServiceError(Exception):
    def __init__(self, code, status):
        self.response = dict(Error=dict(Code=code), ResponseMetadata=dict(HTTPStatusCode=status, RequestId="error-request"))


class Client:
    def __init__(self, name, backend):
        self.name, self.backend = name, backend
        self.meta = SimpleNamespace(endpoint_url=ENDPOINTS[name], region_name=REGION,
                                    config=SimpleNamespace(retries={"total_max_attempts":1}))

    def __getattr__(self, operation):
        def call(**params):
            self.backend.calls.append((self.name, operation, params))
            result = self.backend.respond(operation, params)
            result.setdefault("ResponseMetadata", dict(HTTPStatusCode=200, RequestId=f"request-{len(self.backend.calls)}"))
            return result
        return call


class SessionRuntimeTests(unittest.TestCase):
    def setUp(self):
        f = runpy.run_path(str(Path(__file__).with_name("test-migration-v2.py")))["MigrationContracts"](); f.setUp()
        self.execution, self.grant, self.grant_ref, self.now = deepcopy(f.staging), deepcopy(f.grant), deepcopy(f.grant_ref), f.now
        self.objects, self.calls, self.counter = {}, [], 0
        self.override, self.put_mode, self.claim_mode = None, "success", "success"
        self.principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-deployment-executor/i-1234567890abcdef0"
        self.clients = {name:Client(name, self) for name in ENDPOINTS}
        e = self.publish(f"{PREFIX}executions/{self.execution['sessionId']}.json", encoded(self.execution))
        self.ref = dict(bucket=BUCKET, key=e["key"], versionId=e["VersionId"], sha256=hashlib.sha256(e["body"]).hexdigest())
        # Fixture grants are canonical original bytes; preserve their exact reference.
        self.publish(self.grant_ref["key"], encoded(self.grant), object_version=self.grant_ref["versionId"])
        self.key = f"{PREFIX}sessions/{self.execution['sessionId']}.json"
        self.record = dict(schemaVersion=2, executorBackend="ec2-ssm", sessionId=self.execution["sessionId"],
                           executionReference=deepcopy(self.ref), status="PLANNING", leaseEpoch=1, runtimeRevision=1, lastMutationId="1" * 32)
        self.publish_session(self.record)

    def publish(self, key, body, object_tags=None, object_version=None):
        self.counter += 1
        obj = dict(key=key, body=body, tags=deepcopy(object_tags or {}), VersionId=object_version or f"version-{self.counter}",
                   ETag='"' + hashlib.md5(body).hexdigest() + '"')
        self.objects.setdefault(key, []).append(obj)
        return obj

    def publish_session(self, value): return self.publish(self.key, encoded(value), session_tags(value, self.ref))

    def store(self, role=None):
        if role:
            suffix = "i-1234567890abcdef0" if role.endswith("-executor") else "runtime-session"
            if role == "manager": suffix = "techx-v2-123456-1"
            self.principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-{role}/{suffix}"
        return SessionStore(self.clients, self.ref, clock=lambda:self.now)

    def respond(self, operation, params):
        if self.override:
            result = self.override(operation, params)
            if result is not None: return result
        if operation == "get_caller_identity": return dict(Account=ACCOUNT, Arn=self.principal)
        self.assertEqual(params["Bucket"], BUCKET); self.assertEqual(params["ExpectedBucketOwner"], ACCOUNT)
        if operation in ("get_object", "get_object_tagging"):
            items = self.objects[params["Key"]]
            obj = next((o for o in items if o["VersionId"] == params["VersionId"]), None) if "VersionId" in params else items[-1]
            if obj is None: raise ServiceError("NoSuchVersion", 404)
            if operation == "get_object_tagging":
                return dict(VersionId=obj["VersionId"], TagSet=[dict(Key=k, Value=v) for k,v in obj["tags"].items()])
            return dict(Body=io.BytesIO(obj["body"]), ETag=obj["ETag"], VersionId=obj["VersionId"], ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY)
        if operation == "put_object":
            if params["Key"] != self.key:
                self.assertEqual(params.get("IfNoneMatch"), "*")
                if params["Key"] in self.objects: raise ServiceError("PreconditionFailed", 412)
                if self.claim_mode == "timeout-before": raise TimeoutError()
                if self.claim_mode == "conflict409": raise ServiceError("ConditionalRequestConflict", 409)
                obj = self.publish(params["Key"], params["Body"], dict(parse_qsl(params["Tagging"])))
                if self.claim_mode == "timeout-after": raise TimeoutError()
                return dict(VersionId=obj["VersionId"], ETag=obj["ETag"], ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY, ChecksumSHA256=params["ChecksumSHA256"])
            if self.put_mode == "conflict": raise ServiceError("PreconditionFailed", 412)
            if self.put_mode == "conflict409": raise ServiceError("ConditionalRequestConflict", 409)
            if self.put_mode == "conflict-without-metadata":
                error = ServiceError("PreconditionFailed", 412); del error.response["ResponseMetadata"]["RequestId"]; raise error
            if self.put_mode == "timeout-before": raise TimeoutError("sensitive-transport-message")
            if self.objects[params["Key"]][-1]["ETag"] != params["IfMatch"]: raise ServiceError("PreconditionFailed", 412)
            obj = self.publish(params["Key"], params["Body"], dict(parse_qsl(params["Tagging"])))
            if self.put_mode == "timeout-after": raise TimeoutError("sensitive-transport-message")
            if self.put_mode == "supersede":
                value = json.loads(params["Body"]); value.update(status="DESTROYING", leaseEpoch=value["leaseEpoch"]+1,
                                                               runtimeRevision=value["runtimeRevision"]+1, lastMutationId="f"*32)
                self.publish_session(value)
            response = dict(VersionId=obj["VersionId"], ETag=obj["ETag"], ServerSideEncryption="aws:kms", SSEKMSKeyId=KMS_KEY, ChecksumSHA256=params["ChecksumSHA256"])
            if self.put_mode == "bad-response": response["ResponseMetadata"] = {}
            return response
        raise AssertionError(operation)

    def puts(self): return [p for _, op, p in self.calls if op == "put_object" and p["Key"] == self.key]

    def test_actual_sdk_reads_bind_original_context_tags_and_cas_write(self):
        store = self.store(); before = store.read(); prepared = store.prepare(before, "PROVISIONING")
        after = store.commit(prepared)
        self.assertEqual(after.as_dict()["status"], "PROVISIONING")
        self.assertEqual(after.as_dict()["runtimeRevision"], 2)
        self.assertEqual(after.as_dict()["leaseEpoch"], 1)
        self.assertEqual(self.puts()[0]["IfMatch"], before._etag)
        self.assertEqual(self.puts()[0]["Key"], self.key)
        self.assertEqual(self.puts()[0]["SSEKMSKeyId"], KMS_KEY)
        self.assertEqual(dict(parse_qsl(self.puts()[0]["Tagging"]))["techx-status"], "PROVISIONING")

    def test_json_cannot_substitute_for_service_observation_or_prepared_operation(self):
        with self.assertRaises(ValueError): SessionObservation(None, {}, {}, "etag")
        with self.assertRaises(ValueError): PreparedMutation(None, None, {})
        store = self.store()
        with self.assertRaises(ValueError): store.prepare(self.record, "PROVISIONING")
        with self.assertRaises(ValueError): store.commit({})

    def test_observations_are_defensive_copies(self):
        store = self.store(); observed = store.read()
        changed = observed.as_dict(); changed["leaseEpoch"] = 999
        changed_ref = observed.reference; changed_ref["versionId"] = "fake"
        self.assertEqual(observed.as_dict()["leaseEpoch"], 1)
        self.assertNotEqual(observed.reference["versionId"], "fake")
        from execution_v2 import validate_session
        self.assertEqual(validate_session(observed.envelope(), store.execution, store.execution_reference)["leaseEpoch"], 1)

    def test_cannot_use_snapshot_from_another_store(self):
        first = self.store(); other = self.store()
        with self.assertRaises(ValueError): other.prepare(first.read(), "PROVISIONING")

    def test_no_v1_scope_or_backend_execution_is_accepted(self):
        changed = dict(self.ref, key="executions/staging-gh-1001.json")
        with self.assertRaises(ValueError): SessionStore(self.clients, changed)
        self.assertFalse(any(p.get("Key", "").startswith("automation/staging/") for _, _, p in self.calls))

    def test_wrong_endpoint_or_region_rejected_before_service_call(self):
        self.clients["s3"].meta.endpoint_url = "http://localhost:4566"
        with self.assertRaises(ValueError): self.store()
        self.assertEqual(self.calls, [])

    def test_sdk_mutation_retries_must_be_disabled(self):
        for setting in (None, {}, dict(total_max_attempts=3), dict(max_attempts=1)):
            self.clients["s3"].meta.config.retries = setting
            with self.subTest(setting=setting), self.assertRaises(ValueError): self.store()
        self.assertEqual(self.calls, [])

    def test_human_or_wrong_account_identity_is_not_a_runtime_writer(self):
        for arn in (f"arn:aws:iam::{ACCOUNT}:user/terraform-admin", self.principal.replace(ACCOUNT, "000000000000")):
            self.principal = arn
            with self.assertRaises(ValueError): self.store()
        self.assertEqual(self.puts(), [])

    def test_wrong_manager_attempt_or_worker_session_is_rejected(self):
        for role, suffix in (("manager", "techx-v2-123456-2"), ("deployment-executor", "invented-instance")):
            self.principal = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-{role}/{suffix}"
            with self.assertRaises(ValueError): self.store()

    def test_missing_positive_aws_metadata_is_not_read_success(self):
        self.override = lambda op, p: dict(Account=ACCOUNT, Arn=self.principal, ResponseMetadata={}) if op == "get_caller_identity" else None
        with self.assertRaises(ValueError): self.store()

    def test_exact_execution_version_hash_and_kms_are_required(self):
        for changes in (dict(versionId="missing-version"), dict(sha256="f"*64)):
            with self.assertRaises((ValueError, ServiceError)): SessionStore(self.clients, dict(self.ref, **changes))
        def override(op, p):
            if op == "get_object": return dict(Body=io.BytesIO(b"{}"), VersionId=p.get("VersionId", "version"), ETag='"etag"', ServerSideEncryption="AES256")
        self.override = override
        with self.assertRaisesRegex(ValueError, "encryption"): self.store()

    def test_body_tag_mismatch_and_tag_version_mismatch_fail(self):
        store = self.store()
        self.objects[self.key][-1]["tags"]["techx-status"] = "READY"
        with self.assertRaisesRegex(ValueError, "version tags"): store.read()
        self.objects[self.key][-1]["tags"] = session_tags(self.record, self.ref)
        self.override = lambda op, p: dict(VersionId="wrong", TagSet=[]) if op == "get_object_tagging" else None
        with self.assertRaisesRegex(ValueError, "Version-specific"): store.read()

    def test_duplicate_tags_and_unexpected_fields_fail(self):
        store = self.store()
        self.override = lambda op, p: dict(VersionId=self.objects[self.key][-1]["VersionId"], TagSet=[dict(Key="techx-status", Value="PLANNING")] * 2) if op == "get_object_tagging" else None
        with self.assertRaisesRegex(ValueError, "duplicate"): store.read()
        self.override = None
        self.publish_session(dict(self.record, forceComplete=True))
        with self.assertRaises(ValueError): store.read()

    def test_worker_cannot_promote_ready_or_complete(self):
        store = self.store(); observed = store.read()
        for target in ("READY", "DESTROYED", "DESTROYING", "TESTING"):
            with self.subTest(target=target), self.assertRaises(ValueError): store.prepare(observed, target)
        self.assertEqual(self.puts(), [])

    def test_destroyed_body_does_not_substitute_for_finalizer_receipt(self):
        self.publish_session(dict(self.record, status="DESTROYED"))
        with self.assertRaisesRegex(ValueError, "protected finalizer"): self.store().read()

    def test_manager_can_only_start_testing_an_already_ready_session(self):
        self.publish_session(dict(self.record, status="READY"))
        store = self.store("manager"); observed = store.read()
        prepared = store.prepare(observed, "TESTING")
        self.assertEqual(store.commit(prepared).as_dict()["status"], "TESTING")
        for target in ("READY", "PROVISIONING", "DESTROYED"):
            with self.assertRaises(ValueError): store.prepare(observed, target)

    def test_readiness_role_is_required_for_ready_transition(self):
        self.publish_session(dict(self.record, status="PROVISIONING"))
        store = self.store("readiness")
        self.assertEqual(store.commit(store.prepare(store.read(), "READY")).as_dict()["status"], "READY")

    def test_cleanup_fences_epoch_and_can_continue_after_deadline(self):
        self.now += timedelta(days=1)
        store = self.store("cleanup-launcher")
        after = store.commit(store.prepare(store.read(), "DESTROYING")).as_dict()
        self.assertEqual((after["status"], after["leaseEpoch"]), ("DESTROYING", 2))

    def test_cleanup_failure_keeps_fence_and_cannot_complete(self):
        self.publish_session(dict(self.record, status="DESTROYING", leaseEpoch=2))
        store = self.store("cleanup-executor"); observed = store.read()
        result = store.commit(store.prepare(observed, "FAILED_NEEDS_CLEANUP")).as_dict()
        self.assertEqual(result["leaseEpoch"], 2)
        with self.assertRaises(ValueError): store.prepare(observed, "DESTROYED")

    def test_deployment_stops_at_cleanup_reserve_or_before_window(self):
        for delta in (timedelta(hours=8), timedelta(days=-1)):
            self.now += delta
            store = self.store()
            with self.assertRaises(ValueError): store.prepare(store.read(), "PROVISIONING")
            self.now -= delta
        self.assertEqual(self.puts(), [])

    def test_clock_rechecked_after_preparation(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING")
        self.now += timedelta(hours=8)
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(self.puts(), [])

    def test_principal_change_is_rejected_before_write(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING")
        self.principal = self.principal.replace("deployment-executor", "cleanup-executor")
        with self.assertRaisesRegex(ValueError, "principal changed"): store.commit(prepared)
        self.assertEqual(self.puts(), [])

    def test_prewrite_read_detects_another_fence_before_put(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING")
        self.publish_session(dict(self.record, status="DESTROYING", leaseEpoch=2, runtimeRevision=2, lastMutationId="f"*32))
        with self.assertRaises(CASConflict): store.commit(prepared)
        self.assertEqual(self.puts(), [])

    def test_same_body_republished_version_does_not_reuse_old_snapshot(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING")
        self.publish_session(self.record)
        with self.assertRaises(CASConflict): store.commit(prepared)
        self.assertEqual(self.puts(), [])

    def test_service_conflict_is_not_retried(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.put_mode = "conflict"
        with self.assertRaises(CASConflict): store.commit(prepared)
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(len(self.puts()), 1)

    def test_lost_response_after_write_reconciles_without_another_write(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.put_mode = "timeout-after"
        with self.assertRaises(MutationUncertain) as caught: store.commit(prepared)
        self.assertNotIn("sensitive-transport-message", str(caught.exception))
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(store.reconcile(prepared).as_dict()["status"], "PROVISIONING")
        self.assertEqual(len(self.puts()), 1)

    def test_lost_response_before_write_stays_unknown_and_cannot_retry(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.put_mode = "timeout-before"
        with self.assertRaises(MutationUncertain): store.commit(prepared)
        self.assertIsNone(store.reconcile(prepared))
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(len(self.puts()), 1)

    def test_incomplete_success_metadata_reconciles_from_service_reads(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.put_mode = "bad-response"
        with self.assertRaises(MutationUncertain): store.commit(prepared)
        self.assertIsNotNone(store.reconcile(prepared))
        self.assertEqual(len(self.puts()), 1)

    def test_write_superseded_by_cleanup_never_grants_next_stage(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.put_mode = "supersede"
        with self.assertRaises(WriteSuperseded): store.commit(prepared)
        self.assertIsNone(store.reconcile(prepared))
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(len(self.puts()), 1)

    def test_same_preparation_has_stable_mutation_id_and_unique_revision(self):
        store = self.store(); observed = store.read()
        first, second = store.prepare(observed, "PROVISIONING"), store.prepare(observed, "PROVISIONING")
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertEqual(first.as_dict()["runtimeRevision"], observed.as_dict()["runtimeRevision"] + 1)

    def test_restart_cannot_reprepare_and_replay_unknown_write(self):
        first = self.store(); old = first.read(); prepared = first.prepare(old, "PROVISIONING")
        self.put_mode = "timeout-before"
        with self.assertRaises(MutationUncertain): first.commit(prepared)
        second = self.store(); re_prepared = second.prepare(second.read(), "PROVISIONING")
        self.assertEqual(re_prepared.as_dict()["lastMutationId"], prepared.as_dict()["lastMutationId"])
        with self.assertRaises(MutationConsumed): second.commit(re_prepared)
        self.assertIsNone(second.recover(prepared.as_dict()["lastMutationId"]))
        self.assertEqual(len(self.puts()), 1)

    def test_restart_recovers_exact_success_without_writing(self):
        first = self.store(); prepared = first.prepare(first.read(), "PROVISIONING"); self.put_mode = "timeout-after"
        with self.assertRaises(MutationUncertain): first.commit(prepared)
        second = self.store()
        self.assertEqual(second.recover(prepared.as_dict()["lastMutationId"]).as_dict()["status"], "PROVISIONING")
        self.assertEqual(len(self.puts()), 1)

    def test_unknown_claim_response_never_dispatches_session_write(self):
        first = self.store(); prepared = first.prepare(first.read(), "PROVISIONING"); self.claim_mode = "timeout-after"
        with self.assertRaises(MutationUncertain): first.commit(prepared)
        self.assertEqual(self.puts(), [])
        second = self.store(); re_prepared = second.prepare(second.read(), "PROVISIONING")
        with self.assertRaises(MutationConsumed): second.commit(re_prepared)
        self.assertEqual(self.puts(), [])
        self.assertIsNone(second.recover(prepared.as_dict()["lastMutationId"]))

    def test_claim_different_nonce_is_not_reused_for_mutation(self):
        first = self.store(); prepared = first.prepare(first.read(), "PROVISIONING")
        second = self.store(); other = second.prepare(second.read(), "PROVISIONING")
        self.assertNotEqual(prepared._nonce, other._nonce)
        self.put_mode = "timeout-before"
        with self.assertRaises(MutationUncertain): first.commit(prepared)
        with self.assertRaises(MutationConsumed): second.commit(other)
        self.assertEqual(len(self.puts()), 1)

    def test_recover_rejects_wrong_claim_scope_or_body(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); store.commit(prepared)
        claim_key = store._claim_key(prepared.as_dict()["lastMutationId"])
        row = self.objects[claim_key][-1]; value = json.loads(row["body"])
        value["intendedRecord"]["leaseEpoch"] = 99
        row["body"] = encoded(value)
        with self.assertRaises(ValueError): store.recover(prepared.as_dict()["lastMutationId"])
        with self.assertRaises(ValueError): store.recover("../../v1")

    def test_clock_rechecked_after_claim_before_session_write(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING")
        def override(op, p):
            if op == "put_object" and p["Key"] != self.key: self.now += timedelta(hours=8)
        self.override = override
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(self.puts(), [])
        with self.assertRaises(ValueError): store.commit(prepared)

    def test_new_cleanup_principal_shares_consumed_semantic_mutation_slot(self):
        first = self.store("cleanup-launcher"); prepared = first.prepare(first.read(), "DESTROYING")
        self.put_mode = "timeout-before"
        with self.assertRaises(MutationUncertain): first.commit(prepared)
        second = self.store("cleanup-executor"); other = second.prepare(second.read(), "DESTROYING")
        self.assertEqual(prepared.as_dict()["lastMutationId"], other.as_dict()["lastMutationId"])
        with self.assertRaises(MutationConsumed): second.commit(other)
        self.assertEqual(len(self.puts()), 1)

    def test_sdk_models_accept_the_actual_request_arguments(self):
        import botocore.session
        from botocore.validate import validate_parameters
        store = self.store(); store.commit(store.prepare(store.read(), "PROVISIONING"))
        session = botocore.session.get_session()
        names = {"get_caller_identity":"GetCallerIdentity", "get_object":"GetObject",
                 "get_object_tagging":"GetObjectTagging", "put_object":"PutObject"}
        for service, operation, params in self.calls:
            shape = session.get_service_model(service).operation_model(names[operation]).input_shape
            validate_parameters(params, shape)

    def test_409_claim_conflict_does_not_fabricate_consumed_slot(self):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.claim_mode = "conflict409"
        with self.assertRaises(MutationUncertain): store.commit(prepared)
        self.assertEqual(prepared._state, "CLAIMING")
        self.assertEqual(self.puts(), [])

    def assert_uncertain_rejection(self, mode):
        store = self.store(); prepared = store.prepare(store.read(), "PROVISIONING"); self.put_mode = mode
        with self.assertRaises(MutationUncertain): store.commit(prepared)
        self.assertIsNone(store.reconcile(prepared))
        with self.assertRaises(ValueError): store.commit(prepared)
        self.assertEqual(len(self.puts()), 1)

    def test_409_session_conflict_remains_uncertain(self):
        self.assert_uncertain_rejection("conflict409")

    def test_rejection_without_request_metadata_remains_uncertain(self):
        self.assert_uncertain_rejection("conflict-without-metadata")


if __name__ == "__main__": unittest.main()
