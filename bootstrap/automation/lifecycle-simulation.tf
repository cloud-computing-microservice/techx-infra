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
        { name = "private-promotion-call-write", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/promotions/staging/simulation/receipt.json"] },
        { name = "write-private-acceptance", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/acceptance/receipt.json"] },
        { name = "owned-vpn-client-export", action = "ec2:ExportClientVpnClientConfiguration", expected = "allowed", resources = ["arn:aws:ec2:us-east-1:058114477594:client-vpn-endpoint/cvpn-endpoint-00000000000000000"],
          context = [for key, value in { Project = "techx", Environment = "staging" } : {
            ContextKeyName = "ec2:ResourceTag/${key}", ContextKeyType = "string", ContextKeyValues = [value]
        }] },
        { name = "owned-vpn-client-disconnect", action = "ec2:TerminateClientVpnConnections", expected = "allowed", resources = ["arn:aws:ec2:us-east-1:058114477594:client-vpn-endpoint/cvpn-endpoint-00000000000000000"],
          context = [for key, value in { Project = "techx", Environment = "staging" } : {
            ContextKeyName = "ec2:ResourceTag/${key}", ContextKeyType = "string", ContextKeyValues = [value]
        }] },
        { name = "no-foreign-vpn-client-disconnect", action = "ec2:TerminateClientVpnConnections", expected = "denied", resources = ["arn:aws:ec2:us-east-1:058114477594:client-vpn-endpoint/cvpn-endpoint-00000000000000000"],
          context = [for key, value in { Project = "unrelated", Environment = "staging" } : {
            ContextKeyName = "ec2:ResourceTag/${key}", ContextKeyType = "string", ContextKeyValues = [value]
        }] },
        { name = "no-promotion-approval-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/promotion-approvals/staging/simulation/approval.json"] },
        { name = "read-active-manifest", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/releases/staging/simulation/active/candidate/manifest.json"] },
        { name = "write-private-active-rollout", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/rollout.json"] },
        { name = "no-publish-active-candidate", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/releases/staging/simulation/active/candidate/manifest.json"] },
        { name = "read-fixed-promotion-app", action = "secretsmanager:GetSecretValue", expected = "allowed", resources = ["arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/promotion-app-Abc123"],
          context = [for key, value in { Project = "techx", Environment = "automation", Role = "github-app" } : {
            ContextKeyName = "aws:ResourceTag/${key}", ContextKeyType = "string", ContextKeyValues = [value]
        }] },
        { name = "no-other-app-key", action = "secretsmanager:GetSecretValue", expected = "denied", resources = ["arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/unrelated-app-Abc123"] },
        { name = "no-approval-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/approvals/staging/simulation/approval.json"] }
      ]
    }
    dispatcher = {
      principalArn = "${local.simulation_account_prefix}:role/techx-automation-lifecycle-dispatcher"
      cases = [
        { name = "fixed-manager-build", action = "codebuild:StartBuild", expected = "allowed", resources = [local.lifecycle_project_arn] },
        { name = "read-pinned-public-acceptance", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/acceptance/staging/simulation/public/dispatch/result.json"] },
        { name = "read-pinned-active-candidate", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/releases/staging/simulation/active/candidate/manifest.json"] },
        { name = "read-pinned-active-proposal", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/proposals/staging/simulation/active/candidate/proposal.json"] },
        { name = "read-pinned-active-rollout", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/rollout.json"] },
        { name = "read-pinned-active-merge", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/promotions/staging/simulation/approval/receipt.json"] },
        { name = "no-active-rollout-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/rollout.json"] },
        { name = "no-active-candidate-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/releases/staging/simulation/active/candidate/manifest.json"] },
        { name = "no-unversioned-active-candidate", action = "s3:GetObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/releases/staging/simulation/active/candidate/manifest.json"] },
        { name = "no-public-evidence-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/acceptance/staging/simulation/public/dispatch/result.json"] },
        { name = "read-pinned-activation-receipt", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/exposure/receipt.json"] },
        { name = "read-pinned-private-acceptance", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/acceptance/receipt.json"] },
        { name = "no-unversioned-private-acceptance", action = "s3:GetObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/acceptance/receipt.json"] },
        { name = "no-private-acceptance-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/acceptance/receipt.json"] },
        { name = "no-unversioned-activation-read", action = "s3:GetObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/exposure/receipt.json"] },
        { name = "no-dispatcher-activation-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/exposure/receipt.json"] },
        { name = "read-admission-encryption", action = "s3:GetEncryptionConfiguration", expected = "allowed", resources = [aws_s3_bucket.control.arn] },
        { name = "read-admission-kms", action = "kms:DescribeKey", expected = "allowed", resources = [aws_kms_key.control.arn] },
        { name = "read-orphan-origins", action = "cloudfront:ListVpcOrigins", expected = "allowed", resources = ["*"] },
        { name = "protected-handoff-write", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/handoffs/staging/simulation/intent.json"] },
        { name = "read-dispatched-promotion-grant", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/promotion-approvals/staging/simulation/approval.json"] },
        { name = "no-dispatcher-promotion-approval-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/promotion-approvals/staging/simulation/approval.json"] },
        { name = "no-other-build", action = "codebuild:StartBuild", expected = "denied", resources = ["arn:aws:codebuild:us-east-1:058114477594:project/unrelated-project"] },
        { name = "no-ec2-create", action = "ec2:RunInstances", expected = "denied", resources = ["arn:aws:ec2:us-east-1:058114477594:instance/i-00000000000000000"] }
      ]
    }
    public_acceptance = {
      principalArn = "${local.simulation_account_prefix}:role/techx-automation-public-acceptance"
      cases = [
        { name = "read-pinned-active-rollout", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/rollout.json"] },
        { name = "read-pinned-application-intent", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/application/intent.json"] },
        { name = "write-active-public-evidence", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/acceptance/staging/simulation/active/candidate/public/dispatch/result.json"] },
        { name = "no-active-private-task-log-read", action = "s3:GetObjectVersion", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/task.private.log"] },
        { name = "no-active-private-acceptance-read", action = "s3:GetObjectVersion", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/active/candidate/acceptance/task.json"] },
        { name = "read-pinned-exposure", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/exposure/receipt.json"] },
        { name = "write-public-evidence", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/acceptance/staging/simulation/public/dispatch/result.json"] },
        { name = "no-private-inputs", action = "s3:GetObjectVersion", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/inputs.json"] },
        { name = "no-control-secret", action = "secretsmanager:GetSecretValue", expected = "denied", resources = ["arn:aws:secretsmanager:us-east-1:058114477594:secret:techx/automation/promotion-app-Abc123"] },
        { name = "no-manager-build", action = "codebuild:StartBuild", expected = "denied", resources = [local.lifecycle_project_arn] }
      ]
    }
    publisher = {
      principalArn = "${local.simulation_account_prefix}:role/techx-automation-publisher"
      cases = [
        { name = "write-active-manifest", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/releases/staging/simulation/active/candidate/manifest.json"] },
        { name = "write-active-proposal", action = "s3:PutObject", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/proposals/staging/simulation/active/candidate/proposal.json"] },
        { name = "no-cold-proposal-write", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/proposals/staging/simulation/bootstrap.json"] },
        { name = "read-pinned-baseline", action = "s3:GetObjectVersion", expected = "allowed", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/acceptance/baseline.json"] },
        { name = "no-baseline-overwrite", action = "s3:PutObject", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/acceptance/baseline.json"] },
        { name = "no-private-inputs-read", action = "s3:GetObjectVersion", expected = "denied", resources = ["${aws_s3_bucket.control.arn}/private/staging/simulation/inputs.json"] },
        { name = "no-publisher-cluster-create", action = "eks:CreateCluster", expected = "denied", resources = ["arn:aws:eks:us-east-1:058114477594:cluster/techx-staging"] }
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
      "${local.simulation_account_prefix}:role/techx-automation-lifecycle-dispatcher",
      "${local.simulation_account_prefix}:role/techx-automation-public-acceptance",
      "${local.simulation_account_prefix}:role/techx-automation-publisher"
    ]
  }] })
}
