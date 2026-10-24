variable "cloudflare_secret_arn" {
  description = "Existing shared secret with apiToken and zoneId JSON keys. Never stored in execution bundles."
  type        = string
  default     = ""
  validation {
    condition     = !var.enable_cleanup_control || can(regex("^arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/cloudflare-[A-Za-z0-9]+$", var.cloudflare_secret_arn))
    error_message = "Cleanup control requires the scoped shared Cloudflare secret ARN."
  }
}

locals {
  cleanup_policies = {
    storage = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = aws_s3_bucket.control.arn },
        { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [
          "${aws_s3_bucket.control.arn}/executions/*", "${aws_s3_bucket.control.arn}/bundles/*",
          "${aws_s3_bucket.control.arn}/private/staging/*", "${aws_s3_bucket.control.arn}/staging/terraform.tfstate",
          "${aws_s3_bucket.control.arn}/staging/terraform.tfstate.tflock", "${aws_s3_bucket.control.arn}/automation/staging/session.json"
        ] },
        { Effect = "Allow", Action = ["s3:PutObject"], Resource = [
          "${aws_s3_bucket.control.arn}/cleanup/*", "${aws_s3_bucket.control.arn}/staging/terraform.tfstate",
          "${aws_s3_bucket.control.arn}/staging/terraform.tfstate.tflock", "${aws_s3_bucket.control.arn}/automation/staging/session.json"
        ] },
        { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${aws_s3_bucket.control.arn}/staging/terraform.tfstate.tflock" },
        { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn,
        Condition = { StringEquals = { "kms:ViaService" = ["s3.us-east-1.amazonaws.com", "secretsmanager.us-east-1.amazonaws.com"] } } },
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.cloudflare_secret_arn },
        { Effect = "Allow", Action = ["secretsmanager:DeleteSecret"], Resource = "arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/staging/*",
        Condition = { StringEquals = { "aws:ResourceTag/Project" = "techx", "aws:ResourceTag/Environment" = "staging" } } }
      ]
    }
    read = {
      Version = "2012-10-17", Statement = [{
        Effect = "Allow", Resource = "*", Action = [
          "ec2:Describe*", "eks:ListClusters", "iam:ListRoles", "iam:ListPolicies", "tag:GetResources",
          "cloudfront:ListDistributions", "cloudfront:ListVpcOrigins", "route53:ListHostedZones", "route53:ListHostedZonesByName",
          "acm:ListCertificates", "logs:DescribeLogGroups", "secretsmanager:ListSecrets"
        ]
      }]
    }
    cluster = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = [
          "eks:DescribeCluster", "eks:DescribeUpdate", "eks:UpdateClusterConfig", "eks:DeleteCluster", "eks:ListNodegroups", "eks:ListAddons",
          "eks:DescribeNodegroup", "eks:DeleteNodegroup", "eks:DescribeAddon", "eks:DeleteAddon", "eks:ListTagsForResource",
          "eks:ListAccessEntries", "eks:DescribeAccessEntry", "eks:CreateAccessEntry", "eks:DeleteAccessEntry",
          "eks:AssociateAccessPolicy", "eks:DisassociateAccessPolicy", "eks:ListAssociatedAccessPolicies"
          ], Resource = [
          "arn:aws:eks:us-east-1:058114477594:cluster/techx-staging",
          "arn:aws:eks:us-east-1:058114477594:nodegroup/techx-staging/*/*",
          "arn:aws:eks:us-east-1:058114477594:addon/techx-staging/*/*",
          "arn:aws:eks:us-east-1:058114477594:access-entry/techx-staging/*"
        ] },
        { Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:StopBuild"], Resource = [
          local.cleanup_project_arn, "arn:aws:codebuild:us-east-1:058114477594:project/techx-automation-lifecycle"
        ] }
      ]
    }
    network = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = [
          "ec2:DeleteVpc", "ec2:DeleteSubnet", "ec2:DetachInternetGateway", "ec2:DeleteInternetGateway",
          "ec2:DeleteRouteTable", "ec2:DeleteRoute", "ec2:DisassociateRouteTable", "ec2:DeleteSecurityGroup",
          "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress", "ec2:DeleteVpcEndpoints", "ec2:DeleteLaunchTemplate"
          ], Resource = [for kind in ["vpc", "subnet", "internet-gateway", "route-table", "security-group", "security-group-rule", "vpc-endpoint", "launch-template"] : "arn:aws:ec2:us-east-1:058114477594:${kind}/*"],
        Condition = { StringEquals = { "ec2:ResourceTag/Project" = "techx", "ec2:ResourceTag/Environment" = "staging" } } },
        { Effect = "Allow", Action = ["ec2:TerminateInstances"], Resource = "arn:aws:ec2:us-east-1:058114477594:instance/*",
        Condition = { StringEquals = { "ec2:ResourceTag/Project" = "techx", "ec2:ResourceTag/Environment" = "staging", "ec2:ResourceTag/Role" = ["private-probe", "deployment-executor"] } } },
        { Effect   = "Allow", Action = ["ec2:DeleteClientVpnEndpoint", "ec2:DisassociateClientVpnTargetNetwork", "ec2:RevokeClientVpnIngress"],
          Resource = "arn:aws:ec2:us-east-1:058114477594:client-vpn-endpoint/*",
        Condition = { StringEquals = { "ec2:ResourceTag/Project" = "techx", "ec2:ResourceTag/Environment" = "staging" } } }
      ]
    }
    services = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution", "cloudfront:ListTagsForResource", "cloudfront:GetVpcOrigin", "cloudfront:DeleteVpcOrigin", "cloudfront:DescribeFunction", "cloudfront:GetFunction", "cloudfront:DeleteFunction"],
        Resource = ["arn:aws:cloudfront::058114477594:distribution/*", "arn:aws:cloudfront::058114477594:vpcorigin/*", "arn:aws:cloudfront::058114477594:function/techx-staging-*"] },
        { Effect = "Allow", Action = ["route53:GetHostedZone", "route53:ListResourceRecordSets", "route53:ListTagsForResource", "route53:ChangeResourceRecordSets", "route53:DeleteHostedZone"], Resource = "arn:aws:route53:::hostedzone/*" },
        { Effect = "Allow", Action = ["route53:GetChange"], Resource = "arn:aws:route53:::change/*" },
        { Effect = "Allow", Action = ["acm:DescribeCertificate", "acm:ListTagsForCertificate", "acm:DeleteCertificate"], Resource = "arn:aws:acm:us-east-1:058114477594:certificate/*" },
        { Effect = "Allow", Action = ["elasticloadbalancing:Describe*"], Resource = "*" },
        { Effect = "Allow", Action = ["dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeContributorInsights", "dynamodb:DescribeKinesisStreamingDestination", "dynamodb:ListTagsOfResource", "dynamodb:DeleteTable"], Resource = "arn:aws:dynamodb:us-east-1:058114477594:table/techx-staging-orders" },
        { Effect = "Allow", Action = ["ecr:DescribeRepositories", "ecr:ListTagsForResource", "ecr:GetLifecyclePolicy", "ecr:DeleteLifecyclePolicy", "ecr:DeleteRepository"], Resource = [for name in ["frontend", "catalog", "order"] : "arn:aws:ecr:us-east-1:058114477594:repository/techx/${name}"] },
        { Effect = "Allow", Action = ["logs:ListTagsForResource", "logs:ListTagsLogGroup", "logs:DescribeLogStreams", "logs:DeleteLogStream", "logs:DeleteLogGroup"], Resource = ["arn:aws:logs:us-east-1:058114477594:log-group:/aws/eks/techx-staging/*", "arn:aws:logs:us-east-1:058114477594:log-group:/aws/client-vpn/techx-staging*"] },
        { Effect = "Allow", Action = ["budgets:ViewBudget", "budgets:ModifyBudget"], Resource = "arn:aws:budgets::058114477594:budget/techx-staging-hard-cap" }
      ]
    }
    identity = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy", "iam:DeleteRolePolicy", "iam:DetachRolePolicy", "iam:DeleteRole"], Resource = "arn:aws:iam::058114477594:role/techx-staging-*" },
        { Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions", "iam:DeletePolicyVersion", "iam:DeletePolicy"], Resource = "arn:aws:iam::058114477594:policy/techx-staging-*" },
        { Effect = "Allow", Action = ["iam:GetOpenIDConnectProvider", "iam:ListOpenIDConnectProviderTags", "iam:DeleteOpenIDConnectProvider"], Resource = "arn:aws:iam::058114477594:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/*",
        Condition = { StringEquals = { "aws:ResourceTag/Project" = "techx", "aws:ResourceTag/Environment" = "staging" } } }
      ]
    }
  }
}

