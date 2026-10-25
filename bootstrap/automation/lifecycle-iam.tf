locals {
  deployment_role_arn    = "arn:aws:iam::058114477594:role/techx-automation-lifecycle-executor"
  deployment_profile_arn = "arn:aws:iam::058114477594:instance-profile/techx-automation-lifecycle-executor"
  lifecycle_manager_policies = var.enable_lifecycle_control ? {
    storage = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.control.arn },
        { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["executions/*", "bundles/*", "private/staging/*/inputs.json", "automation/staging/session.json", "control/staging/tasks/*", "control/staging/executor-config.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
        { Effect = "Allow", Action = ["s3:PutObject"], Resource = [for prefix in ["private/staging/*/inputs.json", "automation/staging/session.json", "control/staging/tasks/*"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
        { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn, Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } },
        { Effect = "Allow", Action = ["scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
        { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher" },
        { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.lifecycle[0].arn}:*" }
      ]
    }
    compute = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeSubnets", "ec2:DescribeVpcs", "ec2:DescribeSecurityGroups", "ec2:DescribeRouteTables"], Resource = "*" },
        { Effect = "Allow", Action = ["iam:PassRole"], Resource = local.deployment_role_arn, Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
        { Effect = "Allow", Action = ["ec2:RunInstances"], Resource = [
          "arn:aws:ec2:us-east-1::image/${var.executor_ami_id}",
          aws_subnet.executor_control[0].arn, aws_security_group.executor_control[0].arn
          ], Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.executor[0].arn }, Bool = { "ec2:IsLaunchTemplateResource" = "true" } }
        },
        { Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "arn:aws:ec2:us-east-1:058114477594:instance/*",
          Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.executor[0].arn }, StringEquals = {
            "ec2:InstanceType" = "t3.small", "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging", "aws:RequestTag/Role" = "deployment-executor"
        } } },
        { Effect = "Allow", Action = ["ec2:RunInstances"], Resource = "arn:aws:ec2:us-east-1:058114477594:volume/*",
          Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.executor[0].arn }, StringEquals = {
            "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging", "aws:RequestTag/Role" = "deployment-executor"
        } } },
        { Effect    = "Allow", Action = ["ec2:RunInstances"], Resource = "arn:aws:ec2:us-east-1:058114477594:network-interface/*",
          Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.executor[0].arn, "ec2:Subnet" = aws_subnet.executor_control[0].arn } }
        },
        { Effect = "Allow", Action = ["ec2:CreateTags"], Resource = [for kind in ["instance", "volume"] : "arn:aws:ec2:us-east-1:058114477594:${kind}/*"], Condition = {
          StringEquals = { "ec2:CreateAction" = "RunInstances", "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging", "aws:RequestTag/Role" = "deployment-executor" }
        } }
      ]
    }
    remote = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["ssm:DescribeDocument", "ssm:SendCommand"], Resource = "arn:aws:ssm:us-east-1:058114477594:document/techx-automation-deployment-task" },
        { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = "arn:aws:ec2:us-east-1:058114477594:instance/*",
        Condition = { StringEquals = { "ssm:resourceTag/Project" = "techx", "ssm:resourceTag/Environment" = "staging", "ssm:resourceTag/Role" = "deployment-executor" } } },
        { Effect = "Allow", Action = ["ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"], Resource = "*" }
      ]
    }
  } : null
}

resource "aws_iam_policy" "lifecycle_manager" {
  for_each = var.enable_lifecycle_control ? toset(["storage", "compute", "remote"]) : toset([])
  name     = "techx-automation-lifecycle-manager-${each.key}"
  policy   = jsonencode(local.lifecycle_manager_policies[each.key])
}

resource "aws_iam_role_policy_attachment" "lifecycle_manager" {
  for_each   = aws_iam_policy.lifecycle_manager
  role       = aws_iam_role.lifecycle_manager[0].name
  policy_arn = each.value.arn
}

resource "aws_iam_role" "deployment_executor" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-lifecycle-executor"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_instance_profile" "deployment_executor" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-lifecycle-executor"
  role  = aws_iam_role.deployment_executor[0].name
}

