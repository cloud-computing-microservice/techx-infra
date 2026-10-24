resource "aws_iam_role" "chart_selector" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-chart-selector"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-infra:environment:staging-chart-select"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "chart_selector" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "verify-release-write-protected-chart-selection"
  role  = aws_iam_role.chart_selector[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["executions/*", "bundles/*", "automation/staging/session.json", "releases/staging/*/bootstrap.json", "chart-selections/staging/*/bootstrap.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["chart-selections/staging/*/bootstrap.json"] } } },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.control.arn}/automation/staging/session.json", "${aws_s3_bucket.control.arn}/chart-selections/staging/*/bootstrap.json"] },
    { Effect = "Allow", Action = ["scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } }
  ] })
}

output "chart_selector_role_arn" { value = var.enable_lifecycle_control ? aws_iam_role.chart_selector[0].arn : null }
