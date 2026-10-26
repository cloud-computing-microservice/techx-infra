"""Verify existing control App keys before any foundation Terraform or assets."""
from copy import deepcopy
import re

from bootstrap_assets import save_private
from github_app_auth import api, app_secret_snapshot, jwt
from plan_approval import version
from plan_review_store import ACCOUNT
from promotion_authority import admitted, check, check_lifecycle, identity


def reference(value, purpose):
    name = f"techx/automation/{purpose}-app"
    if (not isinstance(value, dict) or set(value) != {"arn", "versionId"}
            or not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{name}-[A-Za-z0-9]{{6}}", value.get("arn", ""))
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", value.get("versionId", ""))):
        raise ValueError("Control App key requires an exact fixed-name version reference")
    return deepcopy(value)


def credential_proof(private, execution, execution_version):
    proof = private.get("automationCredentials")
    if (not isinstance(proof, dict) or set(proof) != {"schemaVersion", "sessionId", "executionVersion", "sourceRevisions", "promotion", "lifecycle"}
            or type(proof.get("schemaVersion")) is not int or proof["schemaVersion"] != 1
            or proof.get("sessionId") != execution["sessionId"] or proof.get("executionVersion") != execution_version
            or proof.get("sourceRevisions") != execution["sourceRevisions"]):
        raise ValueError("Private credential proof differs from this immutable execution")
    version(proof["executionVersion"])
    for purpose in ("promotion", "lifecycle"):
        item = proof.get(purpose)
        field = "authority" if purpose == "promotion" else "identity"
        if not isinstance(item, dict) or set(item) != {"secret", field}:
            raise ValueError("Private credential proof must contain only public metadata and key references")
        reference(item["secret"], purpose)
    if (proof["promotion"]["authority"] != admitted(execution)
            or identity(proof["lifecycle"]["identity"]) != identity(execution.get("sourceAdmission", {}).get("lifecycleIdentity"))):
        raise ValueError("Private App identities differ from the human-reviewed opening")
    return deepcopy(proof)


def verify(runtime, guard, clock, *, transport=api, sign=jwt):
    runtime.executor()
    record = guard()
    execution_version = record.get("executionVersion")
    version(execution_version)
    if record.get("sessionId") != runtime.execution["sessionId"]:
        raise ValueError("Private credential check belongs to another session")
    expected_promotion = admitted(runtime.execution)
    expected_lifecycle = identity(runtime.execution.get("sourceAdmission", {}).get("lifecycleIdentity"))
    prior = runtime.private.get("automationCredentials")
    if prior is not None: credential_proof(runtime.private, runtime.execution, execution_version)
    values, refs = {}, {}
    for purpose in ("promotion", "lifecycle"):
        guard()
        values[purpose], refs[purpose] = app_secret_snapshot(runtime.clients["secretsmanager"], purpose, kms_key=runtime.kms)
        reference(refs[purpose], purpose)
        if prior and refs[purpose] != prior[purpose]["secret"]:
            raise ValueError("Control App key version changed; never replace a session credential pin")
    def guarded(path, token, payload=None, **kwargs):
        if not (path == "/installation/token" and kwargs == dict(method="DELETE")): guard()
        return transport(path, token, payload, **kwargs)
    def key(purpose): return {k: v for k, v in values[purpose].items() if k != "schemaVersion"}
    promo = check(key("promotion"), runtime.work, clock, transport=guarded, sign=sign)
    lifecycle = check_lifecycle(key("lifecycle"), runtime.work, clock, transport=guarded, sign=sign)
    if promo != expected_promotion or lifecycle != expected_lifecycle:
        raise ValueError("Private App keys do not match the admitted GitHub identities/policy")
    # Rotation or deletion while JWT/API checks were in flight must not publish
    # readiness for an old key. No control secret is ever created or changed.
    for purpose in ("promotion", "lifecycle"):
        guard()
        _, current = app_secret_snapshot(runtime.clients["secretsmanager"], purpose, kms_key=runtime.kms)
        if current != refs[purpose]: raise ValueError("Control App key changed during credential verification")
    proof = dict(schemaVersion=1, sessionId=runtime.execution["sessionId"], executionVersion=execution_version,
        sourceRevisions=runtime.execution["sourceRevisions"], promotion=dict(secret=refs["promotion"], authority=promo),
        lifecycle=dict(secret=refs["lifecycle"], identity=lifecycle))
    credential_proof(dict(automationCredentials=proof), runtime.execution, execution_version)
    guard()
    if prior is None:
        save_private(runtime, guard, lambda private: private.update(automationCredentials=proof))
    elif prior != proof: raise ValueError("Credential readiness cannot replace an existing session proof")
    guard()
