from copy import deepcopy
from datetime import timedelta
import base64
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from botocore import UNSIGNED
from manager_bridge_v2 import (AUDIENCE, ISSUER, REPO, ROLE, SERVICES, AuthenticatedManager, AuthenticationFailed,
                              GitHubRunReader, ManagerBridge, bootstrap_policy, operation_policy)
from manager_bridge_v2 import credential_clients, unsigned_sts
from session_runtime_v2 import ENDPOINTS, encoded
from recovery_v2 import wire_time

Helpers = runpy.run_path(str(Path(__file__).with_name("test-session-runtime-v2.py")))


def jwt(claims, header=None, signature=b"synthetic-signature-verified-only-by-fake-STS"):
    def b64(value): return base64.urlsafe_b64encode(value).decode().rstrip("=")
    return ".".join((b64(encoded(header or dict(alg="RS256", typ="JWT", kid="test-key"))), b64(encoded(claims)), b64(signature)))


class STS:
    def __init__(self, owner):
        self.owner = owner
        self.meta = SimpleNamespace(endpoint_url=ENDPOINTS["sts"], region_name="us-east-1",
                                    config=SimpleNamespace(signature_version=UNSIGNED, retries={"total_max_attempts":1}))
    def assume_role_with_web_identity(self, **params):
        o = self.owner; o.exchanges.append(deepcopy(params))
        if o.sts_error: raise RuntimeError("secret-transport-token")
        o.assertEqual(params["WebIdentityToken"], o.token)
        o.assertEqual(params["RoleArn"], ROLE)
        response = dict(SubjectFromWebIdentityToken=o.claims["sub"], Audience=AUDIENCE, Provider=ISSUER,
            AssumedRoleUser=dict(Arn=o.aws.principal, AssumedRoleId=o.user_id),
            Credentials=dict(AccessKeyId="ASIA-SYNTHETIC-" + str(len(o.exchanges)), SecretAccessKey="never-log-secret-key",
                             SessionToken="never-log-session-token", Expiration=o.aws.now + timedelta(minutes=45)),
            ResponseMetadata=dict(HTTPStatusCode=200, RequestId="sts-request"))
        if o.response_change: o.response_change(response)
        return response


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.aws = Helpers["SessionRuntimeTests"](); self.aws.setUp()
        self.aws.store("manager")
        self.user_id = "AROA-SYNTHETIC:techx-v2-123456-1"
        self.aws.override = lambda op, p: dict(Account="058114477594", Arn=self.aws.principal, UserId=self.user_id) if op == "get_caller_identity" else None
        self.workflow, self.job = "staging-lifecycle.yml", "foundation-plan"
        stamp = int(self.aws.now.timestamp())
        self.claims = dict(iss=ISSUER, aud=AUDIENCE, repository=REPO, repository_id="1322975868", repository_owner_id="328679702",
            ref="refs/heads/main", run_id="123456", run_attempt="1", environment="staging-lifecycle", runner_environment="github-hosted",
            workflow_ref=f"{REPO}/.github/workflows/{self.workflow}@refs/heads/main", workflow_sha="a"*40, sha="a"*40,
            sub="repo:cloud-computing-microservice@328679702/techx-infra@1322975868:environment:staging-lifecycle",
            event_name="workflow_dispatch", iat=stamp-15, nbf=stamp-20, exp=stamp+285)
        self.token = jwt(self.claims)
        self.run = dict(id=123456, run_attempt=1, status="in_progress", conclusion=None, event="workflow_dispatch", head_branch="main",
            head_sha="a"*40, path=f".github/workflows/{self.workflow}", created_at="2026-11-22T01:00:00Z",
            repository=dict(id=1322975868, owner=dict(id=328679702)), head_repository=dict(id=1322975868))
        self.listing = dict(total_count=1, jobs=[dict(id=987654, run_id=123456, run_attempt=1, name=self.job, head_sha="a"*40,
            status="in_progress", conclusion=None, completed_at=None, started_at="2026-11-22T01:01:00Z",
            runner_group_name="GitHub Actions", labels=["ubuntu-24.04"])])
        self.exchanges, self.factories, self.github_calls = [], [], []
        self.sts_error, self.github_error, self.response_change, self.factory_change = False, False, None, None
        self.sts = STS(self)
        self.bridge = ManagerBridge("private-github-read-token", sts=self.sts, clients_factory=self.factory,
                                    open_url=self.github_http, clock=lambda:self.aws.now)

    def github_http(self, request, timeout):
        self.github_calls.append(request.full_url)
        if self.github_error: raise RuntimeError("secret-transport-token")
        response = io.BytesIO(encoded(self.listing if "/jobs?" in request.full_url else self.run))
        response.status = 200
        response.headers = {"Content-Type":"application/json", "X-GitHub-Request-Id":"api-request"}
        response.geturl = lambda:request.full_url
        return response

    def factory(self, credentials):
        self.factories.append(deepcopy(credentials))
        clients = dict(self.aws.clients)
        for service in SERVICES - set(clients):
            clients[service] = SimpleNamespace(meta=SimpleNamespace(endpoint_url=f"https://{service}.us-east-1.amazonaws.com", region_name="us-east-1",
                                                                   config=SimpleNamespace(retries={"total_max_attempts":1})))
        if self.factory_change: self.factory_change(clients)
        return clients

    def authenticate(self):
        return self.bridge.authenticate(self.token, self.aws.ref, workflow=self.workflow, job=self.job)

    def claims_changed(self, **changes):
        self.claims.update(changes); self.token = jwt(self.claims)

    def test_actual_service_chain_issues_read_only_then_scoped_session(self):
        auth = self.authenticate()
        self.assertEqual(len(self.exchanges), 2)
        bootstrap = json.loads(self.exchanges[0]["Policy"])
        for row in bootstrap["Statement"]:
            actions = row["Action"] if isinstance(row["Action"], list) else [row["Action"]]
            self.assertFalse(any(a.startswith(("s3:Put", "ssm:Send", "lambda:")) for a in actions))
        self.assertNotEqual(self.factories[0]["AccessKeyId"], self.factories[1]["AccessKeyId"])
        self.assertEqual(auth.as_dict()["identity"]["job"], self.job)
        self.assertFalse(auth.as_dict()["observeOnly"])
        self.assertEqual(auth.require_current(mutation=True).as_dict()["leaseEpoch"], 1)
        self.assertEqual(self.aws.puts(), [])
        for marker in (self.token, "never-log-secret-key", "never-log-session-token"):
            self.assertNotIn(marker, json.dumps(auth.as_dict()) + repr(auth))

    def test_no_operator_json_constructor(self):
        with self.assertRaises(AuthenticationFailed): AuthenticatedManager(None,None,None,None,None,None,None,None,None,None,None)

    def test_signature_rejection_never_creates_clients(self):
        self.sts_error = True
        with self.assertRaises(AuthenticationFailed) as caught: self.authenticate()
        self.assertNotIn("secret-transport-token", str(caught.exception))
        self.assertEqual(self.factories, [])

    def test_bad_token_format_or_algorithm_stops_before_sts(self):
        for token in ("not-a-jwt", self.token + " ", jwt(self.claims, dict(alg="none", typ="JWT", kid="test"))):
            self.token = token
            with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.exchanges, [])

    def test_duplicate_token_payload_keys_are_not_trusted(self):
        segments = self.token.split(".")
        raw = encoded(self.claims).decode().replace('"run_attempt":"1"', '"run_attempt":"1","run_attempt":"1"')
        segments[1] = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        self.token = ".".join(segments)
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.exchanges, [])

    def test_wrong_ref_runner_repository_or_attempt_is_preflight_failure(self):
        for key, value in (("ref","refs/heads/feature"), ("runner_environment","self-hosted"), ("repository_id","123"), ("run_attempt","2")):
            old = self.claims[key]; self.claims_changed(**{key:value})
            with self.subTest(key=key), self.assertRaises(AuthenticationFailed): self.authenticate()
            self.claims_changed(**{key:old})
        self.assertEqual(self.exchanges, [])

    def test_token_expiry_or_noninteger_time_is_rejected(self):
        for change in (dict(exp=int(self.aws.now.timestamp())-1), dict(iat=True), dict(nbf=int(self.aws.now.timestamp())+100)):
            before = deepcopy(self.claims); self.claims_changed(**change)
            with self.assertRaises(AuthenticationFailed): self.authenticate()
            self.claims = before; self.token = jwt(before)
        self.assertEqual(self.exchanges, [])

    def test_sts_must_echo_same_subject_audience_and_issuer(self):
        for field in ("SubjectFromWebIdentityToken", "Audience", "Provider"):
            self.response_change = lambda r:r.update({field:"wrong"})
            with self.subTest(field=field), self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.factories, [])

    def test_sts_positive_metadata_and_bound_credential_identity_required(self):
        self.response_change = lambda r:r.update(ResponseMetadata={})
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.factories, [])
        self.response_change = lambda r:r["AssumedRoleUser"].update(AssumedRoleId="different:techx-v2-123456-1")
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(len(self.factories), 1)

    def test_returned_credential_expiry_must_be_bounded(self):
        self.response_change = lambda r:r["Credentials"].update(Expiration=self.aws.now + timedelta(hours=8))
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.factories, [])

    def test_wrong_sts_or_scoped_service_endpoint_rejected(self):
        self.sts.meta.endpoint_url = "https://sts.us-west-2.amazonaws.com"
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.exchanges, [])
        self.sts.meta.endpoint_url = ENDPOINTS["sts"]
        self.factory_change = lambda c:setattr(c["ssm"].meta, "endpoint_url", "http://localhost:4566")
        with self.assertRaises(AuthenticationFailed): self.authenticate()

    def test_wrong_canonical_source_never_issues_operational_session(self):
        self.claims_changed(sha="b"*40, workflow_sha="b"*40)
        self.run["head_sha"] = "b"*40; self.listing["jobs"][0]["head_sha"] = "b"*40
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(len(self.exchanges), 1)
        self.assertEqual(self.aws.puts(), [])

    def test_run_must_be_main_current_same_repository_and_workflow(self):
        for key, value in (("status","completed"), ("head_branch","feature"), ("run_attempt",2), ("path",".github/workflows/other.yml")):
            old = self.run[key]; self.run[key] = value
            before = len(self.exchanges)
            with self.subTest(key=key), self.assertRaises(AuthenticationFailed): self.authenticate()
            self.assertEqual(len(self.exchanges) - before, 1)
            self.run[key] = old

    def test_job_list_must_be_complete_and_unambiguous(self):
        original = deepcopy(self.listing)
        for listing in (dict(total_count=2,jobs=original["jobs"]), dict(total_count=101,jobs=original["jobs"]),
                        dict(total_count=2,jobs=original["jobs"]*2)):
            self.listing = listing
            with self.assertRaises(AuthenticationFailed): self.authenticate()

    def test_self_hosted_or_completed_job_never_gets_operational_session(self):
        row = self.listing["jobs"][0]
        for key, value in (("labels",["self-hosted"]), ("runner_group_name","private"), ("status","completed"), ("run_attempt",2)):
            old = row[key]; row[key] = value
            before = len(self.exchanges)
            with self.assertRaises(AuthenticationFailed): self.authenticate()
            self.assertEqual(len(self.exchanges)-before, 1)
            row[key] = old

    def test_github_errors_do_not_leak_transport_secrets(self):
        self.github_error = True
        with self.assertRaises(AuthenticationFailed) as caught: self.authenticate()
        self.assertNotIn("secret-transport-token", str(caught.exception))
        self.assertEqual(len(self.exchanges), 1)

    def test_canceled_job_is_rechecked_before_new_mutation(self):
        auth = self.authenticate()
        self.listing["jobs"][0].update(status="completed", conclusion="cancelled", completed_at="2026-11-22T01:02:00Z")
        with self.assertRaises(AuthenticationFailed): auth.require_current(mutation=True)

    def test_replaced_job_id_is_not_original_manager(self):
        auth = self.authenticate(); self.listing["jobs"][0]["id"] += 1
        with self.assertRaises(AuthenticationFailed): auth.require_current(mutation=True)

    def test_expired_credential_or_cleanup_reserve_cannot_mutate(self):
        auth = self.authenticate(); self.aws.now += timedelta(minutes=45)
        with self.assertRaises(AuthenticationFailed): auth.require_current(mutation=True)

    def test_existing_closed_window_only_mints_observation_credentials(self):
        self.aws.now += timedelta(hours=8)
        self.run["created_at"] = wire_time(self.aws.now - timedelta(minutes=2))
        self.listing["jobs"][0]["started_at"] = wire_time(self.aws.now - timedelta(minutes=1))
        stamp = int(self.aws.now.timestamp()); self.claims_changed(iat=stamp-15, nbf=stamp-20, exp=stamp+285)
        auth = self.authenticate()
        self.assertTrue(auth.as_dict()["observeOnly"])
        self.assertIsNotNone(auth.require_current())
        with self.assertRaises(AuthenticationFailed): auth.require_current(mutation=True)
        operations = json.loads(self.exchanges[1]["Policy"])
        self.assertFalse(any("ssm:SendCommand" in s.get("Action", []) for s in operations["Statement"] if s["Effect"] == "Allow"))

    def test_operational_policy_has_server_side_window_denies_and_no_runinstances(self):
        policy = operation_policy(self.aws.execution, observe_only=False)
        self.assertLessEqual(len(encoded(policy)), 2048)
        denies = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
        self.assertEqual(len(denies), 4)
        self.assertTrue(any("DateGreaterThanEquals" in s["Condition"] for s in denies))
        self.assertNotIn("ec2:RunInstances", encoded(policy).decode())
        self.assertNotIn("s3:PutObject", encoded(bootstrap_policy()).decode())

    def test_actual_sdk_sts_model_accepts_exchange_parameters(self):
        import botocore.session
        from botocore.validate import validate_parameters
        self.authenticate()
        shape = botocore.session.get_session().get_service_model("sts").operation_model("AssumeRoleWithWebIdentity").input_shape
        for params in self.exchanges: validate_parameters(params, shape)

    def test_store_facade_rechecks_job_before_prepare_and_commit(self):
        self.aws.publish_session(dict(self.aws.record, status="READY"))
        auth = self.authenticate(); view = auth.session_store
        snapshot = view.read(); prepared = view.prepare(snapshot, "TESTING")
        self.run.update(status="completed", conclusion="cancelled")
        with self.assertRaises(AuthenticationFailed): view.commit(prepared)
        with self.assertRaises(AuthenticationFailed): view.prepare(snapshot, "TESTING")
        self.assertEqual(self.aws.puts(), [])

    def test_store_facade_keeps_durable_cas_for_authenticated_manager(self):
        self.aws.publish_session(dict(self.aws.record, status="READY"))
        auth = self.authenticate(); view = auth.session_store
        result = view.commit(view.prepare(view.read(), "TESTING"))
        self.assertEqual(result.as_dict()["status"], "TESTING")

    def test_job_closed_during_second_exchange_does_not_return_context(self):
        def change(response):
            if len(self.exchanges) == 2: self.run.update(status="completed", conclusion="cancelled")
        self.response_change = change
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(self.aws.puts(), [])

    def test_real_sdk_factories_do_not_use_default_credentials_or_connect(self):
        credentials = dict(AccessKeyId="ASIA-SYNTHETIC", SecretAccessKey="synthetic-secret", SessionToken="synthetic-session")
        with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("No live call permitted")):
            anonymous = unsigned_sts()
            self.assertIs(anonymous.meta.config.signature_version, UNSIGNED)
            clients = credential_clients(credentials)
        for service, client in clients.items():
            frozen = client._request_signer._credentials.get_frozen_credentials()
            self.assertEqual(frozen.access_key, credentials["AccessKeyId"])
            self.assertEqual(frozen.token, credentials["SessionToken"])
            self.assertEqual(client.meta.endpoint_url, f"https://{service}.us-east-1.amazonaws.com")
            self.assertEqual(client.meta.config.retries["total_max_attempts"], 1)

    def test_token_expires_during_context_reads_before_operational_exchange(self):
        original = self.github_http
        def delayed(request, timeout):
            result = original(request, timeout)
            if "/jobs?" in request.full_url: self.aws.now += timedelta(minutes=6)
            return result
        self.bridge = ManagerBridge("private-github-read-token", sts=self.sts, clients_factory=self.factory,
                                    open_url=delayed, clock=lambda:self.aws.now)
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(len(self.exchanges), 1)

    def test_low_level_clients_cannot_be_newly_acquired_for_a_closed_job(self):
        auth = self.authenticate()
        self.assertIs(auth.client("ssm"), auth._clients["ssm"])
        self.run.update(status="completed", conclusion="cancelled")
        with self.assertRaises(AuthenticationFailed): auth.client("ssm")
        with self.assertRaises(AuthenticationFailed): auth.client("iam")

    def test_45_minute_job_cap_cannot_be_extended_by_new_credentials(self):
        self.listing["jobs"][0]["started_at"] = "2026-11-22T00:16:00Z"
        self.run["created_at"] = "2026-11-22T00:15:00Z"
        with self.assertRaises(AuthenticationFailed): self.authenticate()
        self.assertEqual(len(self.exchanges), 1)

    def test_operational_policy_uses_earlier_job_deadline(self):
        self.authenticate()
        policy = json.loads(self.exchanges[1]["Policy"])
        deadlines = [s["Condition"]["DateGreaterThanEquals"]["aws:CurrentTime"] for s in policy["Statement"]
                     if "DateGreaterThanEquals" in s.get("Condition", {})]
        self.assertEqual(deadlines, ["2026-11-22T01:46:00Z"]*2)

    def test_issued_session_remains_usable_after_original_jwt_expiry_within_job_cap(self):
        auth = self.authenticate(); self.aws.now += timedelta(minutes=10)
        self.assertGreater(self.aws.now.timestamp(), self.claims["exp"])
        self.assertIsNotNone(auth.require_current(mutation=True))

    def test_same_job_id_with_changed_start_time_is_not_original_context(self):
        auth = self.authenticate()
        self.listing["jobs"][0]["started_at"] = "2026-11-22T01:01:01Z"
        with self.assertRaises(AuthenticationFailed): auth.require_current()


