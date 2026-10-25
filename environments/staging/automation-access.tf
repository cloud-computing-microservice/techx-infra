variable "cleanup_executor_role_arn" {
  description = "Dedicated automation cleanup role. Access is scoped to this disposable cluster."
  type        = string
  default     = null
  validation {
    condition     = var.cleanup_executor_role_arn == null || var.cleanup_executor_role_arn == "arn:aws:iam::058114477594:role/techx-automation-cleanup-executor"
    error_message = "Only the dedicated TechX cleanup executor can be granted cleanup access."
  }
}

resource "aws_eks_access_entry" "cleanup" {
  count         = var.cleanup_executor_role_arn == null ? 0 : 1
  cluster_name  = module.eks.cluster_name
  principal_arn = var.cleanup_executor_role_arn
  type          = "STANDARD"
  tags          = local.tags
}

resource "aws_eks_access_policy_association" "cleanup" {
  count         = var.cleanup_executor_role_arn == null ? 0 : 1
  cluster_name  = module.eks.cluster_name
  principal_arn = aws_eks_access_entry.cleanup[0].principal_arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