resource "aws_iam_role_policy_attachment" "deployment_ssm" {
  count      = var.enable_lifecycle_control ? 1 : 0
  role       = aws_iam_role.deployment_executor[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

locals {
  workload_role_names   = ["techx-staging-cluster", "techx-staging-node", "techx-staging-aws-load-balancer-controller", "techx-staging-order-api"]
  workload_role_arns    = [for name in local.workload_role_names : "arn:aws:iam::058114477594:role/${name}"]
  workload_policy_arn   = "arn:aws:iam::058114477594:policy/techx-staging-aws-load-balancer-controller"
  workload_boundary_arn = "arn:aws:iam::058114477594:policy/techx-automation-workload-boundary"
  deployment_executor_policies = var.enable_lifecycle_control ? {
    state = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = aws_s3_bucket.control.arn },
        { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["executions/*", "bundles/*", "private/staging/*", "approvals/staging/*", "releases/staging/*/bootstrap.json", "chart-selections/staging/*/bootstrap.json", "control/staging/tasks/*", "automation/staging/session.json", "staging/terraform.tfstate", "staging/terraform.tfstate.tflock"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
        { Effect = "Allow", Action = ["s3:PutObject"], Resource = [for prefix in ["private/staging/*", "automation/staging/session.json", "staging/terraform.tfstate", "staging/terraform.tfstate.tflock", "evidence/staging/*"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
        { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${aws_s3_bucket.control.arn}/staging/terraform.tfstate.tflock" },
        { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn, Condition = { StringEquals = { "kms:ViaService" = ["s3.us-east-1.amazonaws.com", "secretsmanager.us-east-1.amazonaws.com"] } } },
        { Effect = "Allow", Action = ["scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
        { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = var.cloudflare_secret_arn },
        { Effect = "Allow", Action = ["secretsmanager:CreateSecret", "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret", "secretsmanager:TagResource"], Resource = "arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/staging/*" }
      ]
    }
    read = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["ec2:Describe*", "eks:ListClusters", "iam:ListRoles", "iam:ListPolicies", "tag:GetResources", "cloudfront:ListDistributions", "cloudfront:ListVpcOrigins", "route53:ListHostedZones", "route53:ListHostedZonesByName", "acm:ListCertificates", "logs:DescribeLogGroups"], Resource = "*" },
        { Effect = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"], Resource = concat(local.workload_role_arns, [local.deployment_role_arn]) },
        { Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion", "iam:ListPolicyVersions"], Resource = [local.workload_policy_arn, local.workload_boundary_arn, "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy", "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy", "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy", "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"] }
      ]
    }
    cluster = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["eks:CreateCluster", "eks:DescribeCluster", "eks:DescribeUpdate", "eks:UpdateClusterConfig", "eks:TagResource", "eks:ListTagsForResource", "eks:CreateNodegroup", "eks:DescribeNodegroup", "eks:ListNodegroups", "eks:CreateAddon", "eks:DescribeAddon", "eks:ListAddons", "eks:CreateAccessEntry", "eks:DescribeAccessEntry", "eks:ListAccessEntries", "eks:AssociateAccessPolicy", "eks:ListAssociatedAccessPolicies"], Resource = ["arn:aws:eks:us-east-1:058114477594:cluster/techx-staging", "arn:aws:eks:us-east-1:058114477594:nodegroup/techx-staging/*/*", "arn:aws:eks:us-east-1:058114477594:addon/techx-staging/*/*", "arn:aws:eks:us-east-1:058114477594:access-entry/techx-staging/*"] },
        { Effect = "Allow", Action = ["iam:PassRole"], Resource = [local.workload_role_arns[0], local.workload_role_arns[1]], Condition = { StringEquals = { "iam:PassedToService" = ["eks.amazonaws.com", "ec2.amazonaws.com"] } } },
        { Effect = "Allow", Action = ["iam:CreateServiceLinkedRole"], Resource = "arn:aws:iam::058114477594:role/aws-service-role/*", Condition = { StringEquals = { "iam:AWSServiceName" = ["eks.amazonaws.com", "eks-nodegroup.amazonaws.com", "elasticloadbalancing.amazonaws.com", "clientvpn.amazonaws.com", "vpcorigin.cloudfront.amazonaws.com"] } } }
      ]
    }
    identity = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["iam:CreateRole"], Resource = local.workload_role_arns, Condition = { StringEquals = { "iam:PermissionsBoundary" = local.workload_boundary_arn, "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging" } } },
        { Effect = "Allow", Action = ["iam:TagRole", "iam:UpdateAssumeRolePolicy"], Resource = local.workload_role_arns, Condition = { StringEquals = { "aws:ResourceTag/Project" = "techx", "aws:ResourceTag/Environment" = "staging" } } },
        { Effect = "Allow", Action = ["iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:TagPolicy"], Resource = local.workload_policy_arn },
        { Effect = "Allow", Action = ["iam:AttachRolePolicy"], Resource = local.workload_role_arns[0], Condition = { ArnEquals = { "iam:PolicyARN" = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy" } } },
        { Effect = "Allow", Action = ["iam:AttachRolePolicy"], Resource = local.workload_role_arns[1], Condition = { ArnEquals = { "iam:PolicyARN" = ["arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy", "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy", "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"] } } },
        { Effect = "Allow", Action = ["iam:AttachRolePolicy"], Resource = local.workload_role_arns[2], Condition = { ArnEquals = { "iam:PolicyARN" = local.workload_policy_arn } } },
        { Effect = "Allow", Action = ["iam:PutRolePolicy"], Resource = local.workload_role_arns[3] },
        { Effect = "Allow", Action = ["iam:CreateOpenIDConnectProvider", "iam:GetOpenIDConnectProvider", "iam:ListOpenIDConnectProviderTags", "iam:TagOpenIDConnectProvider"], Resource = "arn:aws:iam::058114477594:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/*" }
      ]
    }
    network = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateInternetGateway", "ec2:CreateRouteTable", "ec2:CreateSecurityGroup", "ec2:CreateLaunchTemplate", "ec2:CreateVpcEndpoint"], Resource = [for kind in ["vpc", "subnet", "internet-gateway", "route-table", "security-group", "launch-template", "vpc-endpoint"] : "arn:aws:ec2:us-east-1:058114477594:${kind}/*"], Condition = { StringEquals = { "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging" } } },
        { Effect = "Allow", Action = ["ec2:CreateTags"], Resource = [for kind in ["vpc", "subnet", "internet-gateway", "route-table", "security-group", "security-group-rule", "launch-template", "vpc-endpoint"] : "arn:aws:ec2:us-east-1:058114477594:${kind}/*"], Condition = { StringEquals = { "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging" } } },
        { Effect = "Allow", Action = ["ec2:ModifyVpcAttribute", "ec2:ModifySubnetAttribute", "ec2:AttachInternetGateway", "ec2:CreateRoute", "ec2:AssociateRouteTable", "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupEgress", "ec2:ModifyLaunchTemplate", "ec2:CreateLaunchTemplateVersion"], Resource = [for kind in ["vpc", "subnet", "internet-gateway", "route-table", "security-group", "security-group-rule", "launch-template"] : "arn:aws:ec2:us-east-1:058114477594:${kind}/*"], Condition = { StringEquals = { "ec2:ResourceTag/Project" = "techx", "ec2:ResourceTag/Environment" = "staging" } } },
        { Effect = "Allow", Action = ["ec2:CreateClientVpnEndpoint", "ec2:DescribeClientVpnEndpoints", "ec2:AssociateClientVpnTargetNetwork", "ec2:DescribeClientVpnTargetNetworks", "ec2:AuthorizeClientVpnIngress", "ec2:DescribeClientVpnAuthorizationRules"], Resource = ["arn:aws:ec2:us-east-1:058114477594:client-vpn-endpoint/*", "arn:aws:ec2:us-east-1:058114477594:subnet/*"] }
      ]
    }
    services = {
      Version = "2012-10-17", Statement = [
        { Effect = "Allow", Action = ["ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:ListTagsForResource", "ecr:TagResource", "ecr:PutLifecyclePolicy", "ecr:GetLifecyclePolicy"], Resource = [for name in ["frontend", "catalog", "order"] : "arn:aws:ecr:us-east-1:058114477594:repository/techx/${name}"] },
        { Effect = "Allow", Action = ["dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:DescribeTimeToLive", "dynamodb:UpdateTimeToLive", "dynamodb:DescribeContinuousBackups", "dynamodb:DescribeContributorInsights", "dynamodb:DescribeKinesisStreamingDestination", "dynamodb:ListTagsOfResource", "dynamodb:TagResource"], Resource = "arn:aws:dynamodb:us-east-1:058114477594:table/techx-staging-orders" },
        { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:DescribeLogStreams", "logs:PutRetentionPolicy", "logs:ListTagsForResource", "logs:ListTagsLogGroup", "logs:TagResource", "logs:TagLogGroup"], Resource = ["arn:aws:logs:us-east-1:058114477594:log-group:/aws/eks/techx-staging/*", "arn:aws:logs:us-east-1:058114477594:log-group:/aws/client-vpn/techx-staging*"] },
        { Effect = "Allow", Action = ["budgets:ViewBudget", "budgets:ModifyBudget"], Resource = "arn:aws:budgets::058114477594:budget/techx-staging-hard-cap" },
        { Effect = "Allow", Action = ["elasticloadbalancing:Describe*"], Resource = "*" }
      ]
    }
    edge = {
      Version = "2012-10-17", Statement = [
        # RequestCertificate has no resource-level permission in the service
        # authorization table. Restrict its wildcard by exact request context.
        { Effect = "Allow", Action = ["acm:RequestCertificate"], Resource = "*", Condition = {
          StringEquals = { "aws:RequestedRegion" = "us-east-1", "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging",
          "aws:RequestTag/Role" = "storefront-public", "acm:ValidationMethod" = "DNS", "acm:KeyAlgorithm" = "RSA_2048", "acm:Export" = "DISABLED" }
          "ForAllValues:StringEquals" = { "acm:DomainNames" = ["shop.dinhminhkhoa.id.vn"] }
          Null                        = { "acm:DomainNames" = "false" }
        } },
        { Effect = "Allow", Action = ["acm:ImportCertificate"], Resource = "arn:aws:acm:us-east-1:058114477594:certificate/*", Condition = {
          StringEquals = { "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging", "aws:RequestTag/Role" = ["client-vpn-server", "client-vpn-reference"] }
        } },
        { Effect = "Allow", Action = ["acm:DescribeCertificate", "acm:GetCertificate", "acm:ListTagsForCertificate"], Resource = "arn:aws:acm:us-east-1:058114477594:certificate/*" },
        { Effect = "Allow", Action = ["acm:AddTagsToCertificate"], Resource = "arn:aws:acm:us-east-1:058114477594:certificate/*", Condition = {
          StringEquals = { "aws:RequestTag/Project" = "techx", "aws:RequestTag/Environment" = "staging", "aws:RequestTag/Role" = ["storefront-public", "client-vpn-server", "client-vpn-reference"] }
        } },
        { Effect = "Allow", Action = ["cloudfront:CreateDistribution", "cloudfront:GetDistribution", "cloudfront:GetDistributionConfig", "cloudfront:TagResource", "cloudfront:ListTagsForResource", "cloudfront:CreateVpcOrigin", "cloudfront:GetVpcOrigin", "cloudfront:CreateFunction", "cloudfront:DescribeFunction", "cloudfront:PublishFunction"], Resource = ["arn:aws:cloudfront::058114477594:distribution/*", "arn:aws:cloudfront::058114477594:vpcorigin/*", "arn:aws:cloudfront::058114477594:function/techx-staging-*"] },
        { Effect = "Allow", Action = ["cloudfront:GetCachePolicy", "cloudfront:ListCachePolicies", "cloudfront:GetOriginRequestPolicy", "cloudfront:ListOriginRequestPolicies", "cloudfront:GetResponseHeadersPolicy", "cloudfront:ListResponseHeadersPolicies"], Resource = "*" },
        { Effect = "Allow", Action = ["route53:CreateHostedZone", "route53:AssociateVPCWithHostedZone", "route53:GetHostedZone", "route53:ListResourceRecordSets", "route53:ListTagsForResource", "route53:ChangeTagsForResource", "route53:ChangeResourceRecordSets"], Resource = "arn:aws:route53:::hostedzone/*" },
        { Effect = "Allow", Action = ["route53:GetChange"], Resource = "arn:aws:route53:::change/*" }
      ]
    }
  } : null
}

resource "aws_iam_policy" "deployment_executor" {
  for_each = var.enable_lifecycle_control ? toset(["state", "read", "cluster", "identity", "network", "services", "edge"]) : toset([])
  name     = "techx-automation-lifecycle-executor-${each.key}"
  policy   = jsonencode(local.deployment_executor_policies[each.key])
}

resource "aws_iam_role_policy_attachment" "deployment_executor" {
  for_each   = aws_iam_policy.deployment_executor
  role       = aws_iam_role.deployment_executor[0].name
  policy_arn = each.value.arn
}

resource "aws_iam_policy" "workload_boundary" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-workload-boundary"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:Describe*", "elasticloadbalancing:Describe*", "ecr:GetAuthorizationToken", "cloudwatch:PutMetricData"], Resource = "*" },
    { Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:AttachNetworkInterface", "ec2:DetachNetworkInterface", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses", "ec2:AssignIpv6Addresses", "ec2:UnassignIpv6Addresses", "ec2:ModifyNetworkInterfaceAttribute", "ec2:CreateTags", "ec2:DeleteTags", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup", "ec2:AuthorizeSecurityGroupIngress", "ec2:RevokeSecurityGroupIngress"], Resource = [for kind in ["network-interface", "instance", "security-group", "subnet", "vpc"] : "arn:aws:ec2:us-east-1:058114477594:${kind}/*"] },
    { Effect = "Allow", Action = ["eks:DescribeCluster", "eks-auth:AssumeRoleForPodIdentity"], Resource = "arn:aws:eks:us-east-1:058114477594:cluster/techx-staging" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability", "ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:ListImages", "ecr:GetRepositoryPolicy", "ecr:GetLifecyclePolicy", "ecr:GetLifecyclePolicyPreview"], Resource = [for name in ["frontend", "catalog", "order"] : "arn:aws:ecr:us-east-1:058114477594:repository/techx/${name}"] },
    { Effect = "Allow", Action = ["elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:CreateListener", "elasticloadbalancing:CreateRule", "elasticloadbalancing:AddTags", "elasticloadbalancing:RemoveTags", "elasticloadbalancing:RegisterTargets", "elasticloadbalancing:DeregisterTargets", "elasticloadbalancing:ModifyLoadBalancerAttributes", "elasticloadbalancing:ModifyListener", "elasticloadbalancing:ModifyListenerAttributes", "elasticloadbalancing:ModifyRule", "elasticloadbalancing:ModifyTargetGroup", "elasticloadbalancing:ModifyTargetGroupAttributes", "elasticloadbalancing:SetSecurityGroups", "elasticloadbalancing:SetSubnets", "elasticloadbalancing:SetIpAddressType", "elasticloadbalancing:SetRulePriorities", "elasticloadbalancing:DeleteLoadBalancer", "elasticloadbalancing:DeleteTargetGroup", "elasticloadbalancing:DeleteListener", "elasticloadbalancing:DeleteRule", "elasticloadbalancing:AddListenerCertificates", "elasticloadbalancing:RemoveListenerCertificates"], Resource = "arn:aws:elasticloadbalancing:us-east-1:058114477594:*" },
    { Effect = "Allow", Action = ["acm:ListCertificates", "acm:DescribeCertificate", "iam:ListServerCertificates", "iam:GetServerCertificate", "cognito-idp:DescribeUserPool"], Resource = "*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem"], Resource = "arn:aws:dynamodb:us-east-1:058114477594:table/techx-staging-orders" },
    { Effect = "Allow", Action = ["iam:CreateServiceLinkedRole"], Resource = "arn:aws:iam::058114477594:role/aws-service-role/elasticloadbalancing.amazonaws.com/*", Condition = { StringEquals = { "iam:AWSServiceName" = "elasticloadbalancing.amazonaws.com" } } }
  ] })
}

resource "aws_iam_role" "lifecycle_dispatcher" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "techx-automation-lifecycle-dispatcher"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc_arn }
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:cloud-computing-microservice/techx-infra:environment:staging-lifecycle"
    } }
  }] })
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "lifecycle_dispatcher" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "reserve-session-arm-cleanup-dispatch-fixed-manager"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketVersioning"], Resource = aws_s3_bucket.control.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = [for prefix in ["automation/staging/session.json", "staging/terraform.tfstate", "staging/terraform.tfstate.tflock", "control/staging/executor-config.json", "executions/*", "bundles/*", "private/staging/*/plans/*/*/request.json", "approvals/staging/*", "releases/staging/*/bootstrap.json", "chart-selections/staging/*/bootstrap.json"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = [for prefix in ["automation/staging/session.json", "control/staging/current-execution.json", "executions/*", "bundles/*", "private/staging/*/inputs.json", "staging/terraform.tfstate", "staging/terraform.tfstate.tflock"] : "${aws_s3_bucket.control.arn}/${prefix}"] },
    { Effect = "Allow", Action = ["s3:DeleteObject"], Resource = "${aws_s3_bucket.control.arn}/staging/terraform.tfstate.tflock" },
    { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.control.arn, Condition = { StringEquals = { "kms:ViaService" = "s3.us-east-1.amazonaws.com" } } },
    { Effect = "Allow", Action = ["scheduler:CreateSchedule", "scheduler:GetSchedule"], Resource = "arn:aws:scheduler:us-east-1:058114477594:schedule/techx-staging/cleanup-*" },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = "arn:aws:iam::058114477594:role/techx-automation-scheduler", Condition = { StringEquals = { "iam:PassedToService" = "scheduler.amazonaws.com" } } },
    { Effect = "Allow", Action = ["codebuild:StartBuild"], Resource = local.lifecycle_project_arn },
    { Effect = "Allow", Action = ["codebuild:BatchGetProjects", "codebuild:BatchGetBuilds"], Resource = [local.lifecycle_project_arn, local.cleanup_project_arn] },
    { Effect = "Allow", Action = ["ssm:GetCommandInvocation"], Resource = "*" },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:us-east-1:058114477594:function:techx-automation-cleanup-launcher" }
  ] })
}

output "lifecycle_dispatcher_role_arn" { value = var.enable_lifecycle_control ? aws_iam_role.lifecycle_dispatcher[0].arn : null }
