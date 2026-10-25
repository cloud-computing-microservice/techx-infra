resource "aws_iam_role" "promotion_approver" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-promotion-approver"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-infra:environment:staging-chart-promote"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "promotion_approver" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "review-cold-candidate-write-only-private-promotion-approval"
  role  = aws_iam_role.promotion_approver[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["executions/*", "bundles/*", "automation/staging/session.json", "releases/staging/*/bootstrap.json", "proposals/staging/*/bootstrap.json", "promotion-approvals/staging/*/*.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.control.arn}/promotion-approvals/staging/*/*.json" },
    { Effect = "Allow", Action = ["scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } }
  ] })
}

output "promotion_approver_role_arn" { value = var.enable_lifecycle_control ? aws_iam_role.promotion_approver[0].arn : null }
