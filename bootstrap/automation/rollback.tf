# The owned executor revalidates the original public failure before every Git
# mutation. These reads do not allow it to start managers or send SSM commands.
resource "aws_iam_role_policy" "rollback_predecessor_read" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "read-original-rollback-failure-and-terminal-predecessor"
  role  = aws_iam_role.deployment_executor[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds"], Resource = local.lifecycle_project_arn },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = "${aws_s3_bucket.control.arn}/handoffs/staging/*" }
  ] })
}

resource "aws_iam_role_policy" "rollback_proposal_read" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "observe-versioned-private-rollback-proposal"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [for name in ["intent", "proposal"] :
      "${aws_s3_bucket.control.arn}/private/staging/*/rollback/${name}.json"
    ]
  }] })
}
