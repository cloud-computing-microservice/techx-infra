variable "cleanup_alert_email" {
  description = "Operator email for cleanup alarms. Confirm its SNS subscription before enabling paid sessions."
  type        = string
  default     = null
  sensitive   = true
  validation {
    condition     = var.cleanup_alert_email == null || can(regex("^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$", var.cleanup_alert_email))
    error_message = "Cleanup alert email must be a valid address or null while cleanup control is disabled."
  }
}

locals {
  cleanup_alert_metrics = {
    launcher-errors = {
      namespace  = "AWS/Lambda", metric = "Errors", statistic = "Sum", period = 60
      dimensions = { FunctionName = "techx-automation-cleanup-launcher" }
    }
    launcher-throttles = {
      namespace  = "AWS/Lambda", metric = "Throttles", statistic = "Sum", period = 60
      dimensions = { FunctionName = "techx-automation-cleanup-launcher" }
    }
    worker-failures = {
      namespace  = "AWS/CodeBuild", metric = "FailedBuilds", statistic = "Sum", period = 60
      dimensions = { ProjectName = local.cleanup_project_name }
    }
    delivery-dlq = {
      namespace  = "AWS/SQS", metric = "ApproximateNumberOfMessagesVisible", statistic = "Maximum", period = 60
      dimensions = { QueueName = "techx-automation-cleanup-dlq" }
    }
    deadline-overdue = {
      namespace  = "TechX/Automation", metric = "CleanupOverdue", statistic = "Maximum", period = 900
      dimensions = { Environment = "staging" }
    }
  }
  cleanup_alert_alarm_arns = [for name in keys(local.cleanup_alert_metrics) :
    "arn:aws:cloudwatch:us-east-1:058114477594:alarm:techx-automation-cleanup-${name}"
  ]
}

resource "aws_sns_topic" "cleanup_alerts" {
  count             = var.enable_cleanup_control ? 1 : 0
  name              = "techx-automation-cleanup-alerts"
  kms_master_key_id = aws_kms_key.control.arn
}

data "aws_iam_policy_document" "cleanup_alerts" {
  count = var.enable_cleanup_control ? 1 : 0
  statement {
    sid       = "TopicOwner"
    actions   = ["sns:*"]
    resources = [aws_sns_topic.cleanup_alerts[0].arn]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::058114477594:root"]
    }
  }
  statement {
    sid       = "OnlyCleanupAlarmsPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.cleanup_alerts[0].arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = ["058114477594"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = local.cleanup_alert_alarm_arns
    }
  }
  statement {
    sid       = "RequireTLS"
    effect    = "Deny"
    actions   = ["sns:*"]
    resources = [aws_sns_topic.cleanup_alerts[0].arn]
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

resource "aws_sns_topic_policy" "cleanup_alerts" {
  count  = var.enable_cleanup_control ? 1 : 0
  arn    = aws_sns_topic.cleanup_alerts[0].arn
  policy = data.aws_iam_policy_document.cleanup_alerts[0].json
}

resource "aws_sns_topic_subscription" "cleanup_alerts" {
  count     = var.enable_cleanup_control ? 1 : 0
  topic_arn = aws_sns_topic.cleanup_alerts[0].arn
  protocol  = "email"
  endpoint  = var.cleanup_alert_email
}

resource "aws_cloudwatch_metric_alarm" "cleanup" {
  for_each = var.enable_cleanup_control ? local.cleanup_alert_metrics : {}

  alarm_name          = "techx-automation-cleanup-${each.key}"
  alarm_description   = "TechX staging cleanup requires operator attention; retain evidence and check inventory."
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = each.value.statistic
  period              = each.value.period
  dimensions          = each.value.dimensions
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  treat_missing_data  = "notBreaching"
  actions_enabled     = true
  alarm_actions       = [aws_sns_topic.cleanup_alerts[0].arn]
  depends_on          = [aws_sns_topic_policy.cleanup_alerts]
}

output "cleanup_alert_topic_arn" {
  value = var.enable_cleanup_control ? aws_sns_topic.cleanup_alerts[0].arn : null
}

output "cleanup_alert_alarm_names" {
  value = [for alarm in aws_cloudwatch_metric_alarm.cleanup : alarm.alarm_name]
}
