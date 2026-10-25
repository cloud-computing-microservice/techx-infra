locals {
  simulation_account_prefix = "arn:aws:iam::058114477594"
  simulation_tag_context = [for key, value in { Project = "techx", Environment = "staging", Role = "deployment-executor" } : {
    ContextKeyName = "aws:RequestTag/${key}", ContextKeyType = "string", ContextKeyValues = [value]
  }]
}

# These are concrete resource/context API simulations, not proof of service
# integration. Live IAM calls are made only by the separately invoked preflight.
output "lifecycle_iam_simulation_cases_json" {
  value = var.enable_lifecycle_control ? jsonencode({
    manager = {
      principalArn = "${local.simulation_account_prefix}:role/techx-automation-lifecycle-manager"
      cases = [
        { name = "fixed-template-launch", action = "ec2:RunInstances", expected = "allowed"
          resources = ["arn:aws:ec2:us-east-1::image/${var.executor_ami_id}", aws_subnet.executor_control[0].arn, aws_security_group.executor_control[0].arn,
          "arn:aws:ec2:us-east-1:058114477594:instance/i-00000000000000000", "arn:aws:ec2:us-east-1:058114477594:volume/vol-00000000000000000", "arn:aws:ec2:us-east-1:058114477594:network-interface/eni-00000000000000000"]
          context = concat(local.simulation_tag_context, [
            { ContextKeyName = "ec2:LaunchTemplate", ContextKeyType = "string", ContextKeyValues = [aws_launch_template.executor[0].arn] },
            { ContextKeyName = "ec2:IsLaunchTemplateResource", ContextKeyType = "boolean", ContextKeyValues = ["true"] },
            { ContextKeyName = "ec2:InstanceType", ContextKeyType = "string", ContextKeyValues = ["t3.small"] },
            { ContextKeyName = "ec2:Subnet", ContextKeyType = "string", ContextKeyValues = [aws_subnet.executor_control[0].arn] }
          ])
        },
        { name = "no-foreign-pass-role", action = "iam:PassRole", expected = "denied", resources = ["${local.simulation_account_prefix}:role/techx-staging-node"],
        context = [{ ContextKeyName = "iam:PassedToService", ContextKeyType = "string", ContextKeyValues = ["ec2.amazonaws.com"] }] },
        { name = "no-cluster-create", action = "eks:CreateCluster", expected = "denied", resources = ["arn:aws:eks:us-east-1:058114477594:cluster/techx-staging"] },
        { name = "no-approval-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/approvals/staging/simulation/approval.json"] }
      ]
    }
    executor = {
      principalArn = local.deployment_role_arn
      cases = [
        { name = "bounded-workload-role", action = "iam:CreateRole", expected = "allowed", resources = [local.workload_role_arns[0]], context = concat(local.simulation_tag_context, [
          { ContextKeyName = "iam:PermissionsBoundary", ContextKeyType = "string", ContextKeyValues = [local.workload_boundary_arn] }
        ]) },
        { name = "wrong-boundary-denied", action = "iam:CreateRole", expected = "denied", resources = [local.workload_role_arns[0]], context = concat(local.simulation_tag_context, [
          { ContextKeyName = "iam:PermissionsBoundary", ContextKeyType = "string", ContextKeyValues = ["${local.simulation_account_prefix}:policy/unapproved-boundary"] }
        ]) },
        { name = "no-self-policy", action = "iam:PutRolePolicy", expected = "denied", resources = [local.deployment_role_arn] },
        { name = "state-write", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/staging/terraform.tfstate"] },
        { name = "no-approval-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/approvals/staging/simulation/approval.json"] }
      ]
    }
    dispatcher = {
      principalArn = "${local.simulation_account_prefix}:role/techx-automation-lifecycle-dispatcher"
      cases = [
        { name = "fixed-manager-build", action = "codebuild:StartBuild", expected = "allowed", resources = [local.lifecycle_project_arn] },
        { name = "read-admission-encryption", action = "s3:GetEncryptionConfiguration", expected = "allowed", resources = [aws_s3_bucket.control.arn] },
        { name = "read-admission-kms", action = "kms:DescribeKey", expected = "allowed", resources = [aws_kms_key.control.arn] },
        { name = "read-orphan-origins", action = "cloudfront:ListVpcOrigins", expected = "allowed", resources = ["*"] },
        { name = "protected-handoff-write", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/handoffs/staging/simulation/intent.json"] },
        { name = "no-other-build", action = "codebuild:StartBuild", expected = "denied", resources = ["arn:aws:codebuild:us-east-1:058114477594:project/unrelated-project"] },
        { name = "no-ec2-create", action = "ec2:RunInstances", expected = "denied", resources = ["arn:aws:ec2:us-east-1:058114477594:instance/i-00000000000000000"] }
      ]
    }
  }) : null
}

resource "aws_iam_role_policy" "lifecycle_simulation" {
  count = var.enable_lifecycle_control ? 1 : 0
  name  = "simulate-only-reviewed-lifecycle-roles"
  role  = aws_iam_role.lifecycle_dispatcher[0].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["iam:SimulatePrincipalPolicy"], Resource = [
      "${local.simulation_account_prefix}:role/techx-automation-lifecycle-manager", local.deployment_role_arn,
      "${local.simulation_account_prefix}:role/techx-automation-lifecycle-dispatcher"
    ]
  }] })
}
