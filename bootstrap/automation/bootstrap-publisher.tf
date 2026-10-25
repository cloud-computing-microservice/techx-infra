# Separate from READY release publishing. This identity can push existing ECR
# repositories and publish a receipt, but cannot create/recreate infrastructure.
resource "aws_iam_role" "bootstrap_publisher" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-bootstrap-publisher"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-platform:environment:staging-bootstrap"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "bootstrap_publisher_images" {
  count  = var.enable_lifecycle_control ? 1 : 0
  name   = "publish-existing-ecr-and-lease-bounded-session"
  role   = aws_iam_role.bootstrap_publisher[0].name
  policy = data.aws_iam_policy_document.publisher.json
}

resource "aws_iam_role_policy" "bootstrap_publisher_control" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "read-admitted-bundle-write-cold-release-receipt"
  role  = aws_iam_role.bootstrap_publisher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["executions/*", "bundles/*", "releases/staging/*/bootstrap.json", "proposals/staging/*/bootstrap.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = [for prefix in ["releases/staging/*/bootstrap.json", "proposals/staging/*/bootstrap.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds"], Resource = local.lifecycle_project_arn },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher" }
  ] })
}

output "bootstrap_publisher_role_arn" { value = var.enable_lifecycle_control ? aws_iam_role.bootstrap_publisher[0].arn : null }
