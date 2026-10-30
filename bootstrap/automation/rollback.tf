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
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [for name in ["intent", "proposal", "rollout", "application/intent"] :
      "${aws_s3_bucket.control.arn}/private/staging/*/rollback/${name}.json"
    ]
  }] })
}

# Protected preview/approval roles can inspect the exact original failure and
# successful private task. They cannot issue SSM commands, start builds, write
# rollback artifacts, access private inputs, or deploy workloads.
resource "aws_iam_role_policy" "rollback_review_evidence" {
  for_each = var.enable_lifecycle_control ? {
    preview = aws_iam_role.chart_reader[0].name
    approve = aws_iam_role.promotion_approver[0].name
  } : {}
  name = "verify-bounded-rollback-preparation-and-original-failure"
  role = each.value
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds"], Resource = local.lifecycle_project_arn },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [for prefix in [
      "control/staging/tasks/*.json", "private/staging/*/rollback/intent.json", "private/staging/*/rollback/proposal.json"
    ] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["acceptance/staging/*/active/*/public/*/failure.json"] } } }
  ] })
}
