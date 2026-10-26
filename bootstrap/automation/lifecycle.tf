variable "enable_lifecycle_control" {
  description = "Enable reviewed lifecycle resources only after GitHub settings, AMI and independent cleanup integration are accepted."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_lifecycle_control || (var.enable_cleanup_control && var.enable_plan_approval && can(regex("^ami-[a-f0-9]{8,17}$", var.executor_ami_id)))
    error_message = "Lifecycle control requires cleanup, protected plan approval and an explicit account-owned executor AMI."
  }
}

variable "enable_executor_image_build" {
  description = "Create an on-demand Image Builder pipeline; does not start a paid image build on apply."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_executor_image_build || can(regex("^ami-[a-f0-9]{8,17}$", var.executor_parent_ami_id))
    error_message = "Image Builder requires an explicit reviewed Ubuntu 24.04 amd64 parent AMI ID."
  }
}

variable "executor_ami_id" {
  description = "Private baked executor AMI in account 058114477594/us-east-1; never use a mutable latest-image lookup."
  type        = string
  default     = ""
}

variable "executor_parent_ami_id" {
  description = "Exact Canonical Ubuntu 24.04 amd64 base AMI, used only by the separately authorized Image Builder run."
  type        = string
  default     = ""
}

variable "executor_image_version" {
  description = "Bump the image recipe/component semantic version when bootstrap or toolchain content changes."
  type        = string
  default     = "1.0.0"
  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.executor_image_version))
    error_message = "An explicit semantic image version is required."
  }
}

locals {
  executor_network_enabled = var.enable_lifecycle_control || var.enable_executor_image_build
  executor_network_tags    = { Project = "techx", Environment = "automation", Role = "deployment-control" }
  executor_bootstrap_files = {
    "automation_bundle.py"      = "${path.module}/../../scripts/automation_bundle.py"
    "cleanup_launcher.py"       = "${path.module}/cleanup_launcher.py"
    "cleanup_loader.py"         = "${path.module}/cleanup_loader.py"
    "deployment_task_loader.py" = "${path.module}/deployment_task_loader.py"
  }
  executor_manifest      = join("", [for name in sort(keys(local.executor_bootstrap_files)) : "${sha256(replace(file(local.executor_bootstrap_files[name]), "\r\n", "\n"))}  ${name}\n"])
  executor_loader_sha256 = sha256(local.executor_manifest)
  lifecycle_project_arn  = "arn:aws:codebuild:us-east-1:058114477594:project/techx-automation-lifecycle"
}

resource "aws_vpc" "executor_control" {
  count                = local.executor_network_enabled ? 1 : 0
  cidr_block           = "172.30.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = local.executor_network_tags
}

resource "aws_subnet" "executor_control" {
  count                   = local.executor_network_enabled ? 1 : 0
  vpc_id                  = aws_vpc.executor_control[0].id
  cidr_block              = "172.30.0.0/26"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = true
  tags                    = local.executor_network_tags
}

resource "aws_internet_gateway" "executor_control" {
  count  = local.executor_network_enabled ? 1 : 0
  vpc_id = aws_vpc.executor_control[0].id
  tags   = local.executor_network_tags
}

resource "aws_route_table" "executor_control" {
  count  = local.executor_network_enabled ? 1 : 0
  vpc_id = aws_vpc.executor_control[0].id
  tags   = local.executor_network_tags
}

