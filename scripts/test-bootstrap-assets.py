"""Exercise real asset orchestration against bounded in-memory service APIs."""
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace

import botocore.session
from botocore.validate import validate_parameters

from automation_inputs import FIXED
from bootstrap_assets import ACCOUNT, AssetBootstrap, PKI_FIELDS, generate_pki, tag_list, tags
from cloudflare_dns import HOST


class Missing(Exception): pass
class Exists(Exception): pass


def pem(kind): return f"-----BEGIN {kind}-----\nYWJj\n-----END {kind}-----\n"


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.private = dict(schemaVersion=1, sessionId="staging-assets123", terraformVars=dict(FIXED),
            dnsMutationAttempted=False, validationDnsPlans=[])
        self.etag, self.events, self.certificates, self.secrets, self.records = 1, [], {}, {}, []
        self.secret_creates, self.imports, self.requests = 0, 0, 0
        self.fail_after_request = self.fail_after_import = self.fail_after_secret = self.fail_after_dns = False
        self.discovery_visible = True
        self.issued = True
        self.material = {name: pem("CERTIFICATE" if name.endswith(".crt") else "PRIVATE KEY") for name in PKI_FIELDS}
        self.sdk = botocore.session.get_session()
        def get_object(**kw): return dict(ETag=str(self.etag), Body=io.BytesIO(json.dumps(self.private).encode()))
        def put_object(**kw):
            self.assertEqual(kw["IfMatch"], str(self.etag))
            self.assertEqual(kw["SSEKMSKeyId"], self.runtime.kms)
            self.private = json.loads(kw["Body"])
            self.etag += 1
        self.clients = dict(s3=SimpleNamespace(get_object=get_object, put_object=put_object),
            acm=SimpleNamespace(request_certificate=self.request, import_certificate=self.import_certificate,
                meta=SimpleNamespace(config=SimpleNamespace(retries=dict(total_max_attempts=1))),
                describe_certificate=self.describe_certificate, list_tags_for_certificate=lambda **kw: dict(Tags=self.certificates[kw["CertificateArn"]]["Tags"]),
                get_certificate=lambda **kw: deepcopy(self.certificates[kw["CertificateArn"]]["Material"]),
                exceptions=SimpleNamespace(ResourceNotFoundException=Missing)),
            secretsmanager=SimpleNamespace(describe_secret=self.describe_secret, create_secret=self.create_secret,
                get_secret_value=self.get_secret, exceptions=SimpleNamespace(ResourceNotFoundException=Missing, ResourceExistsException=Exists)),
            resourcegroupstaggingapi=SimpleNamespace(get_resources=self.discover))
        self.runtime = SimpleNamespace(private=deepcopy(self.private), execution=dict(sessionId="staging-assets123"),
            bucket="control-bucket", kms=f"arn:aws:kms:us-east-1:{ACCOUNT}:key/12345678-1234-1234-1234-123456789abc", clients=self.clients)
        self.dns = SimpleNamespace(records=lambda _: deepcopy(self.records), ensure=self.ensure_dns)
        self.bootstrap = AssetBootstrap(self.runtime, lambda: self.events.append("guard"), sleep=lambda _: None,
            pki_factory=lambda: deepcopy(self.material), dns_factory=lambda: self.dns)

    def shape(self, service, operation, args):
        validate_parameters(args, self.sdk.get_service_model(service).operation_model(operation).input_shape)

    def create_certificate(self, role, material=None):
        arn = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/12345678-1234-1234-1234-{len(self.certificates) + 1:012d}"
        self.certificates[arn] = dict(Tags=tag_list(tags(self.runtime.execution["sessionId"], role)), calls=0,
            CertificateArn=arn, Type="IMPORTED" if material else "AMAZON_ISSUED", DomainName=HOST,
            Material=material, Status="ISSUED" if material else "PENDING_VALIDATION")
        return arn

    def request(self, **kw):
        self.shape("acm", "RequestCertificate", kw)
        self.assertEqual(self.private["certificateIntents"]["storefront-public"]["phase"], "CALLING")
        self.requests += 1
        arn = self.create_certificate("storefront-public")
        if self.fail_after_request: raise TimeoutError("ACM may have accepted")
        return dict(CertificateArn=arn)

    def import_certificate(self, **kw):
        self.shape("acm", "ImportCertificate", kw)
        self.assertNotIn("CertificateArn", kw)  # Never replace an imported cert.
        role = next(row["Value"] for row in kw["Tags"] if row["Key"] == "Role")
        self.assertEqual(self.private["certificateIntents"][role]["phase"], "CALLING")
        self.imports += 1
        arn = self.create_certificate(role, dict(Certificate=kw["Certificate"].decode(), CertificateChain=kw["CertificateChain"].decode()))
        if self.fail_after_import: raise TimeoutError("ImportCertificate response lost")
        return dict(CertificateArn=arn)

    def describe_certificate(self, **kw):
        value = deepcopy(self.certificates[kw["CertificateArn"]])
        self.certificates[kw["CertificateArn"]]["calls"] += 1
        if value["Type"] == "AMAZON_ISSUED":
            if value["calls"] > 0 and self.issued: value["Status"] = "ISSUED"
            value["DomainValidationOptions"] = [dict(DomainName=HOST, ResourceRecord=dict(
                Type="CNAME", Name="_abc." + HOST, Value="_abc.def.acm-validations.aws."))]
        return dict(Certificate=value)

    def discover(self, **kw):
        self.shape("resourcegroupstaggingapi", "GetResources", kw)
        wanted = {row["Key"]: row["Values"][0] for row in kw["TagFilters"]}
        rows = []
        for arn, value in self.certificates.items():
            if self.discovery_visible and {row["Key"]: row["Value"] for row in value["Tags"]} == wanted:
                rows.append(dict(ResourceARN=arn))
        return dict(ResourceTagMappingList=rows)

    def describe_secret(self, **kw):
        name = kw["SecretId"]
        if name not in self.secrets: raise Missing()
        return {key: deepcopy(value) for key, value in self.secrets[name].items() if key != "SecretString"}

    def create_secret(self, **kw):
        self.shape("secretsmanager", "CreateSecret", kw)
        name, token = kw["Name"], kw["ClientRequestToken"]
        self.assertIn(name.rsplit("/", 1)[-1], self.private["bootstrapSecrets"])
        self.assertEqual(self.private["bootstrapSecrets"][name.rsplit("/", 1)[-1]]["phase"], "CALLING")
        if name in self.secrets: raise Exists()
        self.secret_creates += 1
        self.secrets[name] = dict(ARN=f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{name}-ABC123", Name=name,
            KmsKeyId=kw["KmsKeyId"], Tags=kw["Tags"], VersionIdsToStages={token: ["AWSCURRENT"]}, SecretString=kw["SecretString"])
        if self.fail_after_secret: raise TimeoutError("Secret created but response lost")
        return dict(ARN=self.secrets[name]["ARN"])

    def get_secret(self, **kw):
        self.shape("secretsmanager", "GetSecretValue", kw)
        value = next(item for item in self.secrets.values() if item["ARN"] == kw["SecretId"])
        return dict(ARN=value["ARN"], VersionId=kw["VersionId"], SecretString=value["SecretString"])

    def ensure_dns(self, name, target, session):
        self.assertIn(dict(name=name, target=target), self.private["validationDnsPlans"])
        if not self.records: self.records.append(dict(id="a" * 32, content=target, proxied=False, comment=f"techx:{session}"))
        if self.fail_after_dns: raise TimeoutError("DNS POST response lost")
        return self.records[0]

    def test_cold_start_and_retry_reuse_three_certificates_and_one_pki_secret(self):
        self.bootstrap.prepare()
        self.bootstrap.prepare()
        self.assertEqual((self.requests, self.imports, self.secret_creates), (1, 2, 1))
        variables = self.runtime.private["terraformVars"]
        self.assertTrue(all(variables.get(key) for key in ("public_certificate_arn", "client_vpn_server_certificate_arn", "client_vpn_root_certificate_arn")))
        self.assertNotIn("PRIVATE KEY", json.dumps(self.private))
        self.assertNotIn("CERTIFICATE-----", json.dumps(self.private))
        self.assertEqual(len(self.private["validationDnsPlans"]), 1)

    def test_order_secret_is_created_once_and_retry_does_not_rotate(self):
        value = self.bootstrap.secret("order-api-key")
        self.assertEqual(value, self.bootstrap.secret("order-api-key"))
        self.assertEqual(self.secret_creates, 1)
        self.assertNotIn(value["order-api-key"], json.dumps(self.private))

    def test_automatic_acm_retries_are_rejected_before_import_can_duplicate(self):
        self.clients["acm"].meta.config.retries["total_max_attempts"] = 2
        with self.assertRaisesRegex(ValueError, "automatic retries disabled"):
            AssetBootstrap(self.runtime, lambda: None)
        self.assertFalse(self.certificates)

    def test_lost_secret_response_reuses_original_value(self):
        self.fail_after_secret = True
        with self.assertRaises(TimeoutError): self.bootstrap.secret("order-api-key")
        saved = next(iter(self.secrets.values()))["SecretString"]
        self.assertEqual(json.dumps(self.bootstrap.secret("order-api-key")), saved)
        self.assertEqual(self.secret_creates, 1)

    def test_observed_missing_secret_is_not_recreated_or_rotated(self):
        self.bootstrap.secret("order-api-key")
        self.secrets.clear()
        with self.assertRaisesRegex(ValueError, "must not recreate or rotate"):
            self.bootstrap.secret("order-api-key")
        self.assertEqual(self.secret_creates, 1)

    def test_pki_generation_failure_leaves_pre_api_checkpoint(self):
        def failed(): raise RuntimeError("OpenSSL failed before SecretString existed")
        self.bootstrap.pki_factory = failed
        with self.assertRaises(RuntimeError): self.bootstrap.secret("vpn-pki")
        self.assertEqual(self.private["bootstrapSecrets"]["vpn-pki"]["phase"], "PREPARED")
        self.assertFalse(self.secrets)

    def test_unknown_public_and_import_receipts_are_recovered_by_session_role(self):
        self.fail_after_request = True
        with self.assertRaises(TimeoutError): self.bootstrap.prepare()
        self.fail_after_request = False
        self.fail_after_import = True
        with self.assertRaises(TimeoutError): self.bootstrap.prepare()
        self.fail_after_import = False
        self.bootstrap.prepare()
        self.assertEqual((self.requests, self.imports, self.secret_creates), (1, 2, 1))

    def test_unknown_receipt_with_empty_tag_read_never_repeats_request(self):
        self.fail_after_request = True
        with self.assertRaises(TimeoutError): self.bootstrap.public()
        self.discovery_visible = False
        with self.assertRaisesRegex(ValueError, "uncertain"): self.bootstrap.public()
        self.assertEqual(self.requests, 1)

    def test_matching_foreign_validation_record_is_read_only_and_not_cleanup_owned(self):
        self.records = [dict(id="b" * 32, content="_abc.def.acm-validations.aws", proxied=False, comment="other-session")]
        self.bootstrap.public()
        self.assertEqual(self.private["validationDnsPlans"], [])
        self.assertEqual(self.records[0]["comment"], "other-session")

    def test_foreign_dns_conflict_and_missing_issuance_fail(self):
        self.records = [dict(id="b" * 32, content="other.example", proxied=False)]
        with self.assertRaisesRegex(ValueError, "conflicts"): self.bootstrap.public()
        self.assertEqual(self.private["validationDnsPlans"], [])
        self.records = []
        self.issued = False
        with self.assertRaises(TimeoutError): self.bootstrap.public(observations=2)

    def test_lost_dns_post_has_durable_intent_for_cleanup(self):
        self.fail_after_dns = True
        with self.assertRaises(TimeoutError): self.bootstrap.public()
        self.assertEqual(len(self.private["validationDnsPlans"]), 1)
        self.bootstrap.public()
        self.assertEqual(len(self.records), 1)

    def test_lease_loss_and_snapshot_conflict_prevent_cloud_mutation(self):
        def lost(): raise ValueError("lease lost")
        self.bootstrap.guard = lost
        with self.assertRaises(ValueError): self.bootstrap.prepare()
        self.assertFalse(self.certificates)
        self.bootstrap.guard = lambda: None
        self.private["changed"] = True
        with self.assertRaisesRegex(ValueError, "snapshot changed"): self.bootstrap.prepare()
        self.assertFalse(self.certificates)

    def test_secret_wrong_ownership_encryption_and_rotation_are_rejected(self):
        self.bootstrap.secret("order-api-key")
        value = next(iter(self.secrets.values()))
        for key, replacement in (("KmsKeyId", "wrong"), ("Tags", []), ("VersionIdsToStages", {})):
            previous = value[key]
            value[key] = replacement
            with self.assertRaises(ValueError): self.bootstrap.secret("order-api-key")
            value[key] = previous

    def test_actual_openssl_helper_generates_pki_outside_source_and_removes_keys(self):
        runtime = SimpleNamespace(infra=Path(__file__).parents[1], run=subprocess.run, env=None)
        value = generate_pki(runtime)
        self.assertEqual(set(value), PKI_FIELDS)
        self.assertNotIn("ca.key", value)
        self.assertTrue(value["server.key"].startswith("-----BEGIN"))


if __name__ == "__main__": unittest.main()
