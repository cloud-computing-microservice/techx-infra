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
  mock_data "aws_ami" {
    defaults = {
      id               = "ami-1234567890abcdef0", architecture = "x86_64", root_device_type = "ebs", root_device_name = "/dev/sda1",
      platform_details = "Linux/UNIX", name = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server"
    }
  }
  mock_resource "aws_vpc" { defaults = { id = "vpc-12345678", arn = "arn:aws:ec2:us-east-1:058114477594:vpc/vpc-12345678" } }
  mock_resource "aws_subnet" { defaults = { id = "subnet-12345678", arn = "arn:aws:ec2:us-east-1:058114477594:subnet/subnet-12345678" } }
  mock_resource "aws_security_group" { defaults = { id = "sg-12345678", arn = "arn:aws:ec2:us-east-1:058114477594:security-group/sg-12345678" } }
  mock_resource "aws_launch_template" { defaults = { id = "lt-12345678", arn = "arn:aws:ec2:us-east-1:058114477594:launch-template/lt-12345678", latest_version = 1 } }
  mock_resource "aws_ssm_document" { defaults = { latest_version = "1", hash_type = "Sha256", hash = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", arn = "arn:aws:ssm:us-east-1:058114477594:document/techx-automation-deployment-task" } }
  mock_resource "aws_iam_instance_profile" { defaults = { arn = "arn:aws:iam::058114477594:instance-profile/techx-automation-lifecycle-executor" } }
  mock_resource "aws_imagebuilder_component" { defaults = { arn = "arn:aws:imagebuilder:us-east-1:058114477594:component/techx-automation-deployment-executor/1.0.0/1" } }
  mock_resource "aws_imagebuilder_image_recipe" { defaults = { arn = "arn:aws:imagebuilder:us-east-1:058114477594:image-recipe/techx-automation-deployment-executor/1.0.0" } }
  mock_resource "aws_imagebuilder_infrastructure_configuration" { defaults = { arn = "arn:aws:imagebuilder:us-east-1:058114477594:infrastructure-configuration/techx-automation-deployment-executor" } }
  mock_resource "aws_imagebuilder_distribution_configuration" { defaults = { arn = "arn:aws:imagebuilder:us-east-1:058114477594:distribution-configuration/techx-automation-deployment-executor" } }
  mock_resource "aws_imagebuilder_image_pipeline" { defaults = { arn = "arn:aws:imagebuilder:us-east-1:058114477594:image-pipeline/techx-automation-deployment-executor" } }
  mock_resource "aws_s3_object" { defaults = { arn = "arn:aws:s3:::techx-test-control/bootstrap/executor/fixture.zip" } }
}

variables {
  bucket_name           = "techx-test-control"
  cloudflare_secret_arn = "arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/cloudflare-TestOnly"
}

run "control_off_by_default" {
  command = plan
  assert {
    condition     = length(aws_codebuild_project.lifecycle) == 0 && length(aws_imagebuilder_image_pipeline.executor) == 0 && length(aws_vpc.executor_control) == 0 && length(aws_iam_role.chart_selector) == 0 && length(aws_iam_role.chart_reader) == 0 && length(aws_iam_role.destroy_dispatcher) == 0
    error_message = "Lifecycle compute/network/image resources must remain disabled by default."
  }
  assert {
    condition     = length(aws_lambda_function.cleanup_launcher) == 0 && length(aws_scheduler_schedule_group.staging) == 0
    error_message = "Cleanup execution must remain disabled until its executor has been configured."
  }
  assert {
    condition     = length(aws_iam_role.plan_review) == 0
    error_message = "Approval roles must remain disabled by default."
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
    enable_plan_approval   = true
  }
  assert {
    condition = (length(aws_iam_role.destroy_dispatcher) == 1 &&
      jsondecode(aws_iam_role.destroy_dispatcher[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:cloud-computing-microservice/techx-infra:environment:staging-destroy" &&
      alltrue(flatten([for statement in jsondecode(aws_iam_role_policy.destroy_dispatcher[0].policy).Statement : [for action in statement.Action :
        contains(["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket", "kms:Decrypt", "lambda:InvokeFunction", "codebuild:BatchGetBuilds"], action)
      ]])) &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.destroy_dispatcher[0].policy).Statement :
        !contains(statement.Action, "lambda:InvokeFunction") || statement.Resource == "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher"
    ]))
    error_message = "Cleanup request must remain available without deploy control, trusting only its main-only environment, read-only evidence and the fixed launcher."
  }
  assert {
    condition     = length(aws_iam_role.plan_review) == 2
    error_message = "Plan review requires separate reader and approver roles."
  }
  assert {
    condition = anytrue([
      for statement in data.aws_iam_policy_document.control.statement :
      statement.sid == "OnlyPlanBridgeCanWriteApprovals" && statement.effect == "Deny" &&
      contains(statement.actions, "s3:PutObject") && contains(statement.resources, "arn:aws:s3:::techx-test-control/approvals/staging/*") &&
      anytrue([for condition in statement.condition :
        condition.test == "ArnNotEquals" && condition.variable == "aws:PrincipalArn" &&
        contains(condition.values, "arn:aws:iam::058114477594:role/techx-automation-plan-approver")
      ])
    ])
    error_message = "Bucket policy must deny approval writes from every principal except the trusted bridge role."
  }
  assert {
    condition = alltrue(flatten([
      for name, role in aws_iam_role.plan_review : [
        for statement in jsondecode(role.assume_role_policy).Statement :
        statement.Condition.StringEquals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com" &&
        statement.Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:cloud-computing-microservice/techx-infra:environment:${name == "approver" ? "staging-apply" : "staging-plan-read"}"
      ]
    ]))
    error_message = "OIDC roles must trust only the exact protected infra environments."
  }
  assert {
    condition = alltrue(flatten([
      for name, policy in aws_iam_role_policy.plan_review : [
        for statement in jsondecode(policy.policy).Statement :
        !contains(statement.Action, "s3:PutObject") || (name == "approver" && statement.Resource == "arn:aws:s3:::techx-test-control/approvals/staging/*")
      ]
    ]))
    error_message = "Only the approver may write, and only to approval objects; neither role may change state/session."
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
    condition = anytrue([
      for statement in jsondecode(aws_iam_policy.cleanup_executor["network"].policy).Statement :
      contains(statement.Action, "ec2:TerminateInstances") &&
      statement.Condition.StringEquals["ec2:ResourceTag/Project"] == "techx" &&
      statement.Condition.StringEquals["ec2:ResourceTag/Environment"] == "staging" &&
      toset(statement.Condition.StringEquals["ec2:ResourceTag/Role"]) == toset(["private-probe", "deployment-executor"])
    ])
    error_message = "Cleanup must stop only tagged staging probes/executors, including detached remote deployment work."
  }
  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_policy.cleanup_executor["services"].policy).Statement :
      contains(statement.Action, "logs:DeleteLogGroup") &&
      contains(statement.Resource, "arn:aws:logs:us-east-1:058114477594:log-group:/aws/client-vpn/techx-staging*")
    ])
    error_message = "Cleanup log policy must cover the actual /aws/client-vpn module log path."
  }
  assert {
    condition = anytrue([
      for statement in jsondecode(aws_iam_policy.cleanup_executor["storage"].policy).Statement :
      contains(statement.Action, "s3:GetObject") &&
      contains(try(tolist(statement.Resource), []), "arn:aws:s3:::techx-test-control/cleanup/*")
    ])
    error_message = "Cleanup must be able to read its durable executor/certificate receipts on retry."
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

run "approval_requires_cleanup" {
  command = plan
  variables {
    enable_plan_approval   = true
    enable_cleanup_control = false
  }
  expect_failures = [var.enable_plan_approval]
}

run "lifecycle_is_scoped_and_pinned" {
  command = apply
  variables {
    enable_cleanup_control   = true
    enable_plan_approval     = true
    enable_lifecycle_control = true
    executor_ami_id          = "ami-1234567890abcdef0"
  }
  override_data {
    target = data.aws_ami.executor[0]
    values = {
      id = "ami-1234567890abcdef0", architecture = "x86_64", root_device_type = "ebs", root_device_name = "/dev/sda1", platform_details = "Linux/UNIX"
      tags = {
        Project      = "techx", Role = "deployment-executor-image"
        LoaderSha256 = sha256(join("", [for name in sort(["automation_bundle.py", "cleanup_launcher.py", "cleanup_loader.py", "deployment_task_loader.py"]) : "${sha256(replace(file(name == "automation_bundle.py" ? "../../scripts/${name}" : name), "\r\n", "\n"))}  ${name}\n"]))
        BuildSha256  = sha256(jsonencode({ for name in ["automation_bundle.py", "cleanup_launcher.py", "cleanup_loader.py", "deployment_task_loader.py", "install-executor.sh", "install_automation_tools.py", "toolchain-linux-amd64.json", "executor-image-tools.json", "executor-python.lock", "executor-aws-cli-public-key.asc", "verify_executor_ssm.py"] : name => sha256(replace(file(contains(["automation_bundle.py", "install_automation_tools.py", "toolchain-linux-amd64.json"], name) ? "../../scripts/${name}" : name), "\r\n", "\n")) }))
      }
    }
  }
  assert {
    condition     = aws_codebuild_project.lifecycle[0].source[0].type == "NO_SOURCE" && !aws_codebuild_project.lifecycle[0].environment[0].privileged_mode && aws_codebuild_project.lifecycle[0].concurrent_build_limit == 1
    error_message = "Manager must use the immutable loader, no Docker privilege and one build at a time."
  }
  assert {
    condition = toset(jsondecode(aws_iam_role.lifecycle_dispatcher[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset([
      "repo:cloud-computing-microservice/techx-infra:environment:staging-lifecycle", "repo:cloud-computing-microservice/techx-infra:environment:staging-start"
    ])
    error_message = "The opener and continuation must use the two protected environment subjects only."
  }
  assert {
    condition = alltrue(flatten([for statement in jsondecode(aws_iam_role_policy.lifecycle_admission_inventory[0].policy).Statement : [for action in statement.Action :
      can(regex("^[^:]+:(Describe|List|Get)", action))
    ]]))
    error_message = "Startup inventory authority must remain read-only."
  }
  assert {
    condition     = aws_launch_template.executor[0].instance_type == "t3.small" && aws_launch_template.executor[0].metadata_options[0].http_tokens == "required" && aws_launch_template.executor[0].disable_api_stop && aws_launch_template.executor[0].credit_specification[0].cpu_credits == "standard"
    error_message = "Executor launch must enforce the bounded instance and IMDS/stability controls."
  }
  assert {
    condition = (length(jsondecode(aws_ssm_document.deployment_task[0].content).mainSteps) == 1 &&
      jsondecode(aws_ssm_document.deployment_task[0].content).parameters.TaskId.interpolationType == "ENV_VAR" &&
      jsondecode(aws_ssm_document.deployment_task[0].content).parameters.TaskId.allowedPattern == "^[a-f0-9]{32}$" &&
    keys(jsondecode(aws_ssm_document.deployment_task[0].content).parameters) == ["TaskId"])
    error_message = "Remote document may accept only the typed TaskId, never a caller-selected shell command."
  }
  assert {
    condition     = length(aws_security_group.executor_control[0].ingress) == 0
    error_message = "Executor control network must have no inbound rule."
  }
  assert {
    condition     = alltrue([for p in aws_iam_policy.lifecycle_manager : length(p.policy) <= 6144]) && alltrue([for p in aws_iam_policy.deployment_executor : length(p.policy) <= 6144]) && length(aws_iam_policy.workload_boundary[0].policy) <= 6144
    error_message = "All managed role policies and the workload boundary must fit AWS limits."
  }
  assert {
    condition = alltrue(flatten([for policy in aws_iam_policy.lifecycle_manager : [for statement in jsondecode(policy.policy).Statement :
      !contains(statement.Action, "eks:CreateCluster") && !contains(statement.Action, "iam:CreateRole")
    ]]))
    error_message = "The manager cannot create workload clusters or IAM roles."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_policy.deployment_executor["identity"].policy).Statement :
      statement.Condition.StringEquals["iam:PermissionsBoundary"] == "arn:aws:iam::058114477594:policy/techx-automation-workload-boundary"
      if contains(statement.Action, "iam:CreateRole")
    ])
    error_message = "Executor-created application roles must carry the reviewed boundary."
  }
  assert {
    condition = alltrue(flatten([for policy in aws_iam_policy.deployment_executor : [for statement in jsondecode(policy.policy).Statement :
      !contains(statement.Action, "iam:PutRolePermissionsBoundary") && !contains(statement.Action, "iam:DeleteRolePermissionsBoundary") && !contains(statement.Action, "*")
    ]]))
    error_message = "Deployment cannot remove/change boundaries or grant arbitrary account-admin actions."
  }
  assert {
    # AmazonEKS_CNI_Policy v6: ENI attribute updates are required as well as
    # attachment/address changes. Missing this ceiling permission breaks CNI.
    condition = anytrue([for statement in jsondecode(aws_iam_policy.workload_boundary[0].policy).Statement :
      contains(statement.Action, "ec2:ModifyNetworkInterfaceAttribute") &&
      contains(try(tolist(statement.Resource), []), "arn:aws:ec2:us-east-1:058114477594:network-interface/*")
    ])
    error_message = "The scoped boundary must permit CNI ENI attribute updates."
  }
  assert {
    condition = anytrue([for statement in jsondecode(aws_iam_policy.deployment_executor["edge"].policy).Statement :
      contains(statement.Action, "acm:RequestCertificate") && statement.Resource == "*" &&
      try(statement.Condition.StringEquals["acm:Export"] == "DISABLED", false) &&
      try(statement.Condition["ForAllValues:StringEquals"]["acm:DomainNames"] == ["shop.dinhminhkhoa.id.vn"], false)
    ])
    error_message = "The resource-less ACM request action must constrain domain and forbid exportable certificates."
  }
  assert {
    condition = (length(aws_iam_role.bootstrap_publisher) == 1 &&
      jsondecode(aws_iam_role.bootstrap_publisher[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:cloud-computing-microservice/techx-platform:environment:staging-bootstrap" &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.bootstrap_publisher_control[0].policy).Statement :
        toset(statement.Resource) == toset(["arn:aws:s3:::techx-test-control/releases/staging/*/bootstrap.json", "arn:aws:s3:::techx-test-control/proposals/staging/*/bootstrap.json"])
      if contains(statement.Action, "s3:PutObject")])
    )
    error_message = "Cold publish needs a separate protected OIDC subject and a scoped immutable receipt prefix."
  }
  assert {
    condition = (toset(jsondecode(aws_iam_role.chart_selector[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"]) == toset([
      "repo:cloud-computing-microservice/techx-infra:environment:staging-chart-select", "repo:cloud-computing-microservice/techx-infra:environment:staging-chart-reuse"
      ]) &&
      alltrue(flatten([for statement in jsondecode(aws_iam_role_policy.chart_selector[0].policy).Statement : [for action in statement.Action :
        !contains(["codebuild:StartBuild", "iam:PassRole", "ec2:RunInstances", "ecr:PutImage", "secretsmanager:GetSecretValue"], action)
      ]]))
    )
    error_message = "Chart selector must have a protected subject and no deploy/publish/secret credentials authority."
  }
  assert {
    condition = (jsondecode(aws_iam_role.chart_reader[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:cloud-computing-microservice/techx-infra:environment:staging-chart-read" &&
      alltrue(flatten([for statement in jsondecode(aws_iam_role_policy.chart_reader[0].policy).Statement : [for action in statement.Action :
        contains(["s3:GetObject", "s3:GetObjectVersion", "scheduler:GetSchedule", "kms:Decrypt"], action)
      ]]))
    )
    error_message = "Reuse preview identity must be read-only and trust only its exact main-only environment."
  }
  assert {
    condition = length([for statement in data.aws_iam_policy_document.control.statement : statement if
      statement.sid == "OnlyChartSelectorCanWriteSelections" && statement.effect == "Deny" &&
      contains(statement.resources, "arn:aws:s3:::techx-test-control/chart-selections/staging/*")
    ]) == 1
    error_message = "A bucket deny must reserve chart-selection writes for the selector role."
  }
  assert {
    condition = length([for statement in data.aws_iam_policy_document.control.statement : statement if
      statement.sid == "OnlyBootstrapPublisherCanWriteProposals" && statement.effect == "Deny" &&
      contains(statement.resources, "arn:aws:s3:::techx-test-control/proposals/staging/*")
    ]) == 1
    error_message = "Only the cold publisher may write immutable proposal receipts."
  }
  assert {
    condition = (length([for statement in data.aws_iam_policy_document.control.statement : statement if
      statement.sid == "OnlyLifecycleDispatcherCanWriteHandoffs" && statement.effect == "Deny" &&
      contains(statement.resources, "arn:aws:s3:::techx-test-control/handoffs/staging/*") &&
      anytrue([for condition in statement.condition : condition.variable == "aws:PrincipalArn" &&
        contains(condition.values, "arn:aws:iam::058114477594:role/techx-automation-lifecycle-dispatcher")
      ])
      ]) == 1 && length(aws_iam_role_policy.handoff_children) == 5 &&
      alltrue(flatten([for policy in aws_iam_role_policy.handoff_children : [for statement in jsondecode(policy.policy).Statement : [for action in statement.Action :
        contains(["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"], action)
    ]]])))
    error_message = "Only the dispatcher may write protected handoffs; every child must have read-only receipt access."
  }
  assert {
    condition = (length([for statement in data.aws_iam_policy_document.control.statement : statement if
      statement.sid == "OnlyPromotionApproverCanWritePromotionApprovals" && statement.effect == "Deny" &&
      contains(statement.resources, "arn:aws:s3:::techx-test-control/promotion-approvals/staging/*") &&
      anytrue([for condition in statement.condition : condition.variable == "aws:PrincipalArn" &&
        contains(condition.values, "arn:aws:iam::058114477594:role/techx-automation-promotion-approver")
      ])
      ]) == 1 && length(aws_iam_role.promotion_approver) == 1 &&
      jsondecode(aws_iam_role.promotion_approver[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:cloud-computing-microservice/techx-infra:environment:staging-chart-promote" &&
      alltrue(flatten([for statement in jsondecode(aws_iam_role_policy.promotion_approver[0].policy).Statement : [for action in statement.Action :
        contains(["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "scheduler:GetSchedule", "kms:Decrypt", "kms:GenerateDataKey"], action)
      ]])) &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.promotion_approver[0].policy).Statement :
        statement.Resource == "arn:aws:s3:::techx-test-control/promotion-approvals/staging/*/*.json" if contains(statement.Action, "s3:PutObject")
    ]))
    error_message = "Only a protected promotion-approver may write grants; it cannot mutate session, workload or GitHub."
  }
  assert {
    condition = (length([for statement in data.aws_iam_policy_document.control.statement : statement if
      statement.sid == "OnlyPrivateExecutorCanWritePromotionCalls" && statement.effect == "Deny" &&
      contains(statement.resources, "arn:aws:s3:::techx-test-control/promotions/staging/*") &&
      anytrue([for condition in statement.condition : condition.variable == "aws:PrincipalArn" &&
        contains(condition.values, "arn:aws:iam::058114477594:role/techx-automation-lifecycle-executor")
      ])
      ]) == 1 && length(aws_iam_role_policy.private_promotion) == 1 &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.private_promotion[0].policy).Statement :
        statement.Resource == "arn:aws:s3:::techx-test-control/promotions/staging/*" if contains(statement.Action, "s3:PutObject")
        ]) && alltrue([for statement in jsondecode(aws_iam_role_policy.private_promotion[0].policy).Statement :
        toset(statement.Resource) == toset(["arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/promotion-app-??????", "arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/lifecycle-app-??????"]) &&
        statement.Condition.StringEquals["aws:ResourceTag/Role"] == "github-app" && statement.Condition.StringEquals["aws:ResourceTag/Environment"] == "automation"
        if contains(statement.Action, "secretsmanager:GetSecretValue")
    ]))
    error_message = "Only the owned executor writes merge calls and reads the two fixed tagged App keys; grants stay separate."
  }
}

run "lifecycle_requires_dependencies" {
  command = plan
  variables {
    enable_lifecycle_control = true
    executor_ami_id          = "ami-1234567890abcdef0"
  }
  expect_failures = [var.enable_lifecycle_control]
}

run "image_pipeline_is_manual_and_terminates_failed_builds" {
  command = apply
  variables {
    enable_executor_image_build = true
    executor_parent_ami_id      = "ami-1234567890abcdef0"
  }
  assert {
    condition     = aws_imagebuilder_infrastructure_configuration.executor[0].terminate_instance_on_failure && length(aws_imagebuilder_image_pipeline.executor[0].schedule) == 0
    error_message = "Image Builder must be explicitly started and terminate its failed build instance independently of GitHub."
  }
  assert {
    condition     = length(aws_codebuild_project.lifecycle) == 0 && length(aws_s3_object.executor_config) == 0 && length(aws_imagebuilder_image_pipeline.executor) == 1
    error_message = "Creating the image pipeline must not activate workload deployment or start a build."
  }
}
