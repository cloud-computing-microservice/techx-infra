terraform {
  required_version = "= 1.15.5"
  required_providers {
    aws  = { source = "hashicorp/aws", version = "= 6.55.0" }
    helm = { source = "hashicorp/helm", version = "= 3.2.0" }
    http = { source = "hashicorp/http", version = "= 3.6.0" }
    tls  = { source = "hashicorp/tls", version = "= 4.3.0" }
  }
}
