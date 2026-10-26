"""Session-owned certificates and immutable secrets for cold-start deployment.

Only the leased, verified EC2 worker calls this module. No secret values are
printed, returned as task output, or placed in Terraform variables/state.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from cloudflare_dns import CloudflareDNS, HOST, validate_name_target

ACCOUNT = "058114477594"
CERTIFICATE = rf"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/[a-f0-9-]{{36}}"
PKI_FIELDS = {"ca.crt", "server.crt", "server.key", "operator.crt", "operator.key"}


def tags(session, role):
    return dict(Project="techx", Environment="staging", SessionId=session, Role=role)


def tag_list(values):
    return [dict(Key=key, Value=value) for key, value in values.items()]


def generate_pki(runtime):
    # The existing cross-platform OpenSSL helper rejects output inside Git.
    # All local key material is removed by TemporaryDirectory, including CA key.
    with tempfile.TemporaryDirectory(prefix="techx-vpn-bootstrap-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        output = root / "pki"
        runtime.run(["pwsh", "-NoProfile", "-File", str(runtime.infra / "scripts/new-client-vpn-pki.ps1"),
            "-Environment", "staging", "-OutputDirectory", str(output), "-OperatorName", "techx-demo-operator"],
            env=runtime.env, check=True, capture_output=True)
        result = {name: (output / name).read_text(encoding="ascii") for name in PKI_FIELDS}
        validate_secret("vpn-pki", result)
        return result


def validate_secret(purpose, value):
    if purpose == "order-api-key":
        if set(value) != {"order-api-key"} or not re.fullmatch(r"[A-Za-z0-9_-]{64}", value["order-api-key"]):
            raise ValueError("Stored Order API key has an invalid schema")
    elif purpose == "vpn-pki":
        if set(value) != PKI_FIELDS:
            raise ValueError("Stored VPN PKI has an invalid schema")
        for name, pem in value.items():
            kind = "CERTIFICATE" if name.endswith(".crt") else "(?:RSA )?PRIVATE KEY"
            if not isinstance(pem, str) or len(pem) > 8192 or not re.fullmatch(
                    rf"-----BEGIN ({kind})-----\r?\n[A-Za-z0-9+/=\r\n]+-----END \1-----\s*", pem):
                raise ValueError("Stored VPN material is not bounded PEM")
    else:
        raise ValueError("Unsupported bootstrap secret")
    return value


def save_private(runtime, guard, change):
    guard()
    key = f"private/staging/{runtime.execution['sessionId']}/inputs.json"
    response = runtime.clients["s3"].get_object(Bucket=runtime.bucket, Key=key, ExpectedBucketOwner=ACCOUNT)
    body = response["Body"].read(256 * 1024 + 1)
    if len(body) > 256 * 1024 or not response.get("ETag") or json.loads(body) != runtime.private:
        raise ValueError("Private bootstrap snapshot changed; no mutation is authorized")
    updated = deepcopy(runtime.private)
    change(updated)
    guard()
    runtime.clients["s3"].put_object(Bucket=runtime.bucket, Key=key, ExpectedBucketOwner=ACCOUNT,
        IfMatch=response["ETag"], Body=json.dumps(updated, allow_nan=False).encode(), ContentType="application/json",
        ServerSideEncryption="aws:kms", SSEKMSKeyId=runtime.kms)
    runtime.private = updated


def read_cloudflare_client(runtime, guard):
    arn = os.environ.get("CLOUDFLARE_SECRET_ARN", "")
    if not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:techx/automation/cloudflare-[A-Za-z0-9]+", arn):
        raise ValueError("The protected SSM document must provide the scoped Cloudflare secret ARN")
    guard()
    response = runtime.clients["secretsmanager"].get_secret_value(SecretId=arn)
    credentials = json.loads(response["SecretString"])
    return CloudflareDNS(credentials["zoneId"], credentials["apiToken"], guard=guard)


class AssetBootstrap:
    def __init__(self, runtime, guard, *, sleep=time.sleep, pki_factory=None, dns_factory=None):
        self.runtime, self.guard = runtime, guard
        self.clients = runtime.clients
        if self.clients["acm"].meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("ACM imports require SDK automatic retries disabled")
        self.session = runtime.execution["sessionId"]
        self.key = f"private/staging/{self.session}/inputs.json"
        self.sleep = sleep
        self.pki_factory = pki_factory or (lambda: generate_pki(runtime))
        self.dns_factory = dns_factory or self.dns_client

    def save(self, change):
        save_private(self.runtime, self.guard, change)

    def dns_client(self):
        return read_cloudflare_client(self.runtime, self.guard)

    def secret(self, purpose):
        if purpose not in ("vpn-pki", "order-api-key"):
            raise ValueError("Unsupported bootstrap secret")
        client = self.clients["secretsmanager"]
        name = f"techx/staging/{self.session}/{purpose}"
        token = hashlib.sha256(f"{self.session}:{purpose}:v1".encode()).hexdigest()
        expected = tags(self.session, purpose)
        self.guard()
        try:
            metadata = client.describe_secret(SecretId=name)
        except client.exceptions.ResourceNotFoundException:
            # CreateSecret never updates an existing value. Even after an
            # ambiguous response the next task reads/reuses the same name and
            # version; it never calls PutSecretValue or rotates a key.
            previous = self.runtime.private.get("bootstrapSecrets", {}).get(purpose)
            if previous and previous.get("arn"):
                raise ValueError("An observed bootstrap secret disappeared; retry must not recreate or rotate it") from None
            # Preserve a previous CALLING/legacy uncertain intent. Never replace
            # it with PREPARED merely because a read is eventually empty.
            phase = "CALLING" if previous and previous.get("phase") != "PREPARED" else "PREPARED"
            self.save(lambda private: private.setdefault("bootstrapSecrets", {}).update({purpose: dict(name=name, versionId=token, phase=phase)}))
            value = self.pki_factory() if purpose == "vpn-pki" else {"order-api-key": secrets.token_urlsafe(48)}
            validate_secret(purpose, value)
            self.save(lambda private: private["bootstrapSecrets"][purpose].update(phase="CALLING"))
            self.guard()
            try:
                client.create_secret(Name=name, ClientRequestToken=token, KmsKeyId=self.runtime.kms,
                    Tags=tag_list(expected), SecretString=json.dumps(value))
            except client.exceptions.ResourceExistsException:
                pass  # Read and validate the winner; never overwrite it.
            metadata = client.describe_secret(SecretId=name)
        arn = metadata.get("ARN", "")
        if (not re.fullmatch(rf"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:{re.escape(name)}-[A-Za-z0-9]{{6}}", arn)
                or metadata.get("Name") != name or metadata.get("KmsKeyId") != self.runtime.kms or metadata.get("DeletedDate")
                or {row["Key"]: row["Value"] for row in metadata.get("Tags", [])} != expected
                or metadata.get("VersionIdsToStages", {}).get(token) != ["AWSCURRENT"]):
            raise ValueError("Bootstrap secret ownership, encryption or immutable version changed")
        self.guard()
        stored = client.get_secret_value(SecretId=arn, VersionId=token, VersionStage="AWSCURRENT")
        if stored.get("ARN") != arn or stored.get("VersionId") != token or len(stored.get("SecretString", "")) > 48 * 1024:
            raise ValueError("Bootstrap secret version response is invalid")
        value = validate_secret(purpose, json.loads(stored["SecretString"]))
        self.save(lambda private: private.setdefault("bootstrapSecrets", {}).update({purpose: dict(name=name, arn=arn, versionId=token, phase="OBSERVED")}))
        return value

    def discover(self, role):
        result = self.clients["resourcegroupstaggingapi"].get_resources(ResourceTypeFilters=["acm:certificate"],
            TagFilters=[dict(Key=key, Values=[value]) for key, value in tags(self.session, role).items()])
        if result.get("PaginationToken") or len(result.get("ResourceTagMappingList", [])) > 1:
            raise ValueError("Ambiguous certificate recovery inventory")
        rows = result.get("ResourceTagMappingList", [])
        return rows[0]["ResourceARN"] if rows else None

    def owned_certificate(self, arn, role):
        if not re.fullmatch(CERTIFICATE, arn):
            raise ValueError("Certificate is outside the deployment account/region")
        client = self.clients["acm"]
        details = client.describe_certificate(CertificateArn=arn)["Certificate"]
        actual = {row["Key"]: row["Value"] for row in client.list_tags_for_certificate(CertificateArn=arn)["Tags"]}
        if details.get("CertificateArn") != arn or any(actual.get(key) != value for key, value in tags(self.session, role).items()):
            raise ValueError("Certificate ownership changed")
        return details

    def certificate(self, role, material=None):
        fields = {"storefront-public": "publicCertificateArn", "client-vpn-server": "clientVpnServerCertificateArn",
                  "client-vpn-reference": "clientVpnRootCertificateArn"}
        if role not in fields or (role == "storefront-public") != (material is None):
            raise ValueError("Unsupported certificate bootstrap request")
        intents = self.runtime.private.get("certificateIntents", {})
        intent = intents.get(role)
        arn = self.runtime.private.get("certificateState", {}).get(fields[role])
        self.guard()
        if not arn:
            arn = self.discover(role)
            if not arn:
                if intent:
                    raise ValueError("Certificate API outcome is uncertain; no duplicate request/import permitted")
                self.save(lambda private: private.setdefault("certificateIntents", {}).update({role: dict(phase="CALLING")}))
                self.guard()
                if material is None:
                    result = self.clients["acm"].request_certificate(DomainName=HOST, ValidationMethod="DNS", KeyAlgorithm="RSA_2048",
                        Options=dict(Export="DISABLED"),
                        IdempotencyToken=hashlib.sha256(f"{self.session}:{role}".encode()).hexdigest()[:32], Tags=tag_list(tags(self.session, role)))
                else:
                    cert, key, chain = material
                    result = self.clients["acm"].import_certificate(Certificate=cert.encode(), PrivateKey=key.encode(),
                        CertificateChain=chain.encode(), Tags=tag_list(tags(self.session, role)))
                arn = result["CertificateArn"]
            if not re.fullmatch(CERTIFICATE, arn):
                raise ValueError("Certificate API returned an out-of-scope ARN")
            def record(private):
                private.setdefault("certificateState", {}).update(environment="staging", accountId=ACCOUNT, region="us-east-1", domainName=HOST)
                private["certificateState"][fields[role]] = arn
                private.setdefault("certificateIntents", {})[role] = dict(phase="OBSERVED", arn=arn)
            self.save(record)
        return arn

    def public(self, *, observations=61):
        if type(observations) is not int or not 1 <= observations <= 61:
            raise ValueError("Certificate wait must be bounded")
        role = "storefront-public"
        arn = self.certificate(role)
        dns = None
        for attempt in range(observations):
            self.guard()
            try:
                details = self.owned_certificate(arn, role)
            except self.clients["acm"].exceptions.ResourceNotFoundException:
                details = {}  # ACM documents propagation delay after request.
            if details and (details.get("DomainName") != HOST or details.get("Type") != "AMAZON_ISSUED"):
                raise ValueError("Unexpected public certificate type/domain")
            if details.get("Status") == "ISSUED":
                self.save(lambda private: private["terraformVars"].update(public_certificate_arn=arn))
                return arn
            if details.get("Status") not in (None, "PENDING_VALIDATION"):
                raise ValueError("Public certificate cannot become issued from this status")
            options = [row.get("ResourceRecord") for row in details.get("DomainValidationOptions", []) if row.get("DomainName") == HOST and row.get("ResourceRecord")]
            if len(options) > 1:
                raise ValueError("Ambiguous storefront validation record")
            if options:
                record = options[0]
                name, target = validate_name_target(record["Name"], record["Value"])
                if name == HOST or record.get("Type") != "CNAME":
                    raise ValueError("ACM must supply only a validation CNAME")
                dns = dns or self.dns_factory()
                rows = dns.records(name)
                if len(rows) > 1:
                    raise ValueError("Ambiguous validation DNS; no ownership intent may be created")
                # A pre-existing matching validation record may be reused
                # read-only. Never claim/delete another session's DNS record.
                borrowed = len(rows) == 1 and rows[0].get("comment") != f"techx:{self.session}"
                if borrowed:
                    if rows[0].get("content", "").rstrip(".").lower() != target or rows[0].get("proxied") is not False:
                        raise ValueError("Existing validation DNS conflicts with ACM")
                else:
                    plan = dict(name=name, target=target)
                    def intent(private):
                        plans = private["validationDnsPlans"]
                        if plan not in plans: plans.append(plan)
                    self.save(intent)
                    self.guard()
                    dns.ensure(name, target, self.session)
            if attempt + 1 < observations: self.sleep(10)
        raise TimeoutError("Public certificate issuance exceeded its bounded wait")

    def prepare(self):
        self.public()
        material = self.secret("vpn-pki")
        mapping = (("client-vpn-server", "server", "client_vpn_server_certificate_arn"),
                   ("client-vpn-reference", "operator", "client_vpn_root_certificate_arn"))
        for role, prefix, variable in mapping:
            certificate = material[f"{prefix}.crt"]
            arn = self.certificate(role, (certificate, material[f"{prefix}.key"], material["ca.crt"]))
            for attempt in range(21):
                self.guard()
                try:
                    details = self.owned_certificate(arn, role)
                    break
                except self.clients["acm"].exceptions.ResourceNotFoundException:
                    if attempt == 20: raise TimeoutError("Imported certificate has not propagated") from None
                    self.sleep(2)
            actual = self.clients["acm"].get_certificate(CertificateArn=arn)
            if (details.get("Type") != "IMPORTED" or details.get("Status") != "ISSUED"
                    or actual.get("Certificate", "").replace("\r\n", "\n").strip() != certificate.replace("\r\n", "\n").strip()
                    or actual.get("CertificateChain", "").replace("\r\n", "\n").strip() != material["ca.crt"].replace("\r\n", "\n").strip()):
                raise ValueError("Imported VPN certificate differs from the immutable PKI")
            self.save(lambda private: private["terraformVars"].update({variable: arn}))
