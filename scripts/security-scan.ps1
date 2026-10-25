$ErrorActionPreference = 'Stop'

# Accepted architecture findings:
# AWS-0039: EKS >=1.28 has no-cost default envelope encryption with an AWS-owned
# key. A customer-managed KMS key would add cost without improving this demo.
# AWS-0040: the EKS public endpoint is required for the operator and is restricted
# by validation to one caller /32, except for up to four explicit reviewed recovery ranges;
# the private endpoint remains enabled.
# AWS-0164: public subnets/public node IPv4 are intentional because this no-NAT
# design must reach EKS add-ons and ECR while avoiding NAT Gateway cost.
$root = Split-Path -Parent $PSScriptRoot
$scanner = & (Join-Path $PSScriptRoot 'resolve-trivy.ps1')
$targets = @(
  (Join-Path $root 'bootstrap/automation'),
  (Join-Path $root 'environments/demo'),
  (Join-Path $root 'environments/staging'),
  (Join-Path $root 'modules/client-vpn'),
  (Join-Path $root 'modules/cloudfront'),
  (Join-Path $root 'modules/private-dns'),
  (Join-Path $root 'modules/order-persistence'),
  (Join-Path $root 'modules/argocd'),
  (Join-Path $root 'modules/aws-load-balancer-controller'),
  (Join-Path $root 'modules/ecr'),
  (Join-Path $root 'modules/eks'),
  (Join-Path $root 'modules/vpc')
)
foreach ($target in $targets) {
  $extra = @()
  if ($target -eq (Join-Path $root 'bootstrap/automation')) {
    $extra = @('--tf-vars', (Join-Path $target 'scan.tfvars.example'))
  }
  $scanOutput = & $scanner config --severity HIGH,CRITICAL --exit-code 1 --ignorefile (Join-Path $root '.trivyignore') --skip-dirs '.terraform,.terraform-helm,.plans,.claude,.codex' --skip-version-check @extra $target 2>&1
  $scanExit = $LASTEXITCODE
  $scanOutput | Out-Host
  if (($scanOutput -join "`n") -match 'Module will be skipped|older than minimum trivy version') { throw 'Security scan skipped policy coverage; update the pinned scanner.' }
  if ($scanExit -ne 0) { throw "Trivy configuration scan failed for $target." }
}
