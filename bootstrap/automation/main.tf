terraform {
  required_version = "= 1.15.5"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "= 6.55.0" }
    archive = { source = "hashicorp/archive", version = "= 2.7.1" }
  }
}

provider "aws" {
  region              = "us-east-1"
  allowed_account_ids = ["058114477594"]
  default_tags {
    tags = { Project = "techx", Environment = "automation", ManagedBy = "terraform" }
  }
}

variable "bucket_name" {
  description = "Unique private bucket for staging state, session and private artifacts. Not a workload resource."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "A valid unique S3 bucket name is required."
  }
}

variable "create_github_oidc_provider" {
  description = "Enable only after confirming this account does not already have the GitHub OIDC provider."
  type        = bool
  default     = false
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:aws:iam::058114477594:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_s3_bucket" "control" {
  bucket        = var.bucket_name
  force_destroy = false
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_ownership_controls" "control" {
  bucket = aws_s3_bucket.control.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "control" {
  bucket                  = aws_s3_bucket.control.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "control" {
  bucket = aws_s3_bucket.control.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "control" {
  bucket = aws_s3_bucket.control.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.control.arn
    }
  }
}

resource "aws_kms_key" "control" {
  description             = "TechX automation state, session and private artifact encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.control_key.json
  lifecycle { prevent_destroy = true }
}

data "aws_iam_policy_document" "control_key" {
  statement {
    sid       = "AccountDelegation"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::058114477594:root"]
    }
  }
  statement {
    sid       = "AutomationLogs"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.us-east-1.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:us-east-1:058114477594:log-group:/aws/lambda/techx-automation-*", "arn:aws:logs:us-east-1:058114477594:log-group:/aws/codebuild/techx-automation-*"]
    }
  }
}

resource "aws_s3_object" "initial_pointer" {
  bucket                 = aws_s3_bucket.control.id
  key                    = "control/staging/current-execution.json"
  content                = jsonencode({ status = "OFF" })
  content_type           = "application/json"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.control.arn
  lifecycle { ignore_changes = [content] }
}

resource "aws_s3_bucket_lifecycle_configuration" "control" {
  bucket = aws_s3_bucket.control.id
  rule {
    id     = "private-plan-retention"
    status = "Enabled"
    filter { prefix = "plans/" }
    expiration { days = 1 }
    noncurrent_version_expiration { noncurrent_days = 1 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
  rule {
    id     = "evidence-retention"
    status = "Enabled"
    filter { prefix = "evidence/" }
    expiration { days = 30 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
  depends_on = [aws_s3_bucket_versioning.control]
}

data "aws_iam_policy_document" "control" {
  statement {
    sid       = "OnlyChartSelectorCanWriteSelections"
    effect    = "Deny"
    actions   = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.control.arn}/chart-selections/staging/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::058114477594:role/techx-automation-chart-selector"]
    }
  }
  statement {
    sid       = "OnlyPlanBridgeCanWriteApprovals"
    effect    = "Deny"
    actions   = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.control.arn}/approvals/staging/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::058114477594:role/techx-automation-plan-approver"]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.control.arn, "${aws_s3_bucket.control.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "control" {
  bucket = aws_s3_bucket.control.id
  policy = data.aws_iam_policy_document.control.json
}

data "aws_iam_policy_document" "publisher_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:cloud-computing-microservice/techx-platform:environment:staging-release"]
    }
  }
}

resource "aws_iam_role" "publisher" {
  name                 = "techx-automation-publisher"
  assume_role_policy   = data.aws_iam_policy_document.publisher_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "publisher" {
  statement {
    sid       = "EcrAuthentication"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PublishOnlyTechxArtifacts"
    actions = [
      "ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:DescribeImageScanFindings",
      "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
    ]
    resources = [for name in ["frontend", "catalog", "order"] : "arn:aws:ecr:us-east-1:058114477594:repository/techx/${name}"]
  }
  statement {
    sid       = "ReadAndLeaseSessionOnly"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.control.arn}/automation/staging/session.json"]
  }
  statement {
    sid       = "VerifyIndependentCleanup"
    actions   = ["scheduler:GetSchedule"]
    resources = ["arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*"]
  }
  statement {
    sid       = "DecryptSessionViaS3"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.control.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.us-east-1.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "publisher" {
  name   = "techx-publish-images-and-read-session"
  role   = aws_iam_role.publisher.id
  policy = data.aws_iam_policy_document.publisher.json
}

output "state_bucket" { value = aws_s3_bucket.control.id }
output "session_uri" { value = "s3://${aws_s3_bucket.control.id}/automation/staging/session.json" }
output "publisher_role_arn" { value = aws_iam_role.publisher.arn }
output "state_kms_key_arn" { value = aws_kms_key.control.arn }