resource "aws_route" "executor_internet" {
  count                  = local.executor_network_enabled ? 1 : 0
  route_table_id         = aws_route_table.executor_control[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.executor_control[0].id
}

resource "aws_route_table_association" "executor_control" {
  count          = local.executor_network_enabled ? 1 : 0
  subnet_id      = aws_subnet.executor_control[0].id
  route_table_id = aws_route_table.executor_control[0].id
}

resource "aws_security_group" "executor_control" {
  count       = local.executor_network_enabled ? 1 : 0
  name        = "techx-automation-deployment-control"
  description = "No inbound; HTTPS, Client VPN UDP and VPC DNS egress only; no SSH or NAT"
  vpc_id      = aws_vpc.executor_control[0].id
  tags        = local.executor_network_tags
}

# No-NAT control plane needs dynamic AWS/SSM/EKS and signed tool HTTPS endpoints.
# Only TCP/443 is excepted; inbound is empty, DNS is /32 and downloads are hashed.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "executor_https" {
  count             = local.executor_network_enabled ? 1 : 0
  security_group_id = aws_security_group.executor_control[0].id
  description       = "AWS APIs, SSM, pinned tool downloads and EKS public endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "executor_dns" {
  for_each          = local.executor_network_enabled ? toset(["tcp", "udp"]) : toset([])
  security_group_id = aws_security_group.executor_control[0].id
  description       = "Owned control VPC resolver only"
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
  cidr_ipv4         = "172.30.0.2/32"
}

# Client VPN endpoints resolve to dynamic public addresses. The worker pins the
# endpoint/account/region, mutual-TLS CA and server identity before connecting.
# No inbound rule, general UDP or internet DNS exception is introduced.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "executor_vpn" {
  count             = local.executor_network_enabled ? 1 : 0
  security_group_id = aws_security_group.executor_control[0].id
  description       = "Owned staging Client VPN over mutual-TLS UDP 443"
  ip_protocol       = "udp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

data "aws_ami" "executor" {
  count  = var.enable_lifecycle_control ? 1 : 0
  owners = ["058114477594"]
  filter {
    name   = "image-id"
    values = [var.executor_ami_id]
  }
  lifecycle {
    postcondition {
      condition = (self.architecture == "x86_64" && self.root_device_type == "ebs" && self.platform_details == "Linux/UNIX" &&
      lookup(self.tags, "Project", "") == "techx" && lookup(self.tags, "Role", "") == "deployment-executor-image" && lookup(self.tags, "LoaderSha256", "") == local.executor_loader_sha256 && lookup(self.tags, "BuildSha256", "") == local.executor_image_asset_sha256)
      error_message = "Executor AMI must match this reviewed bootstrap manifest and account/platform."
    }
  }
}

