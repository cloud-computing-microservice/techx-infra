variable "cluster_name" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "chart_version" { type = string }
variable "iam_policy_url" { type = string }
variable "iam_policy_sha256" { type = string }
variable "tags" { type = map(string) }
variable "cluster_ready_id" { type = string }
variable "iam_permissions_boundary_arn" {
  description = "Optional pre-created workload role boundary for automated staging."
  type        = string
  default     = null
}
