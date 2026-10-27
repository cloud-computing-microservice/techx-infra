# The callback only reads immutable active artifacts. It cannot publish,
# overwrite a candidate, approve promotion, or mutate a running Application.
resource "aws_iam_role_policy" "ready_candidate_read" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "read-active-release-candidate"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [
      "${aws_s3_bucket.control.arn}/releases/staging/*/active/*",
      "${aws_s3_bucket.control.arn}/proposals/staging/*/active/*"
    ]
  }] })
}

resource "aws_iam_role_policy" "ready_promotion_read" {
  for_each = var.enable_lifecycle_control ? {
    preview = aws_iam_role.chart_reader[0].name
    approve = aws_iam_role.promotion_approver[0].name
  } : {}
  name = "read-pinned-active-promotion-evidence"
  role = each.value
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [for prefix in [
      "releases/staging/*/active/*", "proposals/staging/*/active/*",
      "private/staging/*/acceptance/*.json", "private/staging/*/exposure/*.json",
      "acceptance/staging/*/public/*"
    ] : "${aws_s3_bucket.control.arn}/${prefix}"]
  }] })
}

resource "aws_iam_role_policy" "private_active_candidate_read" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "read-pinned-active-candidate"
  role  = aws_iam_role.deployment_executor[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = [
      "${aws_s3_bucket.control.arn}/releases/staging/*/active/*", "${aws_s3_bucket.control.arn}/proposals/staging/*/active/*"
    ]
  }] })
}

resource "aws_iam_role_policy" "active_rollout_observe" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "read-pinned-private-rollout"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.control.arn}/private/staging/*/active/*", "${aws_s3_bucket.control.arn}/promotions/staging/*"]
  }] })
}
