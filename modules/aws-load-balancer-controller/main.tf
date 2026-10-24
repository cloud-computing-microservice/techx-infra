data "http" "iam_policy" {
  url = var.iam_policy_url
  lifecycle {
    postcondition {
      condition     = sha256(self.response_body) == var.iam_policy_sha256
      error_message = "AWS Load Balancer Controller IAM policy checksum does not match the pinned release."
    }
  }
}

resource "terraform_data" "cluster_ready" {
  input = var.cluster_ready_id
}

locals { oidc_host = replace(var.oidc_provider_url, "https://", "") }

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_policy" "this" {
  name   = "${var.cluster_name}-aws-load-balancer-controller"
  policy = data.http.iam_policy.response_body
  tags   = var.tags
}

resource "aws_iam_role" "this" {
  permissions_boundary = var.iam_permissions_boundary_arn
  name                 = "${var.cluster_name}-aws-load-balancer-controller"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  tags                 = var.tags
}

resource "aws_iam_role_policy_attachment" "this" {
  role       = aws_iam_role.this.name
  policy_arn = aws_iam_policy.this.arn
}

resource "helm_release" "this" {
  name             = "aws-load-balancer-controller"
  repository       = "https://aws.github.io/eks-charts"
  chart            = "aws-load-balancer-controller"
  version          = var.chart_version
  namespace        = "kube-system"
  create_namespace = false
  atomic           = true
  wait             = true
  timeout          = 600

  values = [yamlencode({
    clusterName  = var.cluster_name
    region       = var.region
    vpcId        = var.vpc_id
    replicaCount = 1
    serviceAccount = {
      create = true
      name   = "aws-load-balancer-controller"
      annotations = {
        "eks.amazonaws.com/role-arn" = aws_iam_role.this.arn
      }
    }
    resources = {
      requests = { cpu = "50m", memory = "96Mi" }
      limits   = { cpu = "200m", memory = "256Mi" }
    }
  })]

  depends_on = [aws_iam_role_policy_attachment.this, terraform_data.cluster_ready]
}
