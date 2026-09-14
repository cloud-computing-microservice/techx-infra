resource "aws_dynamodb_table" "orders" {
  name         = "${var.name}-orders"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "ttlEpochSeconds"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = false
  }

  deletion_protection_enabled = false
  tags                        = var.tags
}

locals {
  oidc_host = replace(var.oidc_provider_url, "https://", "")
  data_actions = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:TransactWriteItems",
  ]
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:${var.service_account_namespace}:${var.service_account_name}"]
    }
  }
}

resource "aws_iam_role" "order_api" {
  name               = "${var.name}-order-api"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "order_api" {
  statement {
    actions   = local.data_actions
    resources = [aws_dynamodb_table.orders.arn]
  }
}

resource "aws_iam_role_policy" "order_api" {
  name   = "${var.name}-orders"
  role   = aws_iam_role.order_api.id
  policy = data.aws_iam_policy_document.order_api.json
}

data "aws_iam_policy_document" "endpoint" {
  statement {
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions   = local.data_actions
    resources = [aws_dynamodb_table.orders.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:PrincipalArn"
      values   = [aws_iam_role.order_api.arn]
    }
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.route_table_ids
  policy            = data.aws_iam_policy_document.endpoint.json
  tags              = var.tags
}
