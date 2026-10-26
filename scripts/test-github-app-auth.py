import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from chart_selection import CHART_REPO, INFRA_REPO, PLATFORM_REPO
from github_app_auth import api, app_secret, app_secret_snapshot, installation_client, jwt, RepositoryClient

KMS = "arn:aws:kms:us-east-1:058114477594:key/12345678-1234-1234-1234-123456789abc"


class AppAuthTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.secret = dict(schemaVersion=1, appId=12345, slug="techx-promotion", installationId=77,
                           privateKey="-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n")
        name = "techx/automation/promotion-app"
        self.metadata = dict(Name=name, ARN=f"arn:aws:secretsmanager:us-east-1:058114477594:secret:{name}-Abc123", KmsKeyId=KMS,
                             Tags=[dict(Key=key, Value=value) for key, value in dict(Project="techx", Environment="automation", Role="github-app").items()])
        self.result = dict(Name=name, ARN=self.metadata["ARN"], VersionId="a" * 32, VersionStages=["AWSCURRENT"], SecretString=json.dumps(self.secret))
        self.secret_calls = []
        self.secrets = SimpleNamespace(describe_secret=lambda **kw: deepcopy(self.metadata), get_secret_value=self.read_secret)
        self.sent, self.token_override = [], {}
        self.installation = dict(id=77, app_id=12345, repository_selection="selected", account=dict(login="cloud-computing-microservice"), suspended_at=None)

    def read_secret(self, **kwargs):
        self.secret_calls.append(kwargs)
        return deepcopy(self.result)

    def transport(self, path, token, payload=None, **kwargs):
        self.sent.append((path, token, payload, kwargs))
        if path == "/app": return dict(id=12345, slug="techx-promotion", owner=dict(login="cloud-computing-microservice"))
        if path == "/app/installations/77": return deepcopy(self.installation)
        if path == "/app/installations/77/access_tokens":
            repository = "cloud-computing-microservice/" + payload["repositories"][0]
            self.last_repository = repository
            return dict(token="fake-scoped-token-" + "a" * 20, expires_at=(self.now + timedelta(hours=1)).isoformat(),
                repository_selection="selected", repositories=[dict(full_name=repository)],
                permissions=dict(payload["permissions"], metadata="read"), **self.token_override)
        if path == "/installation/repositories?per_page=2":
            return dict(total_count=1, repositories=[dict(full_name=self.last_repository)])
        return dict(merged=True)

    def mint(self, repository=CHART_REPO, merge=True):
        return installation_client(self.secret, repository, "unused-test-work", lambda: self.now, merge=merge,
                                   transport=self.transport, sign=lambda *args: "fixture-jwt")

    def test_fixed_named_owned_kms_current_secret_is_the_only_key_source(self):
        result = app_secret(self.secrets, "promotion", kms_key=KMS)
        self.assertEqual(result, self.secret)
        self.assertEqual(self.secret_calls, [dict(SecretId=self.metadata["ARN"], VersionStage="AWSCURRENT")])
        for key, value in (("Name", "other-secret"), ("KmsKeyId", "other-kms"), ("DeletedDate", self.now)):
            old = deepcopy(self.metadata)
            self.metadata[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): app_secret(self.secrets, "promotion", kms_key=KMS)
            self.metadata = old

    def test_secret_snapshot_exposes_only_an_exact_version_reference_beside_private_bytes(self):
        value, ref = app_secret_snapshot(self.secrets, "promotion", kms_key=KMS)
        self.assertEqual(value, self.secret)
        self.assertEqual(ref, dict(arn=self.metadata["ARN"], versionId=self.result["VersionId"]))
        self.assertNotIn("privateKey", json.dumps(ref))

    def test_platform_installation_client_has_only_read_capability(self):
        client = self.mint(PLATFORM_REPO, merge=False)
        self.assertEqual(self.last_repository, PLATFORM_REPO)
        with self.assertRaises(ValueError): client.request("actions/workflows/bootstrap-staging.yml/dispatches", dict(ref="main"))
        with self.assertRaises(ValueError): self.mint(PLATFORM_REPO, merge=True)

    def test_wrong_tag_scope_version_stage_or_oversized_secret_is_rejected(self):
        self.metadata["Tags"][0]["Value"] = "other-project"
        with self.assertRaises(ValueError): app_secret(self.secrets, "promotion", kms_key=KMS)
        self.metadata["Tags"][0]["Value"] = "techx"
        for key, value in (("VersionStages", ["AWSPREVIOUS"]), ("VersionId", "null"), ("SecretString", "x" * (33 * 1024))):
            old = self.result[key]
            self.result[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): app_secret(self.secrets, "promotion", kms_key=KMS)
            self.result[key] = old
        with self.assertRaises(ValueError): app_secret(self.secrets, "unrelated", kms_key=KMS)

    def test_chart_token_has_only_one_repo_and_contents_write_with_other_reads(self):
        client = self.mint()
        payload = next(payload for path, _, payload, _ in self.sent if path.endswith("/access_tokens"))
        self.assertEqual(payload["repositories"], ["techx-chart"])
        self.assertEqual(payload["permissions"], dict(contents="write", pull_requests="read", administration="read", actions="read"))
        client.merge(7, "a" * 40, "TechX promotion " + "f" * 64, "Session staging-test123; task " + "9" * 32)
        path, _, body, options = self.sent[-1]
        self.assertEqual(path, f"/repos/{CHART_REPO}/pulls/7/merge")
        self.assertEqual(options, dict(method="PUT"))
        self.assertEqual(body["sha"], "a" * 40)
        self.assertEqual(body["merge_method"], "merge")

    def test_infra_review_token_is_read_only_and_cannot_merge_or_dispatch(self):
        client = self.mint(INFRA_REPO, merge=False)
        payload = next(payload for path, _, payload, _ in self.sent if path.endswith("/access_tokens"))
        self.assertEqual(payload["permissions"], dict(contents="read", actions="read"))
        with self.assertRaises(ValueError): client.merge(7, "a" * 40, "TechX promotion " + "f" * 64, "Session staging-test123; task " + "9" * 32)
        with self.assertRaises(ValueError): client.request("actions/workflows/staging-lifecycle.yml/dispatches", dict(ref="main"))

    def test_wrong_installation_org_or_suspended_app_is_rejected_before_token_mint(self):
        for key, value in (("app_id", 999), ("repository_selection", "all"), ("account", dict(login="other-org")), ("suspended_at", self.now.isoformat())):
            old = self.installation[key]
            self.installation[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.mint()
            self.installation[key] = old
        self.assertFalse(any(path.endswith("/access_tokens") for path, *_ in self.sent))

    def test_token_scope_expansion_wrong_repo_or_short_expiry_is_rejected(self):
        base = self.transport
        for change in (dict(permissions=dict(contents="write", administration="write", actions="read", pull_requests="read", metadata="read")),
                       dict(repositories=[dict(full_name=INFRA_REPO)]), dict(repository_selection="all"),
                       dict(expires_at=(self.now + timedelta(minutes=1)).isoformat())):
            def changed(path, token, payload=None, **kwargs):
                result = base(path, token, payload, **kwargs)
                return dict(result, **change) if path.endswith("/access_tokens") else result
            with self.subTest(change=change), self.assertRaises(ValueError):
                installation_client(self.secret, CHART_REPO, "unused", lambda: self.now, merge=True, transport=changed, sign=lambda *args: "fixture-jwt")

    def test_token_expiry_and_non_merge_mutation_fail_before_transport(self):
        client = self.mint()
        calls = len(self.sent)
        with self.assertRaises(ValueError): client.request("git/refs", dict(ref="refs/heads/main", sha="a" * 40), method="POST")
        self.now += timedelta(hours=1)
        with self.assertRaises(ValueError): client.get("pulls/7")
        self.assertEqual(len(self.sent), calls)

    def test_implicit_metadata_may_be_omitted_but_actual_repo_scope_is_always_verified(self):
        base = self.transport
        def without_metadata(path, token, payload=None, **kwargs):
            result = base(path, token, payload, **kwargs)
            if path.endswith("/access_tokens"):
                result["permissions"].pop("metadata")
                result.pop("repository_selection")
                result.pop("repositories")
            return result
        installation_client(self.secret, CHART_REPO, "unused", lambda: self.now, merge=True, transport=without_metadata, sign=lambda *args: "fixture-jwt")
        self.assertTrue(any(path == "/installation/repositories?per_page=2" for path, *_ in self.sent))
        def wrong_scope(path, token, payload=None, **kwargs):
            result = base(path, token, payload, **kwargs)
            return dict(total_count=2, repositories=[dict(full_name=CHART_REPO), dict(full_name=INFRA_REPO)]) if path.startswith("/installation/repositories?") else result
        with self.assertRaisesRegex(ValueError, "Actual installation token"):
            installation_client(self.secret, CHART_REPO, "unused", lambda: self.now, merge=True, transport=wrong_scope, sign=lambda *args: "fixture-jwt")

    def test_api_has_one_bounded_call_fixed_origin_and_no_redirect_following(self):
        requests = []
        class HTTP:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b'{"ok":true}'
        def open_url(request, **kwargs):
            requests.append((request, kwargs))
            return HTTP()
        api(f"/repos/{CHART_REPO}/pulls/7", "fixture-token", open_url=open_url)
        self.assertEqual(requests[0][1], dict(timeout=30))
        self.assertTrue(requests[0][0].full_url.startswith("https://api.github.com/"))
        self.assertEqual(requests[0][0].get_header("X-github-api-version"), "2026-03-10")
        with self.assertRaises(ValueError): api("https://other.example/", "fixture-token", open_url=open_url)
        count = []
        def lost(*args, **kwargs):
            count.append(1)
            raise TimeoutError("lost mutation response")
        with self.assertRaises(TimeoutError): api(f"/repos/{CHART_REPO}/pulls/7/merge", "fixture-token", dict(sha="a" * 40), method="PUT", open_url=lost)
        self.assertEqual(len(count), 1)

    def test_204_is_accepted_only_for_revoking_the_callers_installation_token(self):
        class HTTP:
            status = 204
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b""
        calls = []
        def open_url(request, **kwargs):
            calls.append(request)
            return HTTP()
        self.assertIsNone(api("/installation/token", "fixture-token", method="DELETE", open_url=open_url))
        self.assertEqual(calls[0].method, "DELETE")
        with self.assertRaises(ValueError): api("/installation/token", "fixture-token", open_url=open_url)
        with self.assertRaises(ValueError): api(f"/repos/{CHART_REPO}/pulls/7/merge", "fixture-token", method="DELETE", open_url=open_url)
        HTTP.status = 200
        with self.assertRaisesRegex(ValueError, "revocation was not acknowledged"):
            api("/installation/token", "fixture-token", method="DELETE", open_url=open_url)

    def test_real_openssl_rs256_jwt_verifies_and_does_not_leave_a_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            key = subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"], check=True, capture_output=True).stdout
            secret = dict(self.secret, privateKey=key.decode())
            token = jwt(secret, self.now, directory)
            header, body, signature = token.split(".")
            decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            self.assertEqual(json.loads(decode(header)), dict(alg="RS256", typ="JWT"))
            claims = json.loads(decode(body))
            self.assertEqual(claims["iss"], "12345")
            self.assertEqual(claims["iat"], int(self.now.timestamp()) - 60)
            self.assertEqual(claims["exp"], int(self.now.timestamp()) + 540)
            self.assertEqual(list(Path(directory).iterdir()), [])
            public = subprocess.run(["openssl", "pkey", "-pubout"], input=key, check=True, capture_output=True).stdout
            pubpath, sigpath = Path(directory) / "public.pem", Path(directory) / "signature.bin"
            pubpath.write_bytes(public)
            sigpath.write_bytes(decode(signature))
            result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pubpath), "-signature", str(sigpath)],
                input=(header + "." + body).encode(), check=True, capture_output=True)
            self.assertIn(b"Verified OK", result.stdout)


if __name__ == "__main__": unittest.main()
