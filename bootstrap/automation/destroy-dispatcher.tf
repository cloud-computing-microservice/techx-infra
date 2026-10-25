# Independent of deploy/lifecycle activation: disabling deployment must not
# remove the operator's ability to request AWS-owned cleanup of an old session.
resource "aws_iam_role" "destroy_dispatcher" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "techx-automation-destroy-dispatcher"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-infra:environment:staging-destroy"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "destroy_dispatcher" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "request-fixed-cleanup-read-protected-evidence"
  role  = aws_iam_role.destroy_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["executions/*", "automation/staging/session.json", "cleanup/*/complete.json", "cleanup/*/inventory-*.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["cleanup/*/complete.json"] } } },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher" },
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds"], Resource = local.cleanup_project_arn }
  ] })
}

output "destroy_dispatcher_role_arn" { value = var.enable_cleanup_control ? aws_iam_role.destroy_dispatcher[0].arn : null }
