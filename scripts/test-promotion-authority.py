from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chart_selection import CHART_REPO
from plan_review_store import checksum
from private_chart_promotion import app_clients
from promotion_authority import admitted, authority, check, check_lifecycle, from_environment, MAXIMUM, LIFECYCLE_MAXIMUM


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


auth_support = load("authority_app_support", "test-github-app-auth.py")
policy_support = load("authority_policy_support", "test-chart-promotion.py")


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.a = auth_support.AppAuthTests()
        self.a.setUp()
        self.p = policy_support.PromotionTests()
        self.p.setUp()
        self.addCleanup(self.p.doCleanups)
        self.secret = {key: value for key, value in self.a.secret.items() if key != "schemaVersion"}
        self.a.installation["permissions"] = dict(MAXIMUM, metadata="read")
        self.policy = self.p.policy
        self.calls, self.revoked = [], []
        self.revoke_fails = self.lost_creation = self.policy_race = self.installation_race = self.wrong_scope = False
        self.install_reads = self.policy_reads = 0
        self.token_permissions_override = None

    def transport(self, path, token, payload=None, **kwargs):
        self.calls.append((path, payload, kwargs))
        if path == "/installation/token":
            self.revoked.append(token)
            self.assertEqual(kwargs, dict(method="DELETE"))
            if self.revoke_fails: raise TimeoutError("revocation response lost")
            return None
        if path == f"/repos/{CHART_REPO}/branches/main/protection":
            self.policy_reads += 1
            result = deepcopy(self.policy)
            if self.policy_race and self.policy_reads > 1: result["required_status_checks"]["strict"] = False
            return result
        result = self.a.transport(path, token, payload, **kwargs)
        if path.endswith("/access_tokens") and self.token_permissions_override is not None:
            result["permissions"] = deepcopy(self.token_permissions_override)
        if path == "/app/installations/77":
            self.install_reads += 1
            if self.installation_race and self.install_reads > 1: result["suspended_at"] = self.a.now.isoformat()
        if path.endswith("/access_tokens") and self.lost_creation: raise TimeoutError("unknown creation response")
        if path.startswith("/installation/repositories?") and self.wrong_scope:
            return dict(total_count=2, repositories=[dict(full_name=CHART_REPO), dict(full_name="other/repo")])
        return result

    def check(self):
        return check(self.secret, "unused-test-work", lambda: self.a.now, transport=self.transport, sign=lambda *args: "fixture-jwt")

    def test_verified_read_scope_and_enforced_policy_produce_only_public_metadata_and_revoke(self):
        proof = self.check()
        self.assertEqual(proof, dict(schemaVersion=1, repository=CHART_REPO, appId=12345, appSlug="techx-promotion",
                                    installationId=77, policySha256=checksum(self.policy)))
        self.assertNotIn("privateKey", json.dumps(proof))
        self.assertNotIn("fake-scoped-token", json.dumps(proof))
        creates = [body for path, body, _ in self.calls if path.endswith("/access_tokens")]
        self.assertEqual(creates, [dict(repositories=["techx-chart"], permissions=dict(MAXIMUM, contents="read"))])
        self.assertEqual(len(self.revoked), 1)
        self.assertEqual(self.calls[-1], ("/installation/token", None, dict(method="DELETE")))
        self.assertFalse(any("/pulls/" in path for path, *_ in self.calls))

    def test_wrong_maximum_or_extra_permissions_fail_before_any_token_creation(self):
        for value in (dict(MAXIMUM, contents="read"), dict(MAXIMUM, administration="write"),
                      dict(MAXIMUM, secrets="read"), {}):
            self.a.installation["permissions"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "maximum permissions"): self.check()
        self.assertFalse(any(path.endswith("/access_tokens") for path, *_ in self.calls))

    def test_unselected_suspended_or_foreign_installation_cannot_start_preflight(self):
        original = deepcopy(self.a.installation)
        for fields in (dict(repository_selection="all"), dict(suspended_at=self.a.now.isoformat()),
                       dict(account=dict(login="other-org")), dict(app_id=999)):
            self.a.installation = dict(original, **fields)
            with self.subTest(fields=fields), self.assertRaises(ValueError): self.check()
        self.assertFalse(any(path.endswith("/access_tokens") for path, *_ in self.calls))

    def test_wrong_actual_token_scope_is_revoked_without_any_policy_or_merge_mutation(self):
        self.wrong_scope = True
        with self.assertRaisesRegex(ValueError, "Actual installation token"): self.check()
        self.assertEqual(len(self.revoked), 1)
        self.assertEqual(self.policy_reads, 0)

    def test_overprivileged_token_response_is_revoked_even_though_mint_requested_only_read(self):
        self.token_permissions_override = dict(MAXIMUM, metadata="read")
        with self.assertRaises(ValueError): self.check()
        self.assertEqual(len(self.revoked), 1)

    def test_policy_or_app_revocation_mid_check_rejects_and_revokes_the_read_token(self):
        self.policy_race = True
        with self.assertRaises(ValueError): self.check()
        self.assertEqual(len(self.revoked), 1)
        self.policy_race = False
        self.policy_reads = self.install_reads = 0
        self.installation_race = True
        with self.assertRaisesRegex(ValueError, "changed during preflight"): self.check()
        self.assertEqual(len(self.revoked), 2)

    def test_unknown_token_creation_or_revocation_outcome_is_not_retried_or_accepted(self):
        self.lost_creation = True
        with self.assertRaises(TimeoutError): self.check()
        self.assertEqual(sum(path.endswith("/access_tokens") for path, *_ in self.calls), 1)
        self.assertFalse(self.revoked)
        self.lost_creation = False
        self.calls.clear()
        self.revoke_fails = True
        with self.assertRaises(TimeoutError): self.check()
        self.assertEqual(sum(path == "/installation/token" for path, *_ in self.calls), 1)

    def test_missing_malformed_or_secret_bearing_public_authority_is_rejected(self):
        proof = self.check()
        for value in (None, dict(proof, privateKey="never-public"), dict(proof, installationId=True),
                      dict(proof, repository="other/repo"), dict(proof, policySha256="old")):
            with self.subTest(value=value), self.assertRaises(ValueError): authority(value)
        with self.assertRaises(ValueError): admitted({})

    def test_private_executor_cannot_swap_installation_after_opening(self):
        proof = self.check()
        runtime = SimpleNamespace(clients=dict(secretsmanager=object()), kms="control-kms", work="unused",
            execution=dict(sourceAdmission=dict(promotionAuthority=proof)))
        policy = {key: proof[key] for key in ("appId", "appSlug", "policySha256")}
        secret = dict(self.secret, installationId=88)
        with patch("private_chart_promotion.app_secret_snapshot", return_value=(secret, {})) as read, patch("private_chart_promotion.installation_client") as mint:
            with self.assertRaisesRegex(ValueError, "human-reviewed branch authority"):
                app_clients(runtime, policy, lambda: self.a.now, "execution-v1")
            self.assertEqual(read.call_count, 1)
            mint.assert_not_called()

    def test_missing_environment_identity_cannot_fall_back_to_a_fictitious_app(self):
        with patch.dict("os.environ", dict(PROMOTION_APP_ID="", PROMOTION_INSTALLATION_ID="")), patch("promotion_authority.check") as live:
            with self.assertRaisesRegex(ValueError, "must be configured"): from_environment("unused")
            live.assert_not_called()

    def test_lifecycle_maximum_is_verified_but_only_two_repo_read_tokens_are_issued(self):
        self.a.installation["permissions"] = dict(LIFECYCLE_MAXIMUM, metadata="read")
        result = check_lifecycle(self.secret, "unused", lambda: self.a.now, transport=self.transport, sign=lambda *args: "fixture-jwt")
        self.assertEqual(result, dict(appId=12345, appSlug="techx-promotion", installationId=77))
        bodies = [body for path, body, _ in self.calls if path.endswith("/access_tokens")]
        self.assertEqual([row["repositories"] for row in bodies], [["techx-infra"], ["techx-platform"]])
        self.assertTrue(all(body["permissions"] == dict(contents="read", actions="read") for body in bodies))
        self.assertEqual(len(self.revoked), 2)

    def test_lifecycle_without_actions_write_maximum_cannot_be_admitted(self):
        self.a.installation["permissions"] = dict(contents="read", actions="read", metadata="read")
        with self.assertRaisesRegex(ValueError, "maximum permissions"):
            check_lifecycle(self.secret, "unused", lambda: self.a.now, transport=self.transport, sign=lambda *args: "fixture-jwt")
        self.assertFalse(any(path.endswith("/access_tokens") for path, *_ in self.calls))


if __name__ == "__main__": unittest.main()
