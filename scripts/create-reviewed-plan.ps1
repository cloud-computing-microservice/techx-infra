param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [Parameter(Mandatory)][string]$BudgetAlertEmail,
  [Parameter(Mandatory)][datetimeoffset]$DestroyDeadline,
  [ValidateRange(1, 24)][int]$MaximumHours = 12,
  [ValidateSet('foundation', 'recovery', 'edge', 'hardening')][string]$Stage = 'foundation',
  [ValidateCount(0, 4)][string[]]$PublicAccessCidrs = @(),
  [string]$PrivateVarFile = ''
)

$ErrorActionPreference = 'Stop'
if (@($PublicAccessCidrs | Where-Object { $_ -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}/(24|32)$' }).Count -gt 0) {
  throw 'PublicAccessCidrs must contain only IPv4 /24 or /32 CIDRs.'
}
$now = [datetimeoffset]::Now
if ($DestroyDeadline -le $now -or $DestroyDeadline -gt $now.AddHours(24)) {
  throw 'DestroyDeadline must be in the future and no more than 24 hours away.'
}

$root = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path $root "environments/$Environment"
$planDirectory = Join-Path $root '.plans'
$planPath = Join-Path $planDirectory "$Environment-$Stage.tfplan"
$helmHome = Join-Path $root '.terraform-helm'
$privateVars = if ($PrivateVarFile) { $PrivateVarFile } else { Join-Path $environmentRoot 'domain-vpn.private.tfvars' }
if (-not (Test-Path -LiteralPath $privateVars)) {
  throw "Private variable file is required: $privateVars"
}
New-Item -ItemType Directory -Force -Path $planDirectory, (Join-Path $helmHome 'repository') | Out-Null

$preflight = & (Join-Path $PSScriptRoot 'preflight.ps1') -Environment $Environment -Stage $Stage -PublicAccessCidrs $PublicAccessCidrs | ConvertFrom-Json
$cost = & (Join-Path $PSScriptRoot 'cost-estimate.ps1') -Hours $MaximumHours -Profile domainVpn | ConvertFrom-Json
if ($cost.upperBoundUsd -gt 60) { throw 'Cost gate failed.' }

$env:HELM_REPOSITORY_CONFIG = Join-Path $helmHome 'repositories.yaml'
$env:HELM_REPOSITORY_CACHE = Join-Path $helmHome 'repository'
$publicAccessCidrs = ConvertTo-Json @($preflight.callerPublicCidrs) -Compress
$temporaryCidrEnabled = @($preflight.callerPublicCidrs | Where-Object { $_ -notlike '*/32' }).Count -gt 0 -or @($preflight.callerPublicCidrs).Count -gt 1
$env:TF_VAR_budget_alert_email = $BudgetAlertEmail

try {
  helm repo add eks https://aws.github.io/eks-charts --force-update | Out-Null
  helm repo add argo https://argoproj.github.io/argo-helm --force-update | Out-Null
  helm repo update eks argo | Out-Null

  terraform "-chdir=$environmentRoot" init -backend=false | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'terraform init failed.' }
  $planArgs = @(
    '-input=false', '-no-color', "-out=$planPath",
    "-var-file=$privateVars",
    "-var=public_access_cidrs=$publicAccessCidrs",
    "-var=allow_temporary_public_access_cidr=$($temporaryCidrEnabled ? 'true' : 'false')",
    '-var=enable_domain_vpn_foundation=true',
    "-var=enable_domain_vpn_edge=$($Stage -in @('edge', 'hardening') ? 'true' : 'false')"
  )
  if ($Stage -in @('edge', 'hardening')) { $planArgs += "-var=internal_alb_arn=$($preflight.internalAlbArn)" }
  if ($Stage -in @('foundation', 'edge')) { $planArgs += '-refresh=false' }
  terraform "-chdir=$environmentRoot" plan @planArgs | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'terraform plan failed.' }

  $review = terraform "-chdir=$environmentRoot" show -json $planPath | python (Join-Path $PSScriptRoot 'review-plan.py') $Stage | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Saved-plan review failed.' }
  $planned = terraform "-chdir=$environmentRoot" show -json $planPath | ConvertFrom-Json
  $plannedCidrs = @($planned.variables.public_access_cidrs.value)
  $expectedCidrs = @($preflight.callerPublicCidrs)
  if ($plannedCidrs.Count -ne $expectedCidrs.Count -or (Compare-Object $plannedCidrs $expectedCidrs)) {
    throw "Saved plan EKS API CIDR does not match preflight: $($plannedCidrs -join ', ')."
  }
  $hash = (Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash.ToLowerInvariant()

  $summary = @"
STATUS: WAITING_FOR_USER_APPROVAL
Environment: $Environment
Stage: $Stage
Account ID: $($preflight.accountId)
Caller: $($preflight.callerArn)
Region: $($preflight.region)
EKS API CIDRs: $($expectedCidrs -join ', ')
Saved plan: .plans/$Environment-$Stage.tfplan
SHA256: $hash
Resources: $($review.actions.create ?? 0) add, $($review.actions.update ?? 0) change, 0 destroy
Estimated upper bound: $($cost.upperBoundUsd) USD for <= $MaximumHours hours
Apply gate: 60 USD; hard cap: 80 USD
Budget alerts in plan: 40, 60, 72 USD
Destroy deadline: $($DestroyDeadline.ToString('yyyy-MM-dd HH:mm:ss zzz'))

Required confirmation (do not run yet):
Tôi xác nhận apply AWS plan này, ngân sách tối đa 80 USD và destroy trước $($DestroyDeadline.ToString('yyyy-MM-dd HH:mm:ss zzz')).
"@
  $summary | Set-Content -LiteralPath (Join-Path $root "approval-summary-$Environment-$Stage.private.txt") -Encoding utf8
  $preflight | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $root "preflight-$Environment.private.json") -Encoding utf8
  Write-Output $summary
}
finally {
  Remove-Item Env:TF_VAR_budget_alert_email, Env:HELM_REPOSITORY_CONFIG, Env:HELM_REPOSITORY_CACHE -ErrorAction SilentlyContinue
}
