"""Own STS/GitHub authentication for v2 managers; no claims-as-JSON authority.

AWS verifies the SAME raw GitHub JWT. Bootstrap credentials are read-only until
canonical execution/window and live run/job observations agree. Operational
credentials have bounded session-policy capabilities and server-side time denies.
No JWT/credential payload is persisted, returned as evidence or logged here.
R05/R06 must still enforce the underlying IAM, environment and workflow layout.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import base64
import json
import re
from urllib.request import HTTPRedirectHandler, Request, build_opener

from botocore import UNSIGNED

from execution_v2 import ACCOUNT, BUCKET, REGION, execution_reference, timestamp
from recovery_v2 import service_time, wire_time
from session_runtime_v2 import ENDPOINTS, SessionStore, encoded, positive
from worker_identity import ORG, REPO, manager_identity

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "sts.amazonaws.com"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/techx-automation-v2-manager"
BINDINGS = {("staging-lifecycle.yml", "start"):"staging-start",
            ("staging-lifecycle.yml", "foundation-plan"):"staging-lifecycle",
            ("staging-continue.yml", "continue"):"staging-lifecycle",
            ("staging-orchestrate.yml", "observe"):"staging-lifecycle",
            ("staging-acceptance.yml", "public"):"staging-acceptance",
            ("staging-destroy.yml", "cleanup"):"staging-destroy",
            ("staging-plan-review.yml", "preview"):"staging-plan-read",
            ("staging-plan-review.yml", "approve"):"staging-apply"}
SERVICES = {"s3", "sts", "ssm", "lambda", "ec2", "iam"}
SERVICE_ENDPOINTS = {s:("https://iam.amazonaws.com" if s == "iam" else f"https://{s}.{REGION}.amazonaws.com") for s in SERVICES}
S3_READ_ACTIONS = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectTagging", "s3:GetObjectVersionTagging"]
READ_ACTIONS = ["sts:GetCallerIdentity", *S3_READ_ACTIONS,
                "ssm:ListCommands", "ssm:ListCommandInvocations", "ssm:GetCommandInvocation", "ssm:DescribeDocument",
                "ssm:DescribeInstanceInformation", "ec2:DescribeInstances", "iam:GetInstanceProfile", "s3:ListBucketVersions"]
WRITE_ACTIONS = ["s3:PutObject", "s3:PutObjectTagging", "ssm:SendCommand"]
# Managers invoke authenticated ingress only. Autonomous/internal launchers
# must not accept unauthenticated manager-shaped JSON on the same IAM route.
LIFECYCLE = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:techx-automation-v2-manager-lifecycle"
CLEANUP = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:techx-automation-v2-manager-cleanup-request"
_SEAL = object()


class AuthenticationFailed(ValueError):
    """Only fixed codes, never SDK/HTTP messages containing credentials."""


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise AuthenticationFailed("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    def constant(_): raise AuthenticationFailed("NONFINITE_JSON_VALUE")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class GitHubRunReader:
    """Fixed-repository GET transport; bounded complete job pages or fail closed."""
    def __init__(self, token, *, open_url=None):
        if not isinstance(token, str) or not token or any(c.isspace() for c in token): raise AuthenticationFailed("GITHUB_TOKEN_REQUIRED")
        self._token = token
        self._open = open_url or build_opener(NoRedirect()).open

    def get(self, endpoint):
        if not re.fullmatch(r"actions/runs/[1-9][0-9]{0,19}(?:/attempts/1/jobs\?per_page=100)?", endpoint):
            raise AuthenticationFailed("UNSUPPORTED_GITHUB_READ")
        url = f"https://api.github.com/repos/{REPO}/{endpoint}"
        request = Request(url, method="GET", headers={"Authorization":"Bearer " + self._token,
            "Accept":"application/vnd.github+json", "X-GitHub-Api-Version":"2026-03-10", "User-Agent":"techx-v2-manager"})
        try:
            with self._open(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if (response.status != 200 or response.geturl() != url or response.headers.get("Link")
                        or not response.headers.get("X-GitHub-Request-Id")
                        or response.headers.get("Content-Type", "").split(";")[0] != "application/json"
                        or len(raw) > 2 * 1024 * 1024):
                    raise AuthenticationFailed("INCOMPLETE_GITHUB_RESPONSE")
            value = strict_json(raw)
            if not isinstance(value, dict): raise AuthenticationFailed("INVALID_GITHUB_RESPONSE")
            return value
        except AuthenticationFailed: raise
        except Exception: raise AuthenticationFailed("GITHUB_READ_FAILED") from None


def token_claims(token, workflow, job, now):
    """Strict preflight only. Claims become trusted only after STS acceptance."""
    if (workflow, job) not in BINDINGS: raise AuthenticationFailed("UNREVIEWED_MANAGER_JOB")
    if not isinstance(token, str) or not 1 <= len(token) <= 20000 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise AuthenticationFailed("INVALID_OIDC_TOKEN")
    def decode(part):
        return strict_json(base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True))
    try:
        header, claims = (decode(p) for p in token.split(".")[:2])
        if (not isinstance(header, dict) or header.get("alg") != "RS256" or header.get("typ") != "JWT"
                or set(header) - {"alg", "typ", "kid", "x5t"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", str(header.get("kid", "")))
                or not isinstance(claims, dict)): raise AuthenticationFailed("UNSUPPORTED_OIDC_HEADER")
        environment = BINDINGS[(workflow, job)]
        expected = dict(iss=ISSUER, aud=AUDIENCE, repository=REPO, repository_id="1322975868", repository_owner_id="328679702",
                        ref="refs/heads/main", run_attempt="1", environment=environment, runner_environment="github-hosted",
                        workflow_ref=f"{REPO}/.github/workflows/{workflow}@refs/heads/main",
                        sub=f"repo:{ORG}@328679702/techx-infra@1322975868:environment:{environment}")
        if any(claims.get(k) != v for k,v in expected.items()): raise AuthenticationFailed("OIDC_CONTEXT_MISMATCH")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", str(claims.get("run_id", ""))) or not isinstance(claims.get("run_id"), str):
            raise AuthenticationFailed("INVALID_OIDC_RUN")
        if not re.fullmatch(r"[a-f0-9]{40}", str(claims.get("sha", ""))) or claims.get("workflow_sha") != claims["sha"]:
            raise AuthenticationFailed("OIDC_SOURCE_MISMATCH")
        if claims.get("event_name") not in {"workflow_dispatch", "workflow_run", "repository_dispatch"}:
            raise AuthenticationFailed("UNSUPPORTED_MANAGER_EVENT")
        if (not isinstance(now, datetime) or now.tzinfo is None or any(type(claims.get(k)) is not int for k in ("iat", "nbf", "exp"))
                or not now.timestamp() - 600 <= claims["iat"] <= now.timestamp() + 30
                or not claims["iat"] - 60 <= claims["nbf"] <= min(claims["iat"] + 30, now.timestamp() + 30)
                or not now.timestamp() < claims["exp"] <= claims["iat"] + 600):
            raise AuthenticationFailed("OIDC_TOKEN_TIME_INVALID")
        return claims
    except AuthenticationFailed: raise
    except Exception: raise AuthenticationFailed("INVALID_OIDC_TOKEN") from None


def bootstrap_policy():
    return dict(Version="2012-10-17", Statement=[dict(Effect="Allow", Action="sts:GetCallerIdentity", Resource="*"),
                 dict(Effect="Allow", Action=S3_READ_ACTIONS, Resource=f"arn:aws:s3:::{BUCKET}/automation/v2/*")])


def operation_policy(execution, *, observe_only, job_deadline=None):
    statements = [dict(Effect="Allow", Action=READ_ACTIONS + ([] if observe_only else WRITE_ACTIONS), Resource="*"),
                  dict(Effect="Allow", Action="lambda:InvokeFunction", Resource=[CLEANUP] if observe_only else [CLEANUP, LIFECYCLE])]
    if not observe_only:
        cutoff = timestamp(execution["cleanupStartAt"])
        if job_deadline is not None: cutoff = min(cutoff, job_deadline)
        for operator, stamp in (("DateLessThan", execution["startedAt"]), ("DateGreaterThanEquals", wire_time(cutoff))):
            condition = {operator:{"aws:CurrentTime":stamp}}
            statements.extend([dict(Effect="Deny", Action=WRITE_ACTIONS, Resource="*", Condition=condition),
                               dict(Effect="Deny", Action="lambda:InvokeFunction", Resource=LIFECYCLE, Condition=condition)])
    value = dict(Version="2012-10-17", Statement=statements)
    if len(encoded(value)) > 2048: raise AuthenticationFailed("SESSION_POLICY_TOO_LARGE")
    return value


def unsigned_sts():
    import boto3
    from botocore.config import Config
    return boto3.client("sts", region_name=REGION, endpoint_url=ENDPOINTS["sts"],
                        config=Config(signature_version=UNSIGNED, retries={"total_max_attempts":1}, connect_timeout=5, read_timeout=20))


def credential_clients(credentials):
    import boto3
    from botocore.config import Config
    session = boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"],
                            aws_session_token=credentials["SessionToken"], region_name=REGION)
    config = Config(retries={"total_max_attempts":1}, connect_timeout=5, read_timeout=20)
    return {s:session.client(s, region_name=REGION, endpoint_url=SERVICE_ENDPOINTS[s], config=config) for s in SERVICES}


def github_context(reader, claims, workflow, job, *, now, expected_job_id=None):
    try:
        run = reader.get(f"actions/runs/{claims['run_id']}")
        listing = reader.get(f"actions/runs/{claims['run_id']}/attempts/1/jobs?per_page=100")
    except Exception: raise AuthenticationFailed("GITHUB_CONTEXT_UNAVAILABLE") from None
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != int(claims["run_id"])
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != 1
            or run.get("status") != "in_progress" or run.get("conclusion") is not None
            or run.get("event") != claims["event_name"] or run.get("head_branch") != "main" or run.get("head_sha") != claims["sha"]
            or run.get("path") != f".github/workflows/{workflow}"
            or run.get("repository", {}).get("id") != 1322975868 or run.get("repository", {}).get("owner", {}).get("id") != 328679702
            or run.get("head_repository", {}).get("id") != 1322975868):
        raise AuthenticationFailed("GITHUB_RUN_MISMATCH")
    if (not isinstance(listing, dict) or type(listing.get("total_count")) is not int or not isinstance(listing.get("jobs"), list)
            or not 1 <= listing["total_count"] <= 100 or listing["total_count"] != len(listing["jobs"])):
        raise AuthenticationFailed("INCOMPLETE_GITHUB_JOB_LIST")
    ids, selected = set(), []
    for row in listing["jobs"]:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in ids
                or type(row.get("run_id")) is not int or row["run_id"] != run["id"]
                or type(row.get("run_attempt")) is not int or row["run_attempt"] != 1 or row.get("head_sha") != claims["sha"]):
            raise AuthenticationFailed("GITHUB_JOB_SCOPE_MISMATCH")
        ids.add(row["id"])
        if row.get("name") == job: selected.append(row)
    if len(selected) != 1: raise AuthenticationFailed("AMBIGUOUS_MANAGER_JOB")
    selected = selected[0]
    if (selected.get("status") != "in_progress" or selected.get("conclusion") is not None or selected.get("completed_at") is not None
            or selected.get("runner_group_name") != "GitHub Actions" or selected.get("labels") != ["ubuntu-24.04"]
            or expected_job_id is not None and selected["id"] != expected_job_id):
        raise AuthenticationFailed("MANAGER_JOB_NOT_CURRENT")
    started = service_time(selected.get("started_at"))
    if not service_time(run.get("created_at")) <= started <= now < started + timedelta(minutes=45):
        raise AuthenticationFailed("GITHUB_JOB_TIME_MISMATCH")
    if claims["iat"] + 30 < service_time(selected["started_at"]).timestamp(): raise AuthenticationFailed("TOKEN_PREDATES_MANAGER_JOB")
    return dict(repositoryId="1322975868", ownerId="328679702", workflow=workflow, job=job, runId=claims["run_id"], runAttempt=1,
                environment=BINDINGS[(workflow, job)], headSha=claims["sha"]), selected["id"], started + timedelta(minutes=45)


class VerifiedManager:
    """Read-only credential verifier; delegated admission is not an SDK capability."""
    __slots__ = ("_identity", "_store", "_expiry", "_clients", "_reader", "_claims", "_workflow", "_job", "_job_id", "_clock", "_observe_only", "_job_deadline")

    def __init__(self, identity, store, expiry, clients, reader, claims, workflow, job, job_id, clock, observe_only, *, job_deadline=None, _seal=None):
        if _seal is not _SEAL: raise AuthenticationFailed("USE_AUTHENTICATED_BRIDGE")
        self._identity, self._store, self._expiry, self._clients = deepcopy(identity), store, expiry, clients
        self._reader, self._claims, self._workflow, self._job, self._job_id = reader, deepcopy(claims), workflow, job, job_id
        self._clock, self._observe_only = clock, observe_only
        self._job_deadline = job_deadline

    def as_dict(self):
        """Diagnostics only: this serialized dictionary is not remote authority."""
        return dict(identity=deepcopy(self._identity), credentialExpiresAt=wire_time(self._expiry), observeOnly=self._observe_only)

    @property
    def execution(self): return self._store.execution

    @property
    def execution_reference(self): return self._store.execution_reference

    def require_current(self, *, mutation=False):
        try:
            now = self._clock()
            if now + timedelta(seconds=30) >= self._expiry: raise AuthenticationFailed("MANAGER_CREDENTIAL_EXPIRED")
            context, _, job_deadline = github_context(self._reader, self._claims, self._workflow, self._job, now=now, expected_job_id=self._job_id)
            if job_deadline != self._job_deadline: raise AuthenticationFailed("MANAGER_JOB_START_CHANGED")
            caller = self._clients["sts"].get_caller_identity(); positive(caller)
            if manager_identity(context, self._claims, caller, self._store.execution) != self._identity:
                raise AuthenticationFailed("MANAGER_IDENTITY_CHANGED")
            observed = self._store.read()
            if mutation and (self._observe_only or not timestamp(self._store.execution["startedAt"]) <= now < timestamp(self._store.execution["cleanupStartAt"])
                             or observed.as_dict()["status"] not in {"PLANNING", "PROVISIONING", "READY", "TESTING"}):
                raise AuthenticationFailed("SESSION_CLOSED_FOR_MANAGER_MUTATION")
            return observed
        except AuthenticationFailed: raise
        except Exception: raise AuthenticationFailed("MANAGER_REVALIDATION_FAILED") from None


class AuthenticatedManager(VerifiedManager):
    __slots__ = ()

    @property
    def session_store(self): return _ManagerStoreView(self)

    def client(self, service):
        if service not in SERVICES: raise AuthenticationFailed("UNSUPPORTED_MANAGER_SERVICE")
        self.require_current()
        return self._clients[service]


class ManagerBridge:
    def __init__(self, github_token, *, sts=None, clients_factory=credential_clients, open_url=None, clock=lambda:datetime.now(timezone.utc)):
        self._sts, self._factory, self._clock = sts, clients_factory, clock
        self._reader = GitHubRunReader(github_token, open_url=open_url)

    def _exchange(self, token, claims, policy):
        sts = self._sts or unsigned_sts()
        if (sts.meta.endpoint_url != ENDPOINTS["sts"] or sts.meta.region_name != REGION
                or sts.meta.config.signature_version is not UNSIGNED or sts.meta.config.retries.get("total_max_attempts") != 1):
            raise AuthenticationFailed("UNTRUSTED_OIDC_STS_CLIENT")
        name = f"techx-v2-{claims['run_id']}-1"
        expected_arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/techx-automation-v2-manager/{name}"
        try:
            response = sts.assume_role_with_web_identity(RoleArn=ROLE, RoleSessionName=name, WebIdentityToken=token,
                                                        DurationSeconds=2700, Policy=encoded(policy).decode())
            positive(response)
            if (response.get("SubjectFromWebIdentityToken") != claims["sub"] or response.get("Audience") != AUDIENCE
                    or response.get("Provider") != ISSUER or response.get("AssumedRoleUser", {}).get("Arn") != expected_arn):
                raise AuthenticationFailed("STS_TOKEN_BINDING_MISMATCH")
            user_id = response["AssumedRoleUser"].get("AssumedRoleId")
            credentials = response.get("Credentials")
            if (not isinstance(user_id, str) or not user_id.endswith(":" + name) or not isinstance(credentials, dict)
                    or any(not isinstance(credentials.get(k), str) or not credentials[k] for k in ("AccessKeyId", "SecretAccessKey", "SessionToken"))):
                raise AuthenticationFailed("INVALID_STS_CREDENTIAL_RESPONSE")
            expiry = credentials.get("Expiration")
            now = self._clock()
            if not isinstance(expiry, datetime) or expiry.tzinfo is None or not now + timedelta(seconds=30) < expiry <= now + timedelta(seconds=2820):
                raise AuthenticationFailed("STS_CREDENTIAL_TIME_INVALID")
            clients = self._factory(credentials)
            for service in SERVICES:
                client = clients[service]
                if (client.meta.endpoint_url != SERVICE_ENDPOINTS[service] or client.meta.region_name != REGION
                        or client.meta.config.retries.get("total_max_attempts") != 1):
                    raise AuthenticationFailed("UNTRUSTED_SCOPED_AWS_CLIENT")
            caller = clients["sts"].get_caller_identity(); positive(caller)
            if caller.get("Account") != ACCOUNT or caller.get("Arn") != expected_arn or caller.get("UserId") != user_id:
                raise AuthenticationFailed("STS_CREDENTIAL_IDENTITY_MISMATCH")
            return clients, expiry
        except AuthenticationFailed: raise
        except Exception: raise AuthenticationFailed("OIDC_EXCHANGE_FAILED") from None

    def verify(self, token, execution_ref, *, workflow, job):
        """Receiver path: validate identity using read-only credentials only."""
        try:
            claims = token_claims(token, workflow, job, self._clock())
            sid = execution_ref.get("key", "").removeprefix("automation/v2/staging/executions/").removesuffix(".json")
            execution_reference(execution_ref, sid)
            # Never issue operational credentials from unverified job/source data.
            bootstrap, bootstrap_expiry = self._exchange(token, claims, bootstrap_policy())
            initial = SessionStore(bootstrap, execution_ref, clock=self._clock)
            context, job_id, job_deadline = github_context(self._reader, claims, workflow, job, now=self._clock())
            caller = bootstrap["sts"].get_caller_identity(); positive(caller)
            identity = manager_identity(context, claims, caller, initial.execution)
            now = self._clock()
            observe_only = (workflow in ("staging-destroy.yml", "staging-orchestrate.yml", "staging-plan-review.yml")
                            or not timestamp(initial.execution["startedAt"]) <= now < timestamp(initial.execution["cleanupStartAt"]))
            token_claims(token, workflow, job, now)  # No stale token reuse after slow context reads.
            # This path never issues operational credentials to the receiver.
            result = VerifiedManager(identity, initial, bootstrap_expiry, bootstrap, self._reader, claims,
                                     workflow, job, job_id, self._clock, observe_only, job_deadline=job_deadline, _seal=_SEAL)
            result.require_current()
            return result
        except AuthenticationFailed: raise
        except Exception: raise AuthenticationFailed("MANAGER_AUTHENTICATION_FAILED") from None

    def authenticate(self, token, execution_ref, *, workflow, job):
        try:
            verified = self.verify(token, execution_ref, workflow=workflow, job=job)
            claims = token_claims(token, workflow, job, self._clock())
            clients, expiry = self._exchange(token, claims, operation_policy(verified.execution, observe_only=verified._observe_only,
                                                                           job_deadline=verified._job_deadline))
            store = SessionStore(clients, execution_ref, clock=self._clock)
            if store.execution != verified.execution: raise AuthenticationFailed("EXECUTION_CHANGED_DURING_AUTHENTICATION")
            result = AuthenticatedManager(verified._identity, store, expiry, clients, self._reader, claims, workflow, job, verified._job_id,
                                          self._clock, verified._observe_only, job_deadline=verified._job_deadline, _seal=_SEAL)
            result.require_current()
            return result
        except AuthenticationFailed: raise
        except Exception: raise AuthenticationFailed("MANAGER_AUTHENTICATION_FAILED") from None


class _ManagerStoreView:
    """No unguarded state-transition convenience API escapes the manager bridge."""
    __slots__ = ("_owner",)

    def __init__(self, owner): self._owner = owner

    @property
    def execution(self): return self._owner._store.execution

    @property
    def execution_reference(self): return self._owner._store.execution_reference

    def read(self): return self._owner.require_current()

    def prepare(self, snapshot, target):
        current = self._owner.require_current(mutation=True)
        if current.reference != snapshot.reference: raise AuthenticationFailed("MANAGER_SESSION_CONTEXT_CHANGED")
        return self._owner._store.prepare(snapshot, target)

    def commit(self, prepared):
        self._owner.require_current(mutation=True)
        return self._owner._store.commit(prepared)

    def reconcile(self, prepared):
        self._owner.require_current()
        return self._owner._store.reconcile(prepared)

    def recover(self, mutation_id):
        self._owner.require_current()
        return self._owner._store.recover(mutation_id)