resource "aws_iam_role" "cleanup_executor" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "techx-automation-cleanup-executor"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRole", Principal = { Service = "codebuild.amazonaws.com" },
    Condition = { StringEquals = { "aws:SourceAccount" = "058114477594" }, ArnEquals = { "aws:SourceArn" = local.cleanup_project_arn } }
  }] })
}

resource "aws_iam_policy" "cleanup_executor" {
  for_each = { for name, policy in local.cleanup_policies : name => jsonencode(policy) if var.enable_cleanup_control }
  name     = "techx-automation-cleanup-${each.key}"
  policy   = each.value
}

resource "aws_iam_role_policy_attachment" "cleanup_executor" {
  for_each   = { for name, policy in local.cleanup_policies : name => jsonencode(policy) if var.enable_cleanup_control }
  role       = aws_iam_role.cleanup_executor[0].name
  policy_arn = aws_iam_policy.cleanup_executor[each.key].arn
}

resource "aws_cloudwatch_log_group" "cleanup_executor" {
  count             = var.enable_cleanup_control ? 1 : 0
  name              = "/aws/codebuild/techx-automation-cleanup"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.control.arn
}

resource "aws_iam_role_policy" "cleanup_executor_logs" {
  count = var.enable_cleanup_control ? 1 : 0
  name  = "write-cleanup-build-logs"
  role  = aws_iam_role.cleanup_executor[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.cleanup_executor[0].arn}:*"
  }] })
}

