variable "chart_version" { type = string }

variable "environment" {
  type = string
  validation {
    condition     = contains(["demo", "staging"], var.environment)
    error_message = "environment must be demo or staging."
  }
}

variable "private_ingress_enabled" {
  type    = bool
  default = false
}

variable "server_rootpath" {
  type    = string
  default = "/argocd"
}

variable "server_url" {
  type    = string
  default = ""
}

variable "hostname" {
  type    = string
  default = ""
}

variable "certificate_arn" {
  type      = string
  default   = ""
  sensitive = true
}

variable "alb_security_group_id" {
  type    = string
  default = ""
}

variable "private_subnet_ids" {
  type    = list(string)
  default = []
}

variable "ingress_group_name" {
  type    = string
  default = "techx-private"
}
