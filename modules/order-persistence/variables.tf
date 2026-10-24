variable "name" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "route_table_ids" { type = list(string) }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "service_account_namespace" { type = string }
variable "service_account_name" { type = string }
variable "tags" { type = map(string) }
variable "iam_permissions_boundary_arn" {
  description = "Optional pre-created workload role boundary for automated staging."
  type        = string
  default     = null
}
