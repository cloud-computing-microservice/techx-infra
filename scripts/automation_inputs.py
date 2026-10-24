"""Validate private Terraform inputs before a cloud executor consumes them."""
import ipaddress
import re

from automation_budget import approved_budget

ROLE = "arn:aws:iam::058114477594:role/techx-automation-cleanup-executor"
FIXED = dict(region="us-east-1", project="techx", environment="staging", vpc_cidr="10.52.0.0/16",
             client_vpn_cidr="172.24.0.0/22", domain_name="shop.dinhminhkhoa.id.vn", cleanup_executor_role_arn=ROLE,
             kubernetes_version="1.35", node_instance_type="t3.medium", node_disk_size_gib=20)
ALLOWED = set(FIXED) | {"availability_zones", "public_access_cidrs", "allow_temporary_public_access_cidr", "budget_alert_email",
    "enable_domain_vpn_foundation", "enable_domain_vpn_edge", "internal_alb_arn", "public_certificate_arn",
    "client_vpn_server_certificate_arn", "client_vpn_root_certificate_arn", "budget_limit_usd"}


def validate_variables(variables):
    if set(variables) - ALLOWED: raise ValueError("Unexpected private Terraform variable")
    # Older cleanup snapshots omitted the budget. Preserve deletion recovery;
    # new deployment sessions require an explicit budget in the coordinator.
    approved_budget(variables.get("budget_limit_usd", 80))
    for name, value in FIXED.items():
        if name in variables and variables[name] != value: raise ValueError(f"Terraform deployment contract changed: {name}")
    if variables.get("cleanup_executor_role_arn") != ROLE: raise ValueError("Cleanup identity must be preserved")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", variables.get("budget_alert_email", "")):
        raise ValueError("A valid budget alert recipient is required")
    cidrs = variables.get("public_access_cidrs", [])
    if len(cidrs) != 1 or ipaddress.ip_network(cidrs[0]).version != 4 or ipaddress.ip_network(cidrs[0]).prefixlen != 32 or not ipaddress.ip_network(cidrs[0]).network_address.is_global:
        raise ValueError("Automation requires one exact IPv4 /32")
    if variables.get("allow_temporary_public_access_cidr", False) is not False:
        raise ValueError("Automation cannot enable temporary broad public access")
    for name in ("enable_domain_vpn_foundation", "enable_domain_vpn_edge"):
        if name in variables and type(variables[name]) is not bool: raise ValueError("Domain/VPN flags must be booleans")
    zones = variables.get("availability_zones", ["us-east-1a", "us-east-1b"])
    if len(zones) != 2 or len(set(zones)) != 2 or any(not re.fullmatch(r"us-east-1[a-z]", zone) for zone in zones):
        raise ValueError("Two distinct regional AZs are required")
    for name in ("public_certificate_arn", "client_vpn_server_certificate_arn", "client_vpn_root_certificate_arn"):
        if variables.get(name) and not re.fullmatch(r"arn:aws:acm:us-east-1:058114477594:certificate/[a-f0-9-]{36}", variables[name]):
            raise ValueError("Certificate ARN must stay in the deployment account and region")
    if variables.get("internal_alb_arn") and not re.fullmatch(r"arn:aws:elasticloadbalancing:us-east-1:058114477594:loadbalancer/app/[A-Za-z0-9-]+/[a-f0-9]+", variables["internal_alb_arn"]):
        raise ValueError("Unexpected internal ALB ARN")
    return variables
