"""Recover exact session certificate receipts before deletion; never infer absence."""
import hashlib
import json
import re

from bootstrap_assets import ACCOUNT, CERTIFICATE, tags
from cloudflare_dns import HOST

FIELDS = {"storefront-public": "publicCertificateArn", "client-vpn-server": "clientVpnServerCertificateArn",
          "client-vpn-reference": "clientVpnRootCertificateArn"}


def reconcile_certificates(clients, bucket, session, execution_version, private):
    intents = private.get("certificateIntents", {})
    state = dict(private.get("certificateState", {}))
    if not intents:
        return state, []  # Legacy/manual snapshots retain their exact ARNs.
    if not execution_version or not re.fullmatch(r"staging-[A-Za-z0-9-]{1,64}", session) or set(intents) - set(FIELDS):
        raise ValueError("Certificate recovery needs a scoped execution and known intents")
    digest = hashlib.sha256(json.dumps(intents, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    key = f"cleanup/{session}/certificate-proof.json"
    s3 = clients["s3"]
    resolved = {}
    try:
        response = s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
        payload = response["Body"].read(16 * 1024 + 1)
        if len(payload) > 16 * 1024: raise ValueError("Certificate proof exceeds size policy")
        proof = json.loads(payload)
        if (proof.get("schemaVersion") != 1 or proof.get("sessionId") != session
                or proof.get("executionVersion") != execution_version or proof.get("inputSha256") != digest
                or not isinstance(proof.get("certificates"), dict) or set(proof["certificates"]) - set(intents)
                or any(not isinstance(arn, str) or not re.fullmatch(CERTIFICATE, arn) for arn in proof["certificates"].values())):
            raise ValueError("Certificate proof differs from the stopped execution")
        resolved.update(proof["certificates"])
    except s3.exceptions.NoSuchKey:
        pass
    unresolved = []
    for role, intent in intents.items():
        if not isinstance(intent, dict) or intent.get("phase") not in ("CALLING", "OBSERVED"):
            raise ValueError("Unknown certificate mutation intent")
        references = {arn for arn in (intent.get("arn"), state.get(FIELDS[role]), resolved.get(role)) if arn}
        if len(references) > 1:
            raise ValueError("Certificate intent/state/proof identities conflict")
        arn = next(iter(references), None)
        if not arn:
            result = clients["resourcegroupstaggingapi"].get_resources(ResourceTypeFilters=["acm:certificate"],
                TagFilters=[dict(Key=name, Values=[value]) for name, value in tags(session, role).items()])
            rows = result.get("ResourceTagMappingList", [])
            if result.get("PaginationToken") or len(rows) > 1:
                raise ValueError("Certificate recovery inventory is ambiguous")
            if not rows:
                unresolved.append(role)
                continue
            arn = rows[0]["ResourceARN"]
        if not isinstance(arn, str) or not re.fullmatch(CERTIFICATE, arn):
            raise ValueError("Certificate recovery cannot cross account/region")
        client = clients["acm"]
        try:
            details = client.describe_certificate(CertificateArn=arn)["Certificate"]
        except client.exceptions.ResourceNotFoundException:
            # An exact prior receipt proves what was created; a confirmed
            # NotFound is then valid deletion recovery, unlike an empty list.
            if not references:
                unresolved.append(role)
                continue
        else:
            actual = {row["Key"]: row["Value"] for row in client.list_tags_for_certificate(CertificateArn=arn)["Tags"]}
            if (details.get("CertificateArn") != arn
                    or any(actual.get(name) != value for name, value in tags(session, role).items())):
                raise ValueError("Recovered certificate is not owned by this session")
        state[FIELDS[role]] = arn
        resolved[role] = arn
    state.update(environment="staging", accountId=ACCOUNT, region="us-east-1", domainName=HOST)
    # Cleanup owns this prefix. Persist exact receipts before removing any
    # certificate so retries remain safe after ACM/tag metadata disappears.
    proof = dict(schemaVersion=1, sessionId=session, executionVersion=execution_version,
        inputSha256=digest, certificates=resolved)
    s3.put_object(Bucket=bucket, Key=key, ExpectedBucketOwner=ACCOUNT,
        Body=json.dumps(proof).encode(), ContentType="application/json")
    return state, unresolved
