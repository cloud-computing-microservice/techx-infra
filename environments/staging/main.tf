locals {
  name = "${var.project}-${var.environment}"
  tags = { Project = var.project, Environment = var.environment, ManagedBy = "terraform" }
}

module "vpc" {
  source                 = "../../modules/vpc"
  name                   = local.name
  vpc_cidr               = var.vpc_cidr
  availability_zones     = var.availability_zones
  create_private_subnets = var.enable_domain_vpn_foundation
  tags                   = local.tags
}

data "aws_ec2_managed_prefix_list" "cloudfront_origin" {
  count = var.enable_domain_vpn_foundation ? 1 : 0
  name  = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "internal_alb" {
  count = var.enable_domain_vpn_foundation ? 1 : 0

  name_prefix = "${local.name}-internal-alb-"
  description = "Internal ALB used by CloudFront and AWS Client VPN"
  vpc_id      = module.vpc.vpc_id
  tags        = merge(local.tags, { Name = "${local.name}-internal-alb" })

  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "cloudfront_http" {
  count = var.enable_domain_vpn_foundation ? 1 : 0

  security_group_id = aws_security_group.internal_alb[0].id
  description       = "CloudFront origin-facing network to ALB HTTP"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront_origin[0].id
}

resource "aws_vpc_security_group_egress_rule" "internal_alb_to_vpc" {
  count = var.enable_domain_vpn_foundation ? 1 : 0

  security_group_id = aws_security_group.internal_alb[0].id
  description       = "Internal ALB to Kubernetes targets"
  ip_protocol       = "-1"
  cidr_ipv4         = var.vpc_cidr
}

module "ecr" {
  source = "../../modules/ecr"
  names  = ["techx/frontend", "techx/catalog", "techx/order"]
  tags   = local.tags
}

module "eks" {
  source              = "../../modules/eks"
  cluster_name        = local.name
  node_group_name     = var.environment
  kubernetes_version  = var.kubernetes_version
  subnet_ids          = module.vpc.public_subnet_ids
  public_access_cidrs = var.public_access_cidrs
  node_instance_type  = var.node_instance_type
  node_disk_size_gib  = var.node_disk_size_gib
  addon_versions      = var.addon_versions
  tags                = local.tags
}

module "order_persistence" {
  source                    = "../../modules/order-persistence"
  name                      = local.name
  region                    = var.region
  vpc_id                    = module.vpc.vpc_id
  route_table_ids           = module.vpc.public_route_table_ids
  oidc_provider_arn         = module.eks.oidc_provider_arn
  oidc_provider_url         = module.eks.oidc_provider_url
  service_account_namespace = local.name
  service_account_name      = "order-api"
  tags                      = local.tags
}

module "aws_load_balancer_controller" {
  source            = "../../modules/aws-load-balancer-controller"
  cluster_name      = module.eks.cluster_name
  region            = var.region
  vpc_id            = module.vpc.vpc_id
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  chart_version     = "3.3.0"
  iam_policy_url    = "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v3.3.0/docs/install/iam_policy.json"
  iam_policy_sha256 = "16f232c9d9f79366fe949c4550ad517a202380058a9e48d45a4e215044a20a6a"
  cluster_ready_id  = module.eks.coredns_addon_id
  tags              = local.tags
}

module "argocd" {
  source                  = "../../modules/argocd"
  chart_version           = "9.5.17"
  environment             = var.environment
  private_ingress_enabled = var.enable_domain_vpn_foundation
  server_rootpath         = "/argocd"
  server_url              = "https://${var.domain_name}/argocd"
  hostname                = var.domain_name
  certificate_arn         = var.public_certificate_arn
  alb_security_group_id   = var.enable_domain_vpn_foundation ? aws_security_group.internal_alb[0].id : ""
  private_subnet_ids      = module.vpc.private_subnet_ids
  ingress_group_name      = "techx-private"
  depends_on              = [module.aws_load_balancer_controller]
}

data "aws_lb" "internal" {
  count = var.enable_domain_vpn_edge ? 1 : 0
  arn   = var.internal_alb_arn
}

module "cloudfront" {
  source = "../../modules/cloudfront"

  enabled            = var.enable_domain_vpn_edge
  name               = local.name
  domain_name        = var.domain_name
  certificate_arn    = var.public_certificate_arn
  origin_alb_arn     = var.internal_alb_arn
  origin_domain_name = var.enable_domain_vpn_edge ? data.aws_lb.internal[0].dns_name : ""
  tags               = local.tags
}

module "private_dns" {
  source = "../../modules/private-dns"

  enabled   = var.enable_domain_vpn_edge
  zone_name = var.domain_name
  vpc_id    = module.vpc.vpc_id
  alb_arn   = var.internal_alb_arn
  tags      = local.tags
}

module "client_vpn" {
  source = "../../modules/client-vpn"

  enabled                     = var.enable_domain_vpn_edge
  name                        = "${local.name}-operator"
  region                      = var.region
  vpc_id                      = module.vpc.vpc_id
  vpc_cidr                    = module.vpc.vpc_cidr
  subnet_id                   = var.enable_domain_vpn_edge ? module.vpc.private_subnet_ids[0] : ""
  client_cidr                 = var.client_vpn_cidr
  server_certificate_arn      = var.client_vpn_server_certificate_arn
  client_root_certificate_arn = var.client_vpn_root_certificate_arn
  alb_security_group_id       = var.enable_domain_vpn_edge ? aws_security_group.internal_alb[0].id : ""
  eks_security_group_id       = module.eks.cluster_security_group_id
  tags                        = local.tags
}

check "edge_requires_foundation" {
  assert {
    condition     = !var.enable_domain_vpn_edge || var.enable_domain_vpn_foundation
    error_message = "enable_domain_vpn_edge requires enable_domain_vpn_foundation=true."
  }
}

resource "aws_budgets_budget" "project" {
  count        = var.budget_alert_email == null ? 0 : 1
  name         = "${local.name}-hard-cap"
  budget_type  = "COST"
  limit_amount = "80"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = [format("user:Project$%s", var.project)]
  }

  dynamic "notification" {
    for_each = toset([40, 60, 72])
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
  tags = local.tags
}
