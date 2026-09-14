variable "region" {
  type    = string
  default = "us-east-1"
  validation {
    condition     = var.region == "us-east-1"
    error_message = "This staging environment is restricted to us-east-1."
  }
}

variable "project" {
  type    = string
  default = "techx"
}

variable "environment" {
  type    = string
  default = "staging"
  validation {
    condition     = var.environment == "staging"
    error_message = "This root module manages only the staging environment."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.52.0.0/16"
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
  validation {
    condition     = length(var.availability_zones) == 2 && alltrue([for az in var.availability_zones : startswith(az, "us-east-1")]) && length(distinct(var.availability_zones)) == 2
    error_message = "Choose exactly two distinct us-east-1 availability zones."
  }
}

variable "public_access_cidrs" {
  type = list(string)
  validation {
    condition = (
      !var.allow_temporary_public_access_cidr &&
      length(var.public_access_cidrs) == 1 &&
      can(cidrhost(var.public_access_cidrs[0], 0)) &&
      endswith(var.public_access_cidrs[0], "/32") &&
      var.public_access_cidrs[0] != "0.0.0.0/32"
      ) || (
      var.allow_temporary_public_access_cidr &&
      length(var.public_access_cidrs) >= 1 &&
      length(var.public_access_cidrs) <= 4 &&
      alltrue([for cidr in var.public_access_cidrs :
        can(cidrhost(cidr, 0)) &&
        (endswith(cidr, "/24") || endswith(cidr, "/32")) &&
        cidr != "0.0.0.0/24" && cidr != "0.0.0.0/32"
      ])
    )
    error_message = "Set one non-zero public IPv4 /32, or explicitly enable up to four temporary recovery CIDRs."
  }
}

variable "allow_temporary_public_access_cidr" {
  type        = bool
  default     = false
  description = "Allows up to four temporary /24 or /32 CIDRs only for the reviewed recovery stage."
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
  validation {
    condition     = var.kubernetes_version == "1.35"
    error_message = "The reviewed staging contract pins Kubernetes 1.35."
  }
}

variable "node_instance_type" {
  type    = string
  default = "t3.medium"
  validation {
    condition     = var.node_instance_type == "t3.medium"
    error_message = "The cost-reviewed staging environment permits one t3.medium only."
  }
}

variable "node_disk_size_gib" {
  type    = number
  default = 20
  validation {
    condition     = var.node_disk_size_gib == 20
    error_message = "The staging node root disk is fixed at 20 GiB."
  }
}

variable "addon_versions" {
  type = map(string)
  default = {
    vpc-cni    = "v1.22.4-eksbuild.3"
    kube-proxy = "v1.35.3-eksbuild.18"
    coredns    = "v1.13.2-eksbuild.11"
  }
  validation {
    condition     = toset(keys(var.addon_versions)) == toset(["vpc-cni", "kube-proxy", "coredns"]) && alltrue([for version in values(var.addon_versions) : startswith(version, "v")])
    error_message = "Pin exactly vpc-cni, kube-proxy, and coredns addon versions."
  }
}

variable "budget_alert_email" {
  type      = string
  default   = null
  nullable  = true
  sensitive = true
  validation {
    condition     = var.budget_alert_email == null || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be a valid email address."
  }
}

variable "enable_domain_vpn_foundation" {
  type        = bool
  description = "Create private subnets, internal-ALB security group and Argo CD private ingress."
  default     = false
}

variable "enable_domain_vpn_edge" {
  type        = bool
  description = "Create CloudFront, private DNS and AWS Client VPN after the internal ALB exists."
  default     = false
}

variable "domain_name" {
  type    = string
  default = "shop.dinhminhkhoa.id.vn"
  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\\.[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a valid lowercase DNS hostname."
  }
}

variable "public_certificate_arn" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition     = var.public_certificate_arn == "" || can(regex("^arn:aws:acm:us-east-1:[0-9]{12}:certificate/[0-9a-f-]+$", var.public_certificate_arn))
    error_message = "public_certificate_arn must be an ACM certificate ARN in us-east-1."
  }
}

variable "internal_alb_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.internal_alb_arn == "" || can(regex("^arn:aws:elasticloadbalancing:us-east-1:[0-9]{12}:loadbalancer/app/", var.internal_alb_arn))
    error_message = "internal_alb_arn must be an Application Load Balancer ARN in us-east-1."
  }
}

variable "client_vpn_cidr" {
  type    = string
  default = "172.24.0.0/22"
  validation {
    condition     = can(cidrhost(var.client_vpn_cidr, 0)) && !startswith(var.client_vpn_cidr, "10.52.")
    error_message = "client_vpn_cidr must be valid and must not overlap the TechX staging VPC."
  }
}

variable "client_vpn_server_certificate_arn" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition     = var.client_vpn_server_certificate_arn == "" || can(regex("^arn:aws:acm:us-east-1:[0-9]{12}:certificate/[0-9a-f-]+$", var.client_vpn_server_certificate_arn))
    error_message = "client_vpn_server_certificate_arn must be an ACM certificate ARN in us-east-1."
  }
}

variable "client_vpn_root_certificate_arn" {
  type      = string
  default   = ""
  sensitive = true
  validation {
    condition     = var.client_vpn_root_certificate_arn == "" || can(regex("^arn:aws:acm:us-east-1:[0-9]{12}:certificate/[0-9a-f-]+$", var.client_vpn_root_certificate_arn))
    error_message = "client_vpn_root_certificate_arn must be an ACM certificate ARN in us-east-1."
  }
}
