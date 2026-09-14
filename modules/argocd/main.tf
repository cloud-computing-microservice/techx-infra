resource "helm_release" "this" {
  name             = "argocd"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.chart_version
  namespace        = "argocd"
  create_namespace = true
  atomic           = true
  wait             = true
  timeout          = 600

  values = [yamlencode({
    configs = {
      params = merge(
        { "server.insecure" = true },
        var.private_ingress_enabled ? {
          "server.rootpath" = var.server_rootpath
          "server.basehref" = var.server_rootpath
        } : {}
      )
      cm = var.private_ingress_enabled ? { url = var.server_url } : {}
    }
    controller = {
      replicas  = 1
      resources = { requests = { cpu = "50m", memory = "128Mi" }, limits = { cpu = "250m", memory = "384Mi" } }
    }
    server = {
      replicas  = 1
      service   = { type = "ClusterIP" }
      resources = { requests = { cpu = "25m", memory = "64Mi" }, limits = { cpu = "200m", memory = "256Mi" } }
      ingress = {
        enabled          = var.private_ingress_enabled
        controller       = "generic"
        ingressClassName = "alb"
        hostname         = var.hostname
        path             = var.server_rootpath
        pathType         = "Prefix"
        tls              = false
        annotations = var.private_ingress_enabled ? {
          "alb.ingress.kubernetes.io/scheme"                              = "internal"
          "alb.ingress.kubernetes.io/target-type"                         = "ip"
          "alb.ingress.kubernetes.io/ip-address-type"                     = "ipv4"
          "alb.ingress.kubernetes.io/listen-ports"                        = "[{\"HTTP\":80},{\"HTTPS\":443}]"
          "alb.ingress.kubernetes.io/certificate-arn"                     = var.certificate_arn
          "alb.ingress.kubernetes.io/security-groups"                     = var.alb_security_group_id
          "alb.ingress.kubernetes.io/manage-backend-security-group-rules" = "true"
          "alb.ingress.kubernetes.io/subnets"                             = join(",", var.private_subnet_ids)
          "alb.ingress.kubernetes.io/group.name"                          = var.ingress_group_name
          "alb.ingress.kubernetes.io/group.order"                         = "10"
          "alb.ingress.kubernetes.io/healthcheck-path"                    = "${var.server_rootpath}/healthz"
          "alb.ingress.kubernetes.io/success-codes"                       = "200"
          "alb.ingress.kubernetes.io/load-balancer-attributes"            = "deletion_protection.enabled=false"
          "alb.ingress.kubernetes.io/tags"                                = "Project=techx,Environment=${var.environment},ManagedBy=kubernetes"
        } : {}
      }
    }
    repoServer = {
      replicas  = 1
      resources = { requests = { cpu = "25m", memory = "128Mi" }, limits = { cpu = "250m", memory = "384Mi" } }
    }
    applicationSet = {
      replicas  = 1
      resources = { requests = { cpu = "25m", memory = "64Mi" }, limits = { cpu = "150m", memory = "192Mi" } }
    }
    dex           = { enabled = false }
    notifications = { enabled = false }
    redis = {
      resources = { requests = { cpu = "25m", memory = "32Mi" }, limits = { cpu = "100m", memory = "128Mi" } }
    }
  })]

  lifecycle {
    precondition {
      condition = !var.private_ingress_enabled || (
        var.server_url != "" &&
        var.hostname != "" &&
        var.certificate_arn != "" &&
        var.alb_security_group_id != "" &&
        length(var.private_subnet_ids) == 2
      )
      error_message = "Argo CD private ingress requires URL, hostname, certificate, ALB security group and two private subnets."
    }
  }
}
