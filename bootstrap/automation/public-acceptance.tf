resource "aws_iam_role" "public_acceptance" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-public-acceptance"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-infra:environment:staging-acceptance"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "public_acceptance" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "public-probes-and-private-public-evidence-only"
  role  = aws_iam_role.public_acceptance[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in [
      "automation/staging/session.json", "executions/*", "releases/staging/*/bootstrap.json", "chart-selections/staging/*/bootstrap.json",
      "acceptance/staging/*/public/*"
    ] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = "${aws_s3_bucket.control.arn}/private/staging/*/exposure/*.json" },
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [for prefix in [
      "releases/staging/*/active/*", "proposals/staging/*/active/*", "promotions/staging/*/receipt.json",
      "private/staging/*/active/*/rollout.json", "private/staging/*/active/*/application/intent.json"
    ] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.control.arn}/automation/staging/session.json", "${aws_s3_bucket.control.arn}/acceptance/staging/*/public/*"] },
    { Effect = "Allow", Action = ["scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } }
  ] })
}

resource "aws_iam_role_policy" "dispatcher_public_acceptance" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "read-pinned-public-acceptance-evidence"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = "${aws_s3_bucket.control.arn}/acceptance/staging/*/public/*" }
  ] })
}

output "public_acceptance_role_arn" { value = var.enable_lifecycle_control ? aws_iam_role.public_acceptance[0].arn : null }
