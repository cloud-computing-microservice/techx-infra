from copy import deepcopy
import io
import json
from types import SimpleNamespace
import unittest

from bootstrap_assets import ACCOUNT, tag_list, tags
from certificate_recovery import reconcile_certificates


class Missing(Exception): pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.session = "staging-certs123"
        self.role = "client-vpn-reference"
        self.arn = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/12345678-1234-1234-1234-123456789abc"
        self.private = dict(certificateIntents={self.role: dict(phase="CALLING")})
        self.objects, self.writes = {}, []
        self.rows = [dict(ResourceARN=self.arn)]
        self.actual_tags = tag_list(tags(self.session, self.role))
        self.present, self.paginated = True, False
        def get(**kw):
            if kw["Key"] not in self.objects: raise Missing()
            return dict(Body=io.BytesIO(self.objects[kw["Key"]]))
        def put(**kw):
            self.writes.append(kw)
            self.objects[kw["Key"]] = kw["Body"]
        def describe(**kw):
            if not self.present: raise Missing()
            return dict(Certificate=dict(CertificateArn=self.arn))
        self.clients = dict(s3=SimpleNamespace(get_object=get, put_object=put, exceptions=SimpleNamespace(NoSuchKey=Missing)),
            resourcegroupstaggingapi=SimpleNamespace(get_resources=lambda **kw: dict(ResourceTagMappingList=deepcopy(self.rows), PaginationToken="next" if self.paginated else "")),
            acm=SimpleNamespace(describe_certificate=describe, list_tags_for_certificate=lambda **kw: dict(Tags=deepcopy(self.actual_tags)),
                exceptions=SimpleNamespace(ResourceNotFoundException=Missing)))

    def reconcile(self):
        return reconcile_certificates(self.clients, "control-bucket", self.session, "execution-v1", self.private)

    def test_discovered_import_receipt_survives_certificate_deletion(self):
        state, unresolved = self.reconcile()
        self.assertEqual(state["clientVpnRootCertificateArn"], self.arn)
        self.assertEqual(unresolved, [])
        self.present = False
        self.rows = []
        self.assertEqual(self.reconcile(), (state, []))
        self.assertTrue(self.writes)

    def test_empty_or_eventually_missing_discovery_does_not_prove_absence(self):
        self.rows = []
        state, unresolved = self.reconcile()
        self.assertEqual(unresolved, [self.role])
        self.assertNotIn("clientVpnRootCertificateArn", state)
        self.rows = [dict(ResourceARN=self.arn)]
        self.present = False
        self.assertEqual(self.reconcile()[1], [self.role])

    def test_exact_observed_receipt_can_be_already_deleted(self):
        self.private["certificateIntents"][self.role] = dict(phase="OBSERVED", arn=self.arn)
        self.present = False
        self.rows = []
        self.assertEqual(self.reconcile()[1], [])

    def test_tag_conflict_ambiguous_inventory_and_foreign_arn_fail(self):
        original = deepcopy(self.actual_tags)
        self.actual_tags = tag_list(tags("staging-other", self.role))
        with self.assertRaises(ValueError): self.reconcile()
        self.assertFalse(self.writes)
        self.actual_tags = original
        self.paginated = True
        with self.assertRaises(ValueError): self.reconcile()
        self.paginated = False
        self.rows *= 2
        with self.assertRaises(ValueError): self.reconcile()
        self.rows = [dict(ResourceARN=self.arn.replace(ACCOUNT, "123456789012"))]
        with self.assertRaises(ValueError): self.reconcile()

    def test_proof_cannot_be_reused_for_changed_intents_or_other_execution(self):
        self.reconcile()
        key = next(iter(self.objects))
        original = self.objects[key]
        value = json.loads(original)
        value["executionVersion"] = "different"
        self.objects[key] = json.dumps(value).encode()
        with self.assertRaises(ValueError): self.reconcile()
        self.objects[key] = original
        self.private["certificateIntents"][self.role]["phase"] = "OBSERVED"
        with self.assertRaises(ValueError): self.reconcile()

    def test_receipt_conflict_and_unknown_role_refuse_deletion_recovery(self):
        self.private["certificateIntents"][self.role]["arn"] = self.arn
        self.private["certificateState"] = dict(clientVpnRootCertificateArn=self.arn.replace("123456789abc", "000000000000"))
        with self.assertRaises(ValueError): self.reconcile()
        self.private = dict(certificateIntents={"unrelated-role": dict(phase="CALLING")})
        with self.assertRaises(ValueError): self.reconcile()


if __name__ == "__main__": unittest.main()
