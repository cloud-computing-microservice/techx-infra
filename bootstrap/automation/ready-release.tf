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
