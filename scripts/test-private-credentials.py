from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from chart_selection import CHART_REPO, INFRA_REPO, PLATFORM_REPO
from plan_review_store import checksum
from private_credentials import credential_proof, verify
from private_chart_promotion import app_clients
from promotion_authority import LIFECYCLE_MAXIMUM, MAXIMUM


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runtime_support = load("credentials_runtime_support", "test-deployment-runtime.py")
authority_support = load("credentials_authority_support", "test-promotion-authority.py")


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.f = runtime_support.RuntimeTests()
        self.f.setUp()
        self.f.flow()
        self.addCleanup(self.f.doCleanups)
        self.a = authority_support.AuthorityTests()
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)
        self.runtime = self.f.runtime
        self.keys = dict(promotion=dict(self.a.secret, schemaVersion=1), lifecycle=dict(schemaVersion=1,
            appId=23456, slug="techx-lifecycle", installationId=88, privateKey=self.a.secret["privateKey"]))
        self.registered = deepcopy(self.keys)
        self.versions = dict(promotion="a" * 32, lifecycle="b" * 32)
        self.policy = self.a.policy
        self.promo = dict(schemaVersion=1, repository=CHART_REPO, appId=12345, appSlug="techx-promotion",
            installationId=77, policySha256=checksum(self.policy))
        self.life = dict(appId=23456, appSlug="techx-lifecycle", installationId=88)
        self.runtime.execution["sourceAdmission"] = dict(promotionAuthority=self.promo, lifecycleIdentity=self.life)
        self.secret_calls, self.network, self.revoked, self.tokens = [], [], [], {}
        self.on_network = None
        self.wrong_kms = self.wrong_scope = self.lost_write = False
        self.private_writes = 0
        parent_put = self.runtime.clients["s3"].put_object
        def put(**kw):
            result = parent_put(**kw)
            if kw["Key"].endswith("inputs.json"):
                self.private_writes += 1
                if self.lost_write: raise TimeoutError("private CAS committed; response lost")
            return result
        self.runtime.clients["s3"].put_object = put
        self.runtime.clients["secretsmanager"] = type("ExistingKeys", (), {
            "describe_secret": lambda _, **kw: self.describe(**kw),
            "get_secret_value": lambda _, **kw: self.read(**kw),
        })()
        self.f.credential_verifier.side_effect = lambda runtime, guard, clock: verify(
            runtime, guard, clock, transport=self.transport, sign=self.sign)

    def sign(self, secret, *args): return "jwt-" + str(secret["appId"])

    def arn(self, purpose): return f"arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/{purpose}-app-Abc123"

    def describe(self, **kw):
        self.secret_calls.append(("describe", kw))
        purpose = next(p for p in self.keys if kw["SecretId"] == f"techx/automation/{p}-app")
        return dict(Name=f"techx/automation/{purpose}-app", ARN=self.arn(purpose),
            KmsKeyId="wrong-kms" if self.wrong_kms else self.runtime.kms,
            Tags=[dict(Key=k, Value=v) for k, v in dict(Project="techx", Environment="automation", Role="github-app").items()])

    def read(self, **kw):
        self.secret_calls.append(("read", kw))
        self.assertEqual(kw["VersionStage"], "AWSCURRENT")
        purpose = next(p for p in self.keys if kw["SecretId"] == self.arn(p))
        return dict(Name=f"techx/automation/{purpose}-app", ARN=self.arn(purpose),
            VersionId=self.versions[purpose], VersionStages=["AWSCURRENT"], SecretString=json.dumps(self.keys[purpose]))

    def transport(self, path, token, payload=None, **kwargs):
        self.network.append((path, payload, kwargs))
        if self.on_network: self.on_network(path, payload)
        if path == "/installation/token":
            self.assertEqual(kwargs, dict(method="DELETE"))
            self.revoked.append(token)
            return None
        if path == f"/repos/{CHART_REPO}/branches/main/protection": return deepcopy(self.policy)
        if path == "/installation/repositories?per_page=2":
            if self.wrong_scope: return dict(total_count=2, repositories=[])
            return dict(total_count=1, repositories=[dict(full_name=self.tokens[token])])
        app_id = int(token.removeprefix("jwt-"))
        key = next(value for value in self.registered.values() if value["appId"] == app_id)
        if path == "/app": return dict(id=app_id, slug=key["slug"], owner=dict(login="cloud-computing-microservice"))
        install = f"/app/installations/{key['installationId']}"
        if path == install:
            return dict(id=key["installationId"], app_id=app_id, account=dict(login="cloud-computing-microservice"),
                repository_selection="selected", suspended_at=None, permissions=dict(MAXIMUM if app_id == 12345 else LIFECYCLE_MAXIMUM, metadata="read"))
        if path == install + "/access_tokens":
            self.assertTrue(all(value == "read" for value in payload["permissions"].values()))
            created = "fake-read-token-" + str(len(self.tokens)) + "a" * 24
            repository = "cloud-computing-microservice/" + payload["repositories"][0]
            self.tokens[created] = repository
            from datetime import timedelta
            return dict(token=created, expires_at=(self.f.now + timedelta(hours=1)).isoformat(),
                permissions=dict(payload["permissions"], metadata="read"), repositories=[dict(full_name=repository)], repository_selection="selected")
        raise AssertionError(path)

    def guard(self): return self.f.worker.guard(self.f.owner, self.f.record["lease"]["epoch"])

    def verify(self): return verify(self.runtime, self.guard, lambda: self.f.now, transport=self.transport, sign=self.sign)

    def test_real_key_reads_three_read_tokens_and_cas_pin_contain_no_private_material(self):
        self.verify()
        proof = credential_proof(self.runtime.private, self.runtime.execution, "execution-version")
        self.assertEqual(proof["promotion"]["authority"], self.promo)
        self.assertEqual(proof["lifecycle"]["identity"], self.life)
        self.assertEqual(proof["promotion"]["secret"]["versionId"], "a" * 32)
        self.assertEqual(set(self.tokens.values()), {CHART_REPO, INFRA_REPO, PLATFORM_REPO})
        self.assertEqual(set(self.revoked), set(self.tokens))
        self.assertEqual(self.private_writes, 1)
        self.assertNotIn("privateKey", json.dumps(self.f.current_private))
        self.assertNotIn("fake-read-token", json.dumps(self.f.current_private))
        self.assertFalse(self.f.calls)

    def test_same_pins_are_revalidated_without_writing_or_rotating(self):
        self.verify()
        self.verify()
        self.assertEqual(len(self.tokens), 6)
        self.assertEqual(self.private_writes, 1)
        self.assertEqual(len(self.revoked), 6)

    def test_wrong_control_kms_or_current_scope_stops_before_snapshot_and_assets(self):
        self.wrong_kms = True
        with self.assertRaises(ValueError): self.verify()
        self.assertFalse(self.network)
        self.wrong_kms = False
        self.wrong_scope = True
        with self.assertRaises(ValueError): self.verify()
        self.assertEqual(len(self.revoked), 1)
        self.assertEqual(self.private_writes, 0)

    def test_secret_rotation_during_jwt_checks_cannot_publish_a_stale_pin(self):
        def rotate(path, payload):
            if path == "/app/installations/88": self.versions["promotion"] = "c" * 32
        self.on_network = rotate
        with self.assertRaisesRegex(ValueError, "changed during credential verification"): self.verify()
        self.assertEqual(self.private_writes, 0)
        self.assertEqual(len(self.revoked), 3)

    def test_changed_lifecycle_identity_cannot_replace_human_opening_scope(self):
        self.runtime.execution["sourceAdmission"]["lifecycleIdentity"]["appId"] = 999
        with self.assertRaisesRegex(ValueError, "admitted GitHub identities"): self.verify()
        self.assertEqual(self.private_writes, 0)

    def test_cleanup_during_token_read_blocks_publication_but_still_revokes_own_token(self):
        def cleanup(path, payload):
            if path == "/installation/repositories?per_page=2":
                self.f.record["status"] = "DESTROYING"
                self.f.record["lease"]["owner"] = "cleanup-owner"
        self.on_network = cleanup
        with self.assertRaises(ValueError): self.verify()
        self.assertEqual(len(self.revoked), 1)
        self.assertEqual(self.private_writes, 0)

    def test_lost_pin_cas_is_recovered_only_from_actual_private_snapshot(self):
        self.lost_write = True
        with self.assertRaises(TimeoutError): self.verify()
        self.assertEqual(self.private_writes, 1)
        self.lost_write = False
        self.runtime.private = deepcopy(self.f.current_private)
        self.verify()
        self.assertEqual(self.private_writes, 1)

    def test_rotated_key_between_reviewed_plan_and_apply_stops_before_any_terraform(self):
        ref = self.f.perform()
        self.f.approve(ref)
        self.f.calls.clear()
        self.versions["lifecycle"] = "c" * 32
        with self.assertRaisesRegex(ValueError, "version changed"): self.f.perform("apply")
        self.assertFalse(self.f.calls)
        self.assertEqual(self.f.record["stages"]["foundation"]["state"], "AWAITING_APPROVAL")

    def test_rotation_after_initialize_is_caught_at_final_apply_boundary(self):
        ref = self.f.perform()
        self.f.approve(ref)
        initialize = self.runtime.initialize
        def rotated():
            initialize()
            self.versions["lifecycle"] = "c" * 32
        self.runtime.initialize = rotated
        self.f.calls.clear()
        with self.assertRaisesRegex(ValueError, "version changed"): self.f.perform("apply")
        self.assertFalse(any(command[2:3] == ["apply"] for command, _ in self.f.calls))
        self.assertEqual(self.f.record["stages"]["foundation"]["state"], "UNCERTAIN")

    def test_missing_or_wrong_execution_proof_is_rejected(self):
        self.verify()
        with self.assertRaises(ValueError): credential_proof({}, self.runtime.execution, "execution-version")
        with self.assertRaises(ValueError): credential_proof(self.runtime.private, self.runtime.execution, "different-version")
        self.runtime.private["automationCredentials"]["lifecycle"]["privateKey"] = "never-public"
        with self.assertRaises(ValueError): self.verify()

    def test_late_private_promotion_cannot_mint_with_either_rotated_control_key(self):
        self.verify()
        policy = {key: self.promo[key] for key in ("appId", "appSlug", "policySha256")}
        for purpose in ("promotion", "lifecycle"):
            original = self.versions[purpose]
            self.versions[purpose] = "c" * 32
            with patch("private_chart_promotion.installation_client") as mint:
                with self.subTest(purpose=purpose), self.assertRaisesRegex(ValueError, "credential pinning"):
                    app_clients(self.runtime, policy, lambda: self.f.now, "execution-version")
                mint.assert_not_called()
            self.versions[purpose] = original


if __name__ == "__main__": unittest.main()
