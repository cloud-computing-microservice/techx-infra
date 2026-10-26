"""Cloud-free admission of one dedicated promotion App; never mint merge tokens."""
from copy import deepcopy
from datetime import datetime, timezone
import os
import re

from chart_selection import CHART_REPO
from github_app_auth import api, installation_client, jwt

FIELDS = {"schemaVersion", "repository", "appId", "appSlug", "installationId", "policySha256"}
MAXIMUM = dict(contents="write", pull_requests="read", actions="read", administration="read")
READ_ONLY = dict(MAXIMUM, contents="read")


def authority(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1
            or value.get("repository") != CHART_REPO
            or any(type(value.get(key)) is not int or value[key] < 1 for key in ("appId", "installationId"))
            or not isinstance(value.get("appSlug"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value["appSlug"])
            or not isinstance(value.get("policySha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", value["policySha256"])):
        raise ValueError("An exact public promotion authority proof is required")
    return deepcopy(value)


def admitted(execution):
    return authority(execution.get("sourceAdmission", {}).get("promotionAuthority"))


def check(secret, work, clock, *, transport=api, sign=jwt):
    # This credential comes only from a protected main environment. The App's
    # actual identity/installation is verified by the existing JWT adapter.
    if (not isinstance(secret, dict) or set(secret) != {"appId", "slug", "installationId", "privateKey"}
            or any(type(secret.get(key)) is not int or secret[key] < 1 for key in ("appId", "installationId"))
            or not isinstance(secret.get("slug"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", secret["slug"])
            or not isinstance(secret.get("privateKey"), str) or not secret["privateKey"].isascii()
            or len(secret["privateKey"].encode()) > 32 * 1024
            or not secret["privateKey"].startswith(("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----"))):
        raise ValueError("Configure the dedicated promotion App identity, installation and protected key")
    install_path = f"/app/installations/{secret['installationId']}"
    token_path = install_path + "/access_tokens"
    issued = []

    def guarded(path, token, payload=None, **kwargs):
        if kwargs or payload is not None and (path != token_path or payload != dict(repositories=["techx-chart"], permissions=READ_ONLY)):
            raise ValueError("Admission may mint only one chart-only read token; no merge mutation")
        result = transport(path, token, payload)
        if path == install_path:
            permissions = result.get("permissions")
            if (not isinstance(permissions, dict) or {k: v for k, v in permissions.items() if k != "metadata"} != MAXIMUM
                    or permissions.get("metadata", "read") != "read"):
                raise ValueError("Promotion installation lacks the exact reviewed maximum permissions")
        if path == token_path:
            value = result.get("token")
            if isinstance(value, str) and 20 <= len(value) <= 512: issued.append(value)
        return result

    from chart_promotion import protection  # Avoid an admission/checker import cycle.
    try:
        chart = installation_client(secret, CHART_REPO, work, clock, transport=guarded, sign=sign)
        first = protection(chart, secret["appId"], secret["slug"])
        # Repeat identity and installation after external policy/scope reads.
        # No token is minted by these final JWT-authenticated reads.
        bearer = sign(secret, clock(), work)
        app = guarded("/app", bearer)
        installation = guarded(install_path, bearer)
        if (app.get("id") != secret["appId"] or app.get("slug") != secret["slug"]
                or app.get("owner", {}).get("login") != "cloud-computing-microservice"
                or installation.get("id") != secret["installationId"] or installation.get("app_id") != secret["appId"]
                or installation.get("account", {}).get("login") != "cloud-computing-microservice"
                or installation.get("repository_selection") != "selected" or installation.get("suspended_at") is not None
                or protection(chart, secret["appId"], secret["slug"]) != first):
            raise ValueError("Promotion App, installation or protected main changed during preflight")
        result = authority(dict(schemaVersion=1, repository=CHART_REPO, installationId=secret["installationId"], **first))
    finally:
        # Even invalid token scope/permissions are revoked if creation returned
        # a token. A lost creation response is never blindly retried.
        for token in issued:
            if transport("/installation/token", token, method="DELETE") is not None:
                raise ValueError("Installation token revocation returned unverified evidence")
    return result


def from_environment(work):
    values = {}
    for name, key in (("PROMOTION_APP_ID", "appId"), ("PROMOTION_INSTALLATION_ID", "installationId")):
        value = os.environ.get(name, "")
        if not re.fullmatch(r"[1-9][0-9]*", value): raise ValueError("Actual promotion App/installation metadata must be configured")
        values[key] = int(value)
    values.update(slug=os.environ.get("PROMOTION_APP_SLUG", ""), privateKey=os.environ.get("PROMOTION_APP_PRIVATE_KEY", ""))
    return check(values, work, lambda: datetime.now(timezone.utc))
