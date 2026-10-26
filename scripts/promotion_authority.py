"""Cloud-free admission of one dedicated promotion App; never mint merge tokens."""
from copy import deepcopy
from datetime import datetime, timezone
import os
import re

from chart_selection import CHART_REPO, INFRA_REPO, PLATFORM_REPO
from github_app_auth import api, jwt, readonly_installation

FIELDS = {"schemaVersion", "repository", "appId", "appSlug", "installationId", "policySha256"}
MAXIMUM = dict(contents="write", pull_requests="read", actions="read", administration="read")
READ_ONLY = dict(MAXIMUM, contents="read")
LIFECYCLE_MAXIMUM = dict(contents="read", actions="write")


def identity(value):
    if (not isinstance(value, dict) or set(value) != {"appId", "appSlug", "installationId"}
            or any(type(value.get(key)) is not int or value[key] < 1 for key in ("appId", "installationId"))
            or not isinstance(value.get("appSlug"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", value["appSlug"])):
        raise ValueError("An exact admitted App identity is required")
    return deepcopy(value)


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


def credentials(secret):
    # This credential comes only from a protected main environment. The App's
    # actual identity/installation is verified by the existing JWT adapter.
    if (not isinstance(secret, dict) or set(secret) != {"appId", "slug", "installationId", "privateKey"}
            or any(type(secret.get(key)) is not int or secret[key] < 1 for key in ("appId", "installationId"))
            or not isinstance(secret.get("slug"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", secret["slug"])
            or not isinstance(secret.get("privateKey"), str) or not secret["privateKey"].isascii()
            or len(secret["privateKey"].encode()) > 32 * 1024
            or not secret["privateKey"].startswith(("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----"))):
        raise ValueError("Configure the dedicated promotion App identity, installation and protected key")
    return identity(dict(appId=secret["appId"], appSlug=secret["slug"], installationId=secret["installationId"]))


def check(secret, work, clock, *, transport=api, sign=jwt):
    credentials(secret)
    install_path = f"/app/installations/{secret['installationId']}"

    from chart_promotion import protection  # Avoid an admission/checker import cycle.
    with readonly_installation(secret, CHART_REPO, MAXIMUM, work, clock, transport=transport, sign=sign) as (chart, guarded):
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
    return result


def check_lifecycle(secret, work, clock, *, transport=api, sign=jwt):
    result = credentials(secret)
    for repository in (INFRA_REPO, PLATFORM_REPO):
        with readonly_installation(secret, repository, LIFECYCLE_MAXIMUM, work, clock, transport=transport, sign=sign):
            pass  # JWT identity, maximum grants, actual token scope and revocation are mandatory.
    return result


def _environment(prefix):
    values = {}
    for name, key in ((prefix + "_APP_ID", "appId"), (prefix + "_INSTALLATION_ID", "installationId")):
        value = os.environ.get(name, "")
        if not re.fullmatch(r"[1-9][0-9]*", value): raise ValueError("Actual promotion App/installation metadata must be configured")
        values[key] = int(value)
    values.update(slug=os.environ.get(prefix + "_APP_SLUG", ""), privateKey=os.environ.get(prefix + "_APP_PRIVATE_KEY", ""))
    return values


def from_environment(work):
    return check(_environment("PROMOTION"), work, lambda: datetime.now(timezone.utc))


def lifecycle_from_environment(work):
    return check_lifecycle(_environment("LIFECYCLE"), work, lambda: datetime.now(timezone.utc))
