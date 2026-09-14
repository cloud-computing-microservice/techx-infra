from __future__ import annotations
import pathlib
import re

root = pathlib.Path(__file__).parents[1]
tf_paths: list[pathlib.Path] = []
for directory in (root / "environments" / "demo", root / "environments" / "staging", root / "modules"):
    tf_paths.extend(directory.rglob("*.tf"))
text = "\n".join(path.read_text(encoding="utf-8") for path in tf_paths)
required = [
    'default = "1.35"', 'ami_type        = "AL2023_x86_64_STANDARD"',
    'http_tokens                 = "required"', 'http_put_response_hop_limit = 1',
    'volume_type           = "gp3"', 'encrypted             = true',
    'image_tag_mutability = "IMMUTABLE"', 'force_delete         = true',
    'retention_in_days = 7', 'enabled_cluster_log_types = ["api", "audit", "authenticator"]',
    'chart_version     = "3.3.0"', 'chart_version           = "9.5.17"',
    'for_each = toset([40, 60, 72])', 'limit_amount = "80"',
    'resource "aws_cloudfront_vpc_origin"',
    'resource "aws_ec2_client_vpn_endpoint"',
    'resource "aws_route53_zone"',
    'Managed-AllViewerAndCloudFrontHeaders-2022-06',
    'uri === \'/argocd\'',
    'cidrhost(var.vpc_cidr, 2)',
    '"server.rootpath" = var.server_rootpath',
    '"alb.ingress.kubernetes.io/scheme"',
    '"alb.ingress.kubernetes.io/group.order"',
    '"10"',
    '"alb.ingress.kubernetes.io/security-groups"',
    '"alb.ingress.kubernetes.io/subnets"',
    'referenced_security_group_id = aws_security_group.this[0].id',
    'billing_mode = "PAY_PER_REQUEST"',
    'attribute_name = "ttlEpochSeconds"',
    'deletion_protection_enabled = false',
    '"dynamodb:TransactWriteItems"',
    'system:serviceaccount:${var.service_account_namespace}:${var.service_account_name}',
    'resource "aws_vpc_endpoint" "dynamodb"',
    'node_group_name = var.node_group_name',
]
for token in required:
    if token not in text:
        raise AssertionError(f"missing guardrail: {token}")
for forbidden in ("aws_nat_gateway", "aws_db_", "aws_wafv2_", "aws_route53_resolver_endpoint"):
    if re.search(rf'(?m)^resource\s+"{forbidden}', text):
        raise AssertionError(f"forbidden out-of-scope resource found: {forbidden}")
for environment in ("demo", "staging"):
    example = (root / "environments" / environment / "terraform.tfvars.example").read_text(encoding="utf-8")
    if "0.0.0.0/0" in example:
        raise AssertionError(f"{environment} EKS API example must not allow 0.0.0.0/0")
    if "enable_domain_vpn_edge                = false" not in example:
        raise AssertionError(f"{environment} edge/VPN resources must be opt-in in the example")

staging_variables = (root / "environments" / "staging" / "variables.tf").read_text(encoding="utf-8")
for token in ('default = "staging"', 'default = "10.52.0.0/16"', 'default = "172.24.0.0/22"'):
    if token not in staging_variables:
        raise AssertionError(f"missing staging environment contract: {token}")

scripts = {path.name: path.read_text(encoding="utf-8") for path in (root / "scripts").glob("*.ps1")}
for name in (
    "preflight.ps1", "create-reviewed-plan.ps1", "apply-reviewed-plan.ps1",
    "domain-vpn-inventory.ps1", "destroy-domain-vpn-environment.ps1",
    "suspend-environment.ps1", "resume-environment.ps1",
    "new-client-vpn-pki.ps1", "prepare-domain-certificates.ps1",
    "export-client-vpn-profile.ps1",
):
    if "[ValidateSet('demo', 'staging')][string]$Environment = 'staging'" not in scripts[name]:
        raise AssertionError(f"{name} must default to a validated staging environment")
for name in ("domain-vpn-inventory.ps1", "destroy-domain-vpn-environment.ps1", "suspend-environment.ps1"):
    script = scripts[name]
    if "Key=Project,Values=techx" not in script or "Environment,Values=$Environment" not in script:
        raise AssertionError(f"{name} must scope tagged AWS discovery by project and environment")
if 'Environment: $Environment' not in scripts["create-reviewed-plan.ps1"]:
    raise AssertionError("reviewed plan summary must bind approval to an environment")
if "summaryEnvironment -ne $Environment" not in scripts["apply-reviewed-plan.ps1"]:
    raise AssertionError("reviewed apply must reject cross-environment approval reuse")
print("Terraform scope and guardrail assertions passed.")
