from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
import unittest

from bootstrap_assets import ACCOUNT, tag_list, tags
from cleanup_execution import delete_session_secrets
from secret_recovery import reconcile_secrets


class Missing(Exception): pass


class SecretRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.session, self.purpose = "staging-secret123", "vpn-pki"
        self.name = f"techx/staging/{self.session}/{self.purpose}"
        self.token = hashlib.sha256(f"{self.session}:{self.purpose}:v1".encode()).hexdigest()
        self.arn = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{self.name}-ABC123"
        self.private = dict(bootstrapSecrets={self.purpose: dict(name=self.name, versionId=self.token)})
        self.metadata = dict(ARN=self.arn, Name=self.name, Tags=tag_list(tags(self.session, self.purpose)), VersionIdsToStages={self.token: ["AWSCURRENT"]})
        self.objects, self.calls, self.deleted = {}, [], []
        self.present = True
        def get(**kw):
            if kw["Key"] not in self.objects: raise Missing()
            return dict(Body=io.BytesIO(self.objects[kw["Key"]]))
        def put(**kw): self.objects[kw["Key"]] = kw["Body"]
        def describe(**kw):
            self.calls.append(kw["SecretId"])
            if not self.present: raise Missing()
            return deepcopy(self.metadata)
        self.client = SimpleNamespace(describe_secret=describe, list_secrets=lambda **kw: dict(SecretList=[]),
            delete_secret=lambda **kw: self.deleted.append(kw), exceptions=SimpleNamespace(ResourceNotFoundException=Missing))
        self.clients = dict(s3=SimpleNamespace(get_object=get, put_object=put, exceptions=SimpleNamespace(NoSuchKey=Missing)), secretsmanager=self.client)

    def reconcile(self):
        return reconcile_secrets(self.clients, "control-bucket", self.session, "execution-v1", self.private)

    def test_exact_receipt_is_deleted_even_when_list_is_empty_then_survives_retry(self):
        known, unresolved = self.reconcile()
        self.assertEqual(unresolved, [])
        self.assertEqual(self.calls, [self.name])
        delete_session_secrets(self.client, self.session, known)
        self.assertEqual(self.deleted, [dict(SecretId=self.arn, ForceDeleteWithoutRecovery=True)])
        self.present = False
        self.assertEqual(self.reconcile(), ([], []))
        self.assertEqual(self.calls[-1], self.arn)

    def test_unknown_creation_and_missing_name_do_not_prove_absence(self):
        self.present = False
        self.assertEqual(self.reconcile(), ([], [self.purpose]))
        self.assertFalse(self.deleted)

    def test_pre_api_prepared_intent_can_be_absent_after_worker_termination(self):
        self.private["bootstrapSecrets"][self.purpose]["phase"] = "PREPARED"
        self.present = False
        self.assertEqual(self.reconcile(), ([], []))
        proof = json.loads(next(iter(self.objects.values())))
        self.assertEqual(proof["notCreated"], [self.purpose])

    def test_known_arn_confirmed_absent_is_valid_deletion_recovery(self):
        self.private["bootstrapSecrets"][self.purpose]["arn"] = self.arn
        self.present = False
        self.assertEqual(self.reconcile(), ([], []))

    def test_wrong_ownership_version_and_arn_are_rejected(self):
        for field, replacement in (("Tags", []), ("VersionIdsToStages", {}), ("ARN", self.arn.replace(ACCOUNT, "123456789012"))):
            old = self.metadata[field]
            self.metadata[field] = replacement
            with self.assertRaises(ValueError): self.reconcile()
            self.metadata[field] = old
        self.assertFalse(self.objects)

    def test_proof_is_bound_to_execution_and_exact_intents(self):
        self.reconcile()
        key = next(iter(self.objects))
        original = self.objects[key]
        value = json.loads(original)
        value["executionVersion"] = "other"
        self.objects[key] = json.dumps(value).encode()
        with self.assertRaises(ValueError): self.reconcile()
        self.objects[key] = original
        self.private["bootstrapSecrets"][self.purpose]["versionId"] = "b" * 64
        with self.assertRaises(ValueError): self.reconcile()

    def test_cleanup_never_accepts_recovered_secret_from_another_session(self):
        self.metadata["Tags"] = tag_list(tags("staging-other", self.purpose))
        with self.assertRaises(ValueError): delete_session_secrets(self.client, self.session, [self.metadata])
        self.assertFalse(self.deleted)


if __name__ == "__main__": unittest.main()
