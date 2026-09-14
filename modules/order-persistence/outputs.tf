output "table_name" { value = aws_dynamodb_table.orders.name }
output "table_arn" { value = aws_dynamodb_table.orders.arn }
output "order_api_role_arn" { value = aws_iam_role.order_api.arn }
output "vpc_endpoint_id" { value = aws_vpc_endpoint.dynamodb.id }
