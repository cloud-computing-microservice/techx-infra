locals {
  executor_image_files = merge(local.executor_bootstrap_files, {
    "install-executor.sh"             = "${path.module}/install-executor.sh"
    "install_automation_tools.py"     = "${path.module}/../../scripts/install_automation_tools.py"
    "toolchain-linux-amd64.json"      = "${path.module}/../../scripts/toolchain-linux-amd64.json"
    "executor-image-tools.json"       = "${path.module}/executor-image-tools.json"
    "executor-python.lock"            = "${path.module}/executor-python.lock"
    "executor-aws-cli-public-key.asc" = "${path.module}/executor-aws-cli-public-key.asc"
    "verify_executor_ssm.py"          = "${path.module}/verify_executor_ssm.py"
  })
  executor_image_asset_sha256 = sha256(jsonencode({ for name, path in local.executor_image_files : name => sha256(replace(file(path), "\r\n", "\n")) }))
}

data "archive_file" "executor_bootstrap" {
  count       = var.enable_executor_image_build ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/executor-bootstrap.zip"
  dynamic "source" {
    for_each = local.executor_image_files
    content {
      filename = source.key
      content  = replace(file(source.value), "\r\n", "\n")
    }
  }
  source {
    filename = "SHA256SUMS"
    content  = local.executor_manifest
  }
}

resource "aws_s3_object" "executor_bootstrap" {
  count                  = var.enable_executor_image_build ? 1 : 0
  bucket                 = aws_s3_bucket.control.id
  key                    = "bootstrap/executor/${local.executor_image_asset_sha256}.zip"
  source                 = data.archive_file.executor_bootstrap[0].output_path
  source_hash            = data.archive_file.executor_bootstrap[0].output_sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.control.arn
  depends_on             = [aws_s3_bucket_versioning.control]
}

data "aws_ami" "executor_parent" {
  count  = var.enable_executor_image_build ? 1 : 0
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = [var.executor_parent_ami_id]
  }
  lifecycle {
    postcondition {
      condition     = self.architecture == "x86_64" && self.root_device_type == "ebs" && can(regex("ubuntu.*24.04.*", self.name))
      error_message = "The exact reviewed parent must be Canonical Ubuntu 24.04 amd64, not an arbitrary public image."
    }
  }
}

resource "aws_iam_role" "executor_image_builder" {
  count = var.enable_executor_image_build ? 1 : 0
  name  = "techx-automation-executor-image-builder"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
}

resource "aws_iam_instance_profile" "executor_image_builder" {
  count = var.enable_executor_image_build ? 1 : 0
  name  = "techx-automation-executor-image-builder"
  role  = aws_iam_role.executor_image_builder[0].name
}

resource "aws_iam_role_policy_attachment" "image_builder_managed" {
  for_each   = var.enable_executor_image_build ? toset(["EC2InstanceProfileForImageBuilder", "AmazonSSMManagedInstanceCore"]) : toset([])
  role       = aws_iam_role.executor_image_builder[0].name
  policy_arn = "arn:aws:iam::aws:policy/${each.key}"
}