resource "aws_launch_template" "executor" {
  count                                = var.enable_lifecycle_control ? 1 : 0
  name                                 = "techx-automation-deployment-executor"
  image_id                             = var.executor_ami_id
  instance_type                        = "t3.small"
  disable_api_stop                     = true
  disable_api_termination              = false
  instance_initiated_shutdown_behavior = "terminate"
  iam_instance_profile { arn = aws_iam_instance_profile.deployment_executor[0].arn }
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  network_interfaces {
    device_index                = 0
    associate_public_ip_address = true
    delete_on_termination       = true
    subnet_id                   = aws_subnet.executor_control[0].id
    security_groups             = [aws_security_group.executor_control[0].id]
  }
  block_device_mappings {
    device_name = data.aws_ami.executor[0].root_device_name
    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tags       = merge(local.executor_network_tags, { Role = "deployment-executor-template" })
  depends_on = [aws_route.executor_internet, aws_route_table_association.executor_control]
}

resource "aws_ssm_document" "deployment_task" {
  count           = var.enable_lifecycle_control ? 1 : 0
  name            = "techx-automation-deployment-task"
  document_type   = "Command"
  document_format = "JSON"
  target_type     = "/AWS::EC2::Instance"
  content = jsonencode({
    schemaVersion = "2.2", description = "Run exactly one admitted immutable TechX task; no caller shell commands"
    parameters    = { TaskId = { type = "String", allowedPattern = "^[a-f0-9]{32}$", interpolationType = "ENV_VAR" } }
    mainSteps = [{
      action = "aws:runShellScript", name = "runTask", precondition = { StringEquals = ["platformType", "Linux"] }
      inputs = { timeoutSeconds = "5400", runCommand = [
        "export CLOUDFLARE_SECRET_ARN='${var.cloudflare_secret_arn}'",
        "set -eu",
        "cd /opt/techx/bootstrap",
        "printf '%s  %s\\n' '${local.executor_loader_sha256}' SHA256SUMS | sha256sum --check --status",
        "sha256sum --check --status SHA256SUMS",
        "exec /opt/techx/venv/bin/python deployment_task_loader.py --task-id \"$SSM_TaskId\" --bucket '${aws_s3_bucket.control.id}' --kms-key '${aws_kms_key.control.arn}'"
      ] }
    }]
  })
  tags = local.executor_network_tags
  lifecycle {
    postcondition {
      condition     = self.hash_type == "Sha256"
      error_message = "Task document must publish the SHA256 hash required by the manager's pinned dispatch."
    }
  }
}

resource "aws_s3_object" "executor_config" {
  count  = var.enable_lifecycle_control ? 1 : 0
  bucket = aws_s3_bucket.control.id
  key    = "control/staging/executor-config.json"
  content = jsonencode({
    schemaVersion         = 1, imageId = var.executor_ami_id, launchTemplateId = aws_launch_template.executor[0].id
    launchTemplateVersion = tostring(aws_launch_template.executor[0].latest_version), subnetId = aws_subnet.executor_control[0].id
    securityGroupId       = aws_security_group.executor_control[0].id, loaderSha256 = local.executor_loader_sha256, imageBuildSha256 = local.executor_image_asset_sha256
    documentVersion       = aws_ssm_document.deployment_task[0].latest_version, documentSha256 = aws_ssm_document.deployment_task[0].hash
  })
  content_type           = "application/json"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.control.arn
  depends_on             = [aws_s3_bucket_versioning.control]
}

resource "aws_iam_role" "lifecycle_manager" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-lifecycle-manager"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" }
    Condition = { StringEquals = { "aws:SourceAccount" = "058114477594" }, ArnEquals = { "aws:SourceArn" = local.lifecycle_project_arn } }
  }] })
}

resource "aws_cloudwatch_log_group" "lifecycle" {
  count             = var.enable_lifecycle_control ? 1 : 0
  name              = "/aws/codebuild/techx-automation-lifecycle"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.control.arn
}

resource "aws_codebuild_project" "lifecycle" {
  count                  = var.enable_lifecycle_control ? 1 : 0
  name                   = "techx-automation-lifecycle"
  service_role           = aws_iam_role.lifecycle_manager[0].arn
  encryption_key         = aws_kms_key.control.arn
  build_timeout          = 100
  queued_timeout         = 5
  concurrent_build_limit = 1
  artifacts { type = "NO_ARTIFACTS" }
  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = false
    environment_variable {
      name  = "CONTROL_BUCKET"
      value = aws_s3_bucket.control.id
    }
  }
  source {
    type = "NO_SOURCE"
    buildspec = jsonencode({ version = 0.2, phases = {
      install = { runtime-versions = { python = "3.13" }, commands = ["python -m pip install boto3==1.43.7"] }
      build = { commands = concat([for name in ["automation_bundle.py", "cleanup_launcher.py", "cleanup_loader.py"] :
        "cat > /tmp/${name} <<'PY'\n${file(local.executor_bootstrap_files[name])}\nPY"
        ], ["cat > /tmp/lifecycle_loader.py <<'PY'\n${file("${path.module}/lifecycle_loader.py")}\nPY", "python /tmp/lifecycle_loader.py"])
      }
    } })
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.lifecycle[0].name
      status     = "ENABLED"
    }
  }
  depends_on = [aws_iam_role_policy_attachment.lifecycle_manager, aws_s3_object.executor_config]
}

output "executor_bootstrap_sha256" { value = local.executor_loader_sha256 }
output "lifecycle_project_name" { value = var.enable_lifecycle_control ? aws_codebuild_project.lifecycle[0].name : null }
