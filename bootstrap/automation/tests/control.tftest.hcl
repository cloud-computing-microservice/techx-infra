mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:058114477594:key/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::techx-test-control", id = "techx-test-control" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::058114477594:role/techx-automation-mock" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::058114477594:policy/techx-automation-mock" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = {
      arn = "arn:aws:sqs:us-east-1:058114477594:techx-automation-cleanup-dlq"
      url = "https://sqs.us-east-1.amazonaws.com/058114477594/techx-automation-cleanup-dlq"
    }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:058114477594:log-group:/aws/lambda/techx-automation-cleanup-launcher" }
  }
  mock_resource "aws_lambda_function" {
    defaults = { arn = "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:058114477594:rule/techx-automation-cleanup-watchdog" }
  }
  mock_resource "aws_scheduler_schedule_group" {
    defaults = { arn = "arn:aws:scheduler:us-east-1:058114477594:schedule-group/techx-staging" }
  }
}

variables {
  bucket_name           = "techx-test-control"
  cloudflare_secret_arn = "arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/cloudflare-TestOnly"
}

run "control_off_by_default" {
  command = plan
  assert {
    condition     = length(aws_lambda_function.cleanup_launcher) == 0 && length(aws_scheduler_schedule_group.staging) == 0
    error_message = "Cleanup execution must remain disabled until its executor has been configured."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.control.block_public_policy && aws_s3_bucket_public_access_block.control.restrict_public_buckets && aws_kms_key.control.enable_key_rotation
    error_message = "Control storage must stay private and encrypted with a rotating key."
  }
}

run "cleanup_control_is_project_scoped" {
  command = apply
  variables {
    enable_cleanup_control = true
  }
  assert {
    condition     = length(aws_lambda_function.cleanup_launcher) == 1 && aws_lambda_function.cleanup_launcher[0].reserved_concurrent_executions == 1
    error_message = "Independent launcher must serialize cleanup dispatch."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.cleanup_launcher[0].policy).Statement :
      statement.Resource == "arn:aws:codebuild:us-east-1:058114477594:project/techx-automation-cleanup"
      if contains(statement.Action, "codebuild:BatchGetBuilds") || contains(statement.Action, "codebuild:StartBuild")
    ])
    error_message = "CodeBuild IAM operations must use the fixed project ARN, never build ARN or wildcard."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.cleanup_launcher[0].policy).Statement :
      !contains(statement.Action, "s3:PutObject") && !contains(statement.Action, "iam:PassRole")
    ])
    error_message = "The launcher must not mutate workload/session state or pass arbitrary roles."
  }
  assert {
    condition     = aws_cloudwatch_event_rule.cleanup_watchdog[0].schedule_expression == "rate(15 minutes)" && aws_sqs_queue.cleanup_dlq[0].sqs_managed_sse_enabled
    error_message = "AWS watchdog and encrypted failure queue are required."
  }
  assert {
    condition = (
      aws_codebuild_project.cleanup[0].concurrent_build_limit == 1 &&
      !aws_codebuild_project.cleanup[0].environment[0].privileged_mode &&
      aws_codebuild_project.cleanup[0].source[0].type == "NO_SOURCE"
    )
    error_message = "Independent cleanup must use a serialized non-privileged executor and immutable S3 bundles."
  }
  assert {
    condition = alltrue([
      for policy in aws_iam_policy.cleanup_executor : length(policy.policy) <= 6144
    ])
    error_message = "Every managed executor policy must fit the AWS IAM size limit."
  }
  assert {
    condition = alltrue(flatten([
      for policy in aws_iam_policy.cleanup_executor : [
        for statement in jsondecode(policy.policy).Statement :
        !contains(statement.Action, "*") && !contains(statement.Action, "iam:PassRole") && !contains(statement.Action, "ecr:CreateRepository") && !contains(statement.Action, "ec2:CreateVpc")
      ]
    ]))
    error_message = "Cleanup cannot gain account-admin or workload creation privileges."
  }
}
