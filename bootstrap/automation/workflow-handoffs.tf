resource "aws_iam_role_policy" "workflow_handoffs" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "protect-single-consumption-workflow-dispatch"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.control.arn}/handoffs/staging/*", "${aws_s3_bucket.control.arn}/control/staging/current-execution.json", "${aws_s3_bucket.control.arn}/proposals/staging/*/bootstrap.json"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.control.arn}/handoffs/staging/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["handoffs/staging/*"] } } }
  ] })
}

locals {
  handoff_child_roles = merge(
    var.enable_plan_approval ? {
      reader   = "techx-automation-plan-reader"
      approver = "techx-automation-plan-approver"
    } : {},
    var.enable_lifecycle_control ? {
      bootstrap = "techx-automation-bootstrap-publisher"
      selector  = "techx-automation-chart-selector"
      preview   = "techx-automation-chart-reader"
    } : {}
  )
}

resource "aws_iam_role_policy" "handoff_children" {
  for_each = local.handoff_child_roles
  name     = "read-protected-workflow-dispatch-receipt"
  role     = each.value
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.control.arn}/handoffs/staging/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["handoffs/staging/*"] } } }
  ] })
  depends_on = [aws_iam_role.plan_review, aws_iam_role.bootstrap_publisher, aws_iam_role.chart_selector, aws_iam_role.chart_reader]
}
