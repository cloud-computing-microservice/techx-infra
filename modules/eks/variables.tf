variable "cluster_name" { type = string }
variable "node_group_name" { type = string }
variable "kubernetes_version" { type = string }
variable "subnet_ids" { type = list(string) }
variable "public_access_cidrs" { type = list(string) }
variable "node_instance_type" { type = string }
variable "node_disk_size_gib" { type = number }
variable "addon_versions" { type = map(string) }
variable "tags" { type = map(string) }
variable "iam_permissions_boundary_arn" {
  description = "Optional pre-created workload role boundary; required by the automation executor IAM policy."
  type        = string
  default     = null
}
