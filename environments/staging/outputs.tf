output "cluster_name" { value = module.eks.cluster_name }
output "region" { value = var.region }
output "node_group_name" { value = module.eks.node_group_name }
output "order_table_name" { value = module.order_persistence.table_name }
output "order_table_arn" { value = module.order_persistence.table_arn }
output "order_api_role_arn" { value = module.order_persistence.order_api_role_arn }
output "dynamodb_vpc_endpoint_id" { value = module.order_persistence.vpc_endpoint_id }
output "ecr_repository_urls" { value = module.ecr.repository_urls }
output "configure_kubectl_command" { value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}" }
output "public_frontend_url" { value = var.enable_domain_vpn_edge ? "https://${var.domain_name}/" : "Available after the selected exposure profile is deployed." }
output "private_argocd_url" { value = var.enable_domain_vpn_foundation ? "https://${var.domain_name}/argocd/" : null }
output "private_subnet_ids" { value = module.vpc.private_subnet_ids }
output "internal_alb_security_group_id" { value = var.enable_domain_vpn_foundation ? aws_security_group.internal_alb[0].id : null }
output "cloudfront_distribution_id" { value = module.cloudfront.distribution_id }
output "cloudfront_domain_name" { value = module.cloudfront.domain_name }
output "client_vpn_endpoint_id" { value = module.client_vpn.endpoint_id }
output "client_vpn_export_command" {
  value     = module.client_vpn.export_client_config_command
  sensitive = true
}
