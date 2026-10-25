resource "aws_iam_role_policy" "private_promotion" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "owned-cold-promotion-private-grant-and-fixed-app-keys"
  role  = aws_iam_role.deployment_executor[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["promotion-approvals/staging/*", "promotions/staging/*", "releases/staging/*/bootstrap.json", "proposals/staging/*/bootstrap.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.control.arn}/promotions/staging/*" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["promotions/staging/*"] } } },
    { Effect = "Allow", Action = ["secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue"], Resource = [for purpose in ["promotion", "lifecycle"] : "arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/${purpose}-app-??????"],
    Condition = { StringEquals = { "aws:ResourceTag/Project" = "techx", "aws:ResourceTag/Environment" = "automation", "aws:ResourceTag/Role" = "github-app" } } },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.us-east-1.amazonaws.com" } } }
  ] })
}
