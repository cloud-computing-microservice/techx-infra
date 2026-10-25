"""Fixed-repository GitHub App clients for the private promotion executor."""
import base64
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.request import HTTPRedirectHandler, Request, build_opener

from automation_session import utc
from chart_selection import CHART_REPO, INFRA_REPO
from plan_review_store import ACCOUNT, encoded

ORG = "cloud-computing-microservice"
LIMIT = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def api(path, token, payload=None, *, method=None, open_url=None):
    if not re.fullmatch(r"/[A-Za-z0-9_./?=&%-]+", path) or ".." in path:
        raise ValueError("Invalid fixed GitHub API path")
    request = Request("https://api.github.com" + path, data=encoded(payload) if payload is not None else None,
        method=method or ("POST" if payload is not None else "GET"), headers={"Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10", "User-Agent": "techx-private-promotion"})
    opener = open_url or build_opener(NoRedirect()).open
    with opener(request, timeout=30) as response:
        raw = response.read(LIMIT + 1)
        if response.status not in (200, 201) or response.headers.get("Link") or len(raw) > LIMIT:
            raise ValueError("GitHub returned incomplete or unverified evidence; do not repeat a mutation")
    return json.loads(raw)


def jwt(secret, now, work, *, run=subprocess.run):
    def b64(data): return base64.urlsafe_b64encode(data).decode().rstrip("=")
    stamp = int(now.timestamp())
    body = b64(encoded(dict(alg="RS256", typ="JWT"))) + "." + b64(encoded(dict(iat=stamp - 60, exp=stamp + 540, iss=str(secret["appId"]))))
    # OpenSSL already belongs to the pinned executor recipe. Never put the key
    # or JWT in argv, logs or the public source/artifact bundle.
    Path(work).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="app-key-", dir=work) as directory:
        os.chmod(directory, 0o700)
        key = Path(directory) / "key.pem"
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as output: output.write(secret["privateKey"])
        signed = run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=body.encode(),
                     check=True, capture_output=True, timeout=15)
        if not 256 <= len(signed.stdout) <= 1024: raise ValueError("Unexpected App RSA signature size")
    return body + "." + b64(signed.stdout)


