variable "enable_cleanup_control" {
  description = "Enable only once the cleanup CodeBuild project and its immutable-bundle entry point have been reviewed and configured."
  type        = bool
  default     = false
}

locals {
  cleanup_project_name = "techx-automation-cleanup"
  cleanup_project_arn  = "arn:aws:codebuild:us-east-1:058114477594:project/${local.cleanup_project_name}"
}

data "archive_file" "cleanup_launcher" {
  count            = var.enable_cleanup_control ? 1 : 0
  type             = "zip"
  source_file      = "${path.module}/cleanup_launcher.py"
  output_path      = "${path.module}/cleanup-launcher.zip"
  output_file_mode = "0644"
}

resource "aws_sqs_queue" "cleanup_dlq" {
  count                     = var.enable_cleanup_control ? 1 : 0
  name                      = "techx-automation-cleanup-dlq"
  sqs_managed_sse_enabled   = true
  message_retention_seconds = 1209600
}

resource "aws_iam_role" "cleanup_launcher" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "techx-automation-cleanup-launcher"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
}

resource "aws_cloudwatch_log_group" "cleanup_launcher" {
  count             = var.enable_cleanup_control ? 1 : 0
  name              = "/aws/lambda/techx-automation-cleanup-launcher"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.control.arn
}

resource "aws_iam_role_policy" "cleanup_launcher" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "launch-only-techx-cleanup"
  role  = aws_iam_role.cleanup_launcher[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [
      "${aws_s3_bucket.control.arn}/control/staging/current-execution.json",
      "${aws_s3_bucket.control.arn}/executions/*", "${aws_s3_bucket.control.arn}/cleanup/*/complete.json"
    ] },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn,
    Condition = { StringLike = { "s3:prefix" = ["cleanup/*"] } } },
    { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.control.arn,
    Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } },
    { Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:ListBuildsForProject"], Resource = local.cleanup_project_arn },
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds"], Resource = local.cleanup_project_arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.cleanup_launcher[0].arn}:*" },
    { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.cleanup_dlq[0].arn }
  ] })
}

resource "aws_lambda_function" "cleanup_launcher" {
  count                          = var.enable_cleanup_control ? 1 : 0
  function_name                  = "techx-automation-cleanup-launcher"
  role                           = aws_iam_role.cleanup_launcher[0].arn
  runtime                        = "python3.13"
  handler                        = "cleanup_launcher.handler"
  filename                       = data.archive_file.cleanup_launcher[0].output_path
  source_code_hash               = data.archive_file.cleanup_launcher[0].output_base64sha256
  timeout                        = 60
  memory_size                    = 128
  reserved_concurrent_executions = 1
  environment {
    variables = { CONTROL_BUCKET = aws_s3_bucket.control.id, CLEANUP_PROJECT = local.cleanup_project_name }
  }
  dead_letter_config { target_arn = aws_sqs_queue.cleanup_dlq[0].arn }
  depends_on = [aws_iam_role_policy.cleanup_launcher, aws_codebuild_project.cleanup]
}

resource "aws_scheduler_schedule_group" "staging" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "techx-staging"
}

resource "aws_iam_role" "scheduler" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "techx-automation-scheduler"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "scheduler.amazonaws.com" },
    Condition = {
      StringEquals = { "aws:SourceAccount" = "058114477594" },
      ArnEquals    = { "aws:SourceArn" = aws_scheduler_schedule_group.staging[0].arn }
    }
  }] })
}

resource "aws_iam_role_policy" "scheduler" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "invoke-only-techx-cleanup-launcher"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = aws_lambda_function.cleanup_launcher[0].arn },
    { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.cleanup_dlq[0].arn }
  ] })
}

resource "aws_cloudwatch_event_rule" "cleanup_watchdog" {
  count               = var.enable_cleanup_control ? 1 : 0
  name                = "techx-automation-cleanup-watchdog"
  description         = "Retry overdue staging cleanup independently of GitHub availability"
  schedule_expression = "rate(15 minutes)"
}

resource "aws_cloudwatch_event_target" "cleanup_watchdog" {
  count = var.enable_cleanup_control ? 1 : 0
  rule  = aws_cloudwatch_event_rule.cleanup_watchdog[0].name
  arn   = aws_lambda_function.cleanup_launcher[0].arn
  input = jsonencode({ watchdog = true })
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }
  dead_letter_config { arn = aws_sqs_queue.cleanup_dlq[0].arn }
}

resource "aws_lambda_permission" "cleanup_watchdog" {
  count          = var.enable_cleanup_control ? 1 : 0
  statement_id   = "AllowAutomationWatchdog"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.cleanup_launcher[0].function_name
  principal      = "events.amazonaws.com"
  source_arn     = aws_cloudwatch_event_rule.cleanup_watchdog[0].arn
  source_account = "058114477594"
}

resource "aws_sqs_queue_policy" "cleanup_dlq" {
  count     = var.enable_cleanup_control ? 1 : 0
  queue_url = aws_sqs_queue.cleanup_dlq[0].url
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sqs:SendMessage",
    Resource  = aws_sqs_queue.cleanup_dlq[0].arn,
    Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.cleanup_watchdog[0].arn } }
  }] })
}

output "cleanup_launcher_arn" { value = var.enable_cleanup_control ? aws_lambda_function.cleanup_launcher[0].arn : null }
output "cleanup_scheduler_role_arn" { value = var.enable_cleanup_control ? aws_iam_role.scheduler[0].arn : null }
output "cleanup_dlq_arn" { value = var.enable_cleanup_control ? aws_sqs_queue.cleanup_dlq[0].arn : null }
