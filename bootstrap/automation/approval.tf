variable "enable_plan_approval" {
  description = "Enable the protected-environment plan reader/approver only after GitHub review settings and lifecycle integration have been verified."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_plan_approval || var.enable_cleanup_control
    error_message = "Plan approval requires the independent cleanup control plane."
  }
}

locals {
  plan_review_roles = var.enable_plan_approval ? {
    reader   = { name = "techx-automation-plan-reader", environment = "staging-plan-read" }
    approver = { name = "techx-automation-plan-approver", environment = "staging-apply" }
  } : {}
}

resource "aws_iam_role" "plan_review" {
  for_each = local.plan_review_roles
  name     = each.value.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-infra:environment:${each.value.environment}"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "plan_review" {
  for_each = local.plan_review_roles
  name     = "read-pending-plan${each.key == "approver" ? "-write-approval" : ""}"
  role     = aws_iam_role.plan_review[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [
      "${aws_s3_bucket.control.arn}/automation/staging/session.json",
      "${aws_s3_bucket.control.arn}/private/staging/*/plans/*/*/request.json"
    ] },
    { Effect = "Allow", Action = ["scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } }
    ], [for statement in [
      { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.control.arn}/approvals/staging/*" },
      { Effect = "Allow", Action = ["kms:GenerateDataKey"], Resource = aws_kms_key.control.arn,
      Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } }
  ] : statement if each.key == "approver"]) })
}

output "plan_review_role_arns" {
  value = { for name, role in aws_iam_role.plan_review : name => role.arn }
}