resource "aws_codebuild_project" "cleanup" {
  count                  = var.enable_cleanup_control ? 1 : 0
  name                   = local.cleanup_project_name
  service_role           = aws_iam_role.cleanup_executor[0].arn
  encryption_key         = aws_kms_key.control.arn
  build_timeout          = 180
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
    environment_variable {
      name  = "TECHX_STATE_BUCKET"
      value = aws_s3_bucket.control.id
    }
    environment_variable {
      name  = "TECHX_STATE_KMS_KEY_ARN"
      value = aws_kms_key.control.arn
    }
    environment_variable {
      name  = "CLOUDFLARE_SECRET_ARN"
      value = var.cloudflare_secret_arn
    }
  }
  source {
    type = "NO_SOURCE"
    buildspec = jsonencode({ version = 0.2, phases = {
      install = {
        runtime-versions = { python = "3.13" }
        commands         = ["python -m pip install boto3==1.43.7 dnspython==2.8.0", "pwsh -NoProfile -Command '$PSVersionTable.PSVersion.ToString()'"]
      }
      build = { commands = [
        "cat > /tmp/automation_bundle.py <<'PY'\n${file("${path.module}/../../scripts/automation_bundle.py")}\nPY",
        "cat > /tmp/cleanup_launcher.py <<'PY'\n${file("${path.module}/cleanup_launcher.py")}\nPY",
        "cat > /tmp/cleanup_loader.py <<'PY'\n${file("${path.module}/cleanup_loader.py")}\nPY",
        "python /tmp/cleanup_loader.py"
      ] }
    } })
  }
  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.cleanup_executor[0].name
      status     = "ENABLED"
    }
  }
  depends_on = [aws_iam_role_policy_attachment.cleanup_executor, aws_iam_role_policy.cleanup_executor_logs]
}

output "cleanup_executor_role_arn" { value = var.enable_cleanup_control ? aws_iam_role.cleanup_executor[0].arn : null }