class HTTPTests(unittest.TestCase):
    def response(self, request, *, status=200, extra=None, payload=None, url=None):
        response = io.BytesIO(encoded(payload if payload is not None else dict(id=123)))
        response.status = status
        response.headers = dict({"Content-Type":"application/json; charset=utf-8", "X-GitHub-Request-Id":"test-request"}, **(extra or {}))
        response.geturl = lambda:url or request.full_url
        return response

    def test_fixed_get_and_authentication_headers(self):
        def opener(request, timeout):
            self.assertEqual(request.full_url, f"https://api.github.com/repos/{REPO}/actions/runs/123")
            self.assertEqual(request.method, "GET"); self.assertEqual(timeout, 20)
            self.assertEqual(request.get_header("Authorization"), "Bearer private-test-token")
            return self.response(request)
        self.assertEqual(GitHubRunReader("private-test-token", open_url=opener).get("actions/runs/123"), dict(id=123))

    def test_pagination_redirect_missing_metadata_or_http_error_is_not_complete(self):
        for options in (dict(extra={"Link":"next"}), dict(url="https://other.example/"), dict(status=302), dict(extra={"X-GitHub-Request-Id":""})):
            reader = GitHubRunReader("private-test-token", open_url=lambda request, timeout:self.response(request, **options))
            with self.subTest(options=options), self.assertRaises(AuthenticationFailed): reader.get("actions/runs/123")

    def test_arbitrary_paths_and_mutation_endpoints_are_rejected_before_http(self):
        def opener(*args, **kwargs): raise AssertionError("Must not send a request")
        reader = GitHubRunReader("private-test-token", open_url=opener)
        for endpoint in ("https://evil.example", "pulls/1/merge", "../actions/runs/123", "actions/runs/123/attempts/2/jobs?per_page=100"):
            with self.assertRaises(AuthenticationFailed): reader.get(endpoint)


if __name__ == "__main__": unittest.main()
