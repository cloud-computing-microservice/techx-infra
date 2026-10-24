"""Recover exact secret receipts without relying on eventually consistent lists."""
import hashlib
import json
import re

from bootstrap_assets import ACCOUNT, tags


def reconcile_secrets(clients, bucket, session, execution_version, private):
    intents = private.get("bootstrapSecrets", {})
    if not intents: return [], []
    if not execution_version or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session) or set(intents) - {"vpn-pki", "order-api-key"}:
        raise ValueError("Secret recovery needs a scoped execution and known intents")
    key = f"cleanup/{session}/secret-proof.json"
    digest = hashlib.sha256(json.dumps(intents, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    s3, client = clients["s3"], clients["secretsmanager"]
    cached = {}
    try:
        response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
        payload = response["Body"].read(16 * 1024 + 1)
        if len(payload) > 16 * 1024: raise ValueError("Secret proof exceeds size policy")
        proof = json.loads(payload)
        if (proof.get("schemaVersion") != 1 or proof.get("sessionId") != session or proof.get("executionVersion") != execution_version
                or proof.get("inputSha256") != digest or not isinstance(proof.get("secrets"), dict) or set(proof["secrets"]) - set(intents)):
            raise ValueError("Secret proof differs from the stopped execution")
        cached = proof["secrets"]
    except s3.exceptions.NoSuchKey:
        pass
    known, unresolved, not_created = [], [], []
    for purpose, intent in intents.items():
        name = f"techx/staging/{session}/{purpose}"
        token = hashlib.sha256(f"{session}:{purpose}:v1".encode()).hexdigest()
        pattern = rf"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{re.escape(name)}-[A-Za-z0-9]{{6}}"
        if not isinstance(intent, dict) or intent.get("name") != name or intent.get("versionId") != token:
            raise ValueError("Invalid secret creation intent")
        references = {arn for arn in (intent.get("arn"), cached.get(purpose)) if arn}
        if len(references) > 1: raise ValueError("Secret intent/proof identities conflict")
        arn = next(iter(references), None)
        if arn is not None and (not isinstance(arn, str) or not re.fullmatch(pattern, arn)):
            raise ValueError("Secret recovery cannot cross session/account/region")
        try:
            metadata = client.describe_secret(SecretId=arn or name)
        except client.exceptions.ResourceNotFoundException:
            if not arn:
                if intent.get("phase") == "PREPARED":
                    not_created.append(purpose)  # Stopped worker never passed the pre-API checkpoint.
                else:
                    unresolved.append(purpose)  # No receipt: a read/list cannot resolve CALLING/legacy.
            continue
        observed = metadata.get("ARN", "")
        if (not re.fullmatch(pattern, observed) or (arn is not None and arn != observed) or metadata.get("Name") != name
                or any({row["Key"]: row["Value"] for row in metadata.get("Tags", [])}.get(key) != value for key, value in tags(session, purpose).items())
                or token not in metadata.get("VersionIdsToStages", {})):
            raise ValueError("Recovered secret is not the session's immutable version")
        cached[purpose] = observed
        known.append(metadata)
    s3.put_object(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT, ContentType="application/json",
        Body=json.dumps(dict(schemaVersion=1, sessionId=session, executionVersion=execution_version,
            inputSha256=digest, secrets=cached, notCreated=not_created)).encode())
    return known, unresolved