def app_secret(client, purpose, *, kms_key):
    if purpose not in ("promotion", "lifecycle"): raise ValueError("Unsupported private App purpose")
    name = f"techx/automation/{purpose}-app"
    metadata = client.describe_secret(SecretId=name)
    arn = metadata.get("ARN", "")
    tags = {row["Key"]: row["Value"] for row in metadata.get("Tags", [])}
    if (metadata.get("Name") != name or metadata.get("KmsKeyId") != kms_key
            or not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{name}-[A-Za-z0-9]{{6}}", arn)
            or metadata.get("DeletedDate") is not None
            or any(tags.get(key) != value for key, value in dict(Project="techx", Environment="automation", Role="github-app").items())):
        raise ValueError("Private App secret has a different name, account or control ownership")
    result = client.get_secret_value(SecretId=arn, VersionStage="AWSCURRENT")
    raw = result.get("SecretString", "")
    if (result.get("ARN") != arn or result.get("Name") != name or "AWSCURRENT" not in result.get("VersionStages", [])
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", result.get("VersionId", "")) or not isinstance(raw, str) or len(raw.encode()) > 32 * 1024):
        raise ValueError("Private App secret version/size is unverified")
    value = json.loads(raw)
    if (set(value) != {"schemaVersion", "appId", "slug", "installationId", "privateKey"}
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or type(value.get("appId")) is not int or value["appId"] < 1
            or type(value.get("installationId")) is not int or value["installationId"] < 1
            or not isinstance(value.get("slug"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value["slug"])
            or not isinstance(value.get("privateKey"), str)
            or not value["privateKey"].startswith(("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----"))
            or not value["privateKey"].isascii()):
        raise ValueError("Private App secret schema is invalid")
    return value


class RepositoryClient:
    def __init__(self, repository, token, expiry, clock, *, merge=False, transport=api):
        if repository not in (CHART_REPO, INFRA_REPO) or merge and repository != CHART_REPO or not token:
            raise ValueError("A fixed repository and scoped token are required")
        self.repository, self.token, self.expiry, self.clock = repository, token, expiry, clock
        self.can_merge, self.transport = merge, transport

    def request(self, endpoint, payload=None, **kwargs):
        if self.clock() + timedelta(seconds=30) >= self.expiry: raise ValueError("Scoped App token has expired")
        if payload is not None and (not self.can_merge or self.repository != CHART_REPO or kwargs.get("method") != "PUT"
                or not re.fullmatch(r"pulls/[1-9][0-9]*/merge", endpoint)
                or set(payload) != {"sha", "merge_method", "commit_title", "commit_message"} or payload.get("merge_method") != "merge"
                or not re.fullmatch(r"[a-f0-9]{40}", payload.get("sha", ""))
                or not re.fullmatch(r"TechX promotion [a-f0-9]{64}", payload.get("commit_title", ""))
                or not re.fullmatch(r"Session staging-[A-Za-z0-9-]{1,64}; task [a-f0-9]{32}", payload.get("commit_message", ""))):
            raise ValueError("Repository client permits only the fixed exact-head merge endpoint")
        return self.transport(f"/repos/{self.repository}/{endpoint}", self.token, payload, **kwargs)

    def get(self, endpoint): return self.request(endpoint)

    def merge(self, number, head, title, message):
        if (not self.can_merge or self.repository != CHART_REPO or type(number) is not int or number < 1
                or not re.fullmatch(r"[a-f0-9]{40}", head) or not re.fullmatch(r"TechX promotion [a-f0-9]{64}", title)
                or not re.fullmatch(r"Session staging-[A-Za-z0-9-]{1,64}; task [a-f0-9]{32}", message)):
            raise ValueError("Only an exact reviewed chart merge is allowed")
        return self.request(f"pulls/{number}/merge", dict(sha=head, merge_method="merge", commit_title=title, commit_message=message), method="PUT")


def installation_client(secret, repository, work, clock, *, merge=False, transport=api, sign=jwt):
    if repository not in (CHART_REPO, INFRA_REPO) or merge and repository != CHART_REPO:
        raise ValueError("Only fixed chart promotion and infra review clients may be minted")
    bearer = sign(secret, clock(), work)
    app = transport("/app", bearer)
    if app.get("id") != secret["appId"] or app.get("slug") != secret["slug"] or app.get("owner", {}).get("login") != ORG:
        raise ValueError("Signing key does not belong to the expected organization App")
    installation = transport(f"/app/installations/{secret['installationId']}", bearer)
    if (installation.get("id") != secret["installationId"] or installation.get("app_id") != secret["appId"]
            or installation.get("account", {}).get("login") != ORG or installation.get("repository_selection") != "selected"
            or installation.get("suspended_at") is not None):
        raise ValueError("App installation is not the selected, active organization installation")
    permissions = dict(contents="write" if merge else "read", actions="read")
    if repository == CHART_REPO: permissions.update(pull_requests="read", administration="read")
    result = transport(f"/app/installations/{secret['installationId']}/access_tokens", bearer,
        dict(repositories=[repository.split("/")[1]], permissions=permissions))
    reported = result.get("permissions")
    expiry = utc(result["expires_at"])
    if (not isinstance(reported, dict) or {key: value for key, value in reported.items() if key != "metadata"} != permissions
            or reported.get("metadata", "read") != "read" or result.get("repository_selection", "selected") != "selected"
            or "repositories" in result and (not isinstance(result["repositories"], list) or len(result["repositories"]) != 1
                or result["repositories"][0].get("full_name") != repository)
            or not isinstance(result.get("token"), str) or not 20 <= len(result["token"]) <= 512
            or not clock() + timedelta(minutes=5) < expiry <= clock() + timedelta(hours=1, minutes=1)):
        raise ValueError("Installation token did not retain the exact repository, permissions and expiry")
    actual = transport("/installation/repositories?per_page=2", result["token"])
    if (type(actual.get("total_count")) is not int or actual["total_count"] != 1
            or not isinstance(actual.get("repositories"), list) or len(actual["repositories"]) != 1
            or actual["repositories"][0].get("full_name") != repository):
        raise ValueError("Actual installation token can access a different repository scope")
    return RepositoryClient(repository, result["token"], expiry, clock, merge=merge, transport=transport)
