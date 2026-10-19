locals {
  origin_id         = "internal-alb"
  block_argocd_code = <<-EOF
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri === '/argocd' || uri.indexOf('/argocd/') === 0) {
    return {
      statusCode: 403,
      statusDescription: 'Forbidden',
      headers: { 'content-type': { value: 'text/plain; charset=utf-8' } }
    };
  }
  return request;
}
EOF
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  count = var.enabled ? 1 : 0
  name  = "Managed-CachingDisabled"
}

# Next.js fingerprints these filenames. HTML, API responses and mutable public
# images continue through the uncached default behavior.
data "aws_cloudfront_cache_policy" "static_assets" {
  count = var.enabled ? 1 : 0
  name  = "Managed-CachingOptimized"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_and_cloudfront_headers" {
  count = var.enabled ? 1 : 0
  name  = "Managed-AllViewerAndCloudFrontHeaders-2022-06"
}

data "aws_cloudfront_response_headers_policy" "security_headers" {
  count = var.enabled ? 1 : 0
  name  = "Managed-SecurityHeadersPolicy"
}

resource "aws_cloudfront_vpc_origin" "this" {
  count = var.enabled ? 1 : 0

  vpc_origin_endpoint_config {
    name                   = "${var.name}-internal-alb"
    arn                    = var.origin_alb_arn
    http_port              = 80
    https_port             = 443
    origin_protocol_policy = "http-only"

    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  tags = merge(var.tags, { Name = "${var.name}-vpc-origin" })
}

resource "aws_cloudfront_function" "block_argocd" {
  count = var.enabled ? 1 : 0

  name    = "${var.name}-block-argocd"
  runtime = "cloudfront-js-2.0"
  comment = "Keep the Argo CD path private"
  publish = true
  code    = local.block_argocd_code
}

resource "aws_cloudfront_distribution" "this" {
  count = var.enabled ? 1 : 0

  enabled             = true
  is_ipv6_enabled     = true
  wait_for_deployment = true
  comment             = "${var.name} storefront"
  aliases             = [var.domain_name]
  price_class         = "PriceClass_100"
  http_version        = "http2and3"

  origin {
    domain_name = var.origin_domain_name
    origin_id   = local.origin_id

    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.this[0].id
      origin_read_timeout      = 30
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = local.origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled[0].id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer_and_cloudfront_headers[0].id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers[0].id

    function_association {
      event_type   = "viewer-request"
      function_arn = aws_cloudfront_function.block_argocd[0].arn
    }
  }

  ordered_cache_behavior {
    path_pattern               = "/_next/static/*"
    target_origin_id           = local.origin_id
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.static_assets[0].id
    response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security_headers[0].id
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    acm_certificate_arn      = var.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = merge(var.tags, { Name = "${var.name}-storefront" })

  lifecycle {
    precondition {
      condition = !var.enabled || (
        var.domain_name != "" &&
        var.certificate_arn != "" &&
        var.origin_alb_arn != "" &&
        var.origin_domain_name != ""
      )
      error_message = "Domain, ACM certificate and internal ALB inputs are required when CloudFront is enabled."
    }
  }
}