resource "aws_iam_role_policy" "image_builder_asset" {
  count = var.enable_executor_image_build ? 1 : 0
  name  = "read-reviewed-executor-bootstrap-write-build-logs"
  role  = aws_iam_role.executor_image_builder[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = aws_s3_object.executor_bootstrap[0].arn },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.control.arn}/imagebuilder/logs/*" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn, Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } }
  ] })
}

resource "aws_imagebuilder_component" "executor" {
  count       = var.enable_executor_image_build ? 1 : 0
  name        = "techx-automation-deployment-executor"
  platform    = "Linux"
  version     = var.executor_image_version
  description = "Reviewed Ubuntu executor with checksum-pinned tools and bootstrap manifest"
  data = yamlencode({ schemaVersion = "1.0", name = "TechxDeploymentExecutor", phases = [
    { name = "build", steps = [
      { name   = "DownloadBootstrap", action = "S3Download", onFailure = "Abort", maxAttempts = 1,
        inputs = [{ source = "s3://${aws_s3_bucket.control.id}/${aws_s3_object.executor_bootstrap[0].key}", destination = "/tmp/techx-executor-bootstrap.zip" }]
      },
      { name = "InstallReviewedTools", action = "ExecuteBash", onFailure = "Abort", maxAttempts = 1, timeoutSeconds = 3600,
        inputs = { commands = [
          "set -eu",
          "printf '%s  %s\n' '${data.archive_file.executor_bootstrap[0].output_sha256}' /tmp/techx-executor-bootstrap.zip | sha256sum --check --status",
          "sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources",
          "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends unzip",
          "mkdir -p /tmp/techx-executor-bootstrap && unzip -q /tmp/techx-executor-bootstrap.zip -d /tmp/techx-executor-bootstrap",
          "bash /tmp/techx-executor-bootstrap/install-executor.sh"
        ] }
      }
    ] },
    { name = "test", steps = [
      { name   = "VerifyBakedBootstrap", action = "ExecuteBash", onFailure = "Abort", maxAttempts = 1, timeoutSeconds = 300,
        inputs = { commands = ["set -eu", "cd /opt/techx/bootstrap && sha256sum --check --status SHA256SUMS", "/opt/techx/venv/bin/python -m compileall -q /opt/techx/bootstrap", "/opt/techx/venv/bin/python /opt/techx/verify_executor_ssm.py", "aws --version", "pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'", "terraform version"] }
      }
    ] }
  ] })
  tags = { Role = "deployment-executor-image" }
}

resource "aws_imagebuilder_image_recipe" "executor" {
  count        = var.enable_executor_image_build ? 1 : 0
  name         = "techx-automation-deployment-executor"
  parent_image = data.aws_ami.executor_parent[0].id
  version      = var.executor_image_version
  component { component_arn = aws_imagebuilder_component.executor[0].arn }
  block_device_mapping {
    device_name = data.aws_ami.executor_parent[0].root_device_name
    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tags = { Role = "deployment-executor-image", LoaderSha256 = local.executor_loader_sha256, BuildSha256 = local.executor_image_asset_sha256 }
}

resource "aws_imagebuilder_infrastructure_configuration" "executor" {
  count                         = var.enable_executor_image_build ? 1 : 0
  name                          = "techx-automation-deployment-executor"
  instance_profile_name         = aws_iam_instance_profile.executor_image_builder[0].name
  instance_types                = ["t3.small"]
  subnet_id                     = aws_subnet.executor_control[0].id
  security_group_ids            = [aws_security_group.executor_control[0].id]
  terminate_instance_on_failure = true
  instance_metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  resource_tags = { Project = "techx", Environment = "automation", Role = "deployment-image-builder" }
  logging {
    s3_logs {
      s3_bucket_name = aws_s3_bucket.control.id
      s3_key_prefix  = "imagebuilder/logs"
    }
  }
  depends_on = [aws_iam_role_policy_attachment.image_builder_managed, aws_iam_role_policy.image_builder_asset, aws_route.executor_internet]
}

resource "aws_imagebuilder_distribution_configuration" "executor" {
  count = var.enable_executor_image_build ? 1 : 0
  name  = "techx-automation-deployment-executor"
  distribution {
    region = "us-east-1"
    ami_distribution_configuration {
      name        = "techx-automation-deployment-executor-{{ imagebuilder:buildDate }}"
      description = "Private TechX executor; exact AMI and bootstrap hashes must be reviewed before lifecycle activation"
      ami_tags    = { Project = "techx", Environment = "automation", Role = "deployment-executor-image", LoaderSha256 = local.executor_loader_sha256, BuildSha256 = local.executor_image_asset_sha256 }
    }
  }
}

resource "aws_imagebuilder_image_pipeline" "executor" {
  count                            = var.enable_executor_image_build ? 1 : 0
  name                             = "techx-automation-deployment-executor"
  status                           = "ENABLED"
  image_recipe_arn                 = aws_imagebuilder_image_recipe.executor[0].arn
  infrastructure_configuration_arn = aws_imagebuilder_infrastructure_configuration.executor[0].arn
  distribution_configuration_arn   = aws_imagebuilder_distribution_configuration.executor[0].arn
  image_tests_configuration {
    image_tests_enabled = true
    timeout_minutes     = 60
  }
  # Intentionally no schedule: only an explicitly approved workflow may start it.
  depends_on = [aws_s3_object.executor_bootstrap]
}

output "executor_image_pipeline_arn" { value = var.enable_executor_image_build ? aws_imagebuilder_image_pipeline.executor[0].arn : null }
output "executor_image_asset_sha256" { value = local.executor_image_asset_sha256 }
