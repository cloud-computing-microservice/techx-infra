param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ApprovedChecksum,
  [Parameter(Mandatory)][datetimeoffset]$ApprovedDestroyDeadline,
  [ValidateSet('foundation', 'recovery', 'edge', 'hardening')][string]$Stage = 'foundation'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path $root "environments/$Environment"
$planPath = Join-Path $root ".plans/$Environment-$Stage.tfplan"
$summaryPath = Join-Path $root "approval-summary-$Environment-$Stage.private.txt"
$helmHome = Join-Path $root '.terraform-helm'

if (-not (Test-Path -LiteralPath $planPath) -or -not (Test-Path -LiteralPath $summaryPath)) {
  throw 'The reviewed plan or private approval summary is missing.'
}

$summary = Get-Content -LiteralPath $summaryPath -Raw
$summaryChecksum = [regex]::Match($summary, 'SHA256:\s*([0-9a-f]{64})').Groups[1].Value
$summaryAccount = [regex]::Match($summary, 'Account ID:\s*(\d{12})').Groups[1].Value
$summaryDeadlineText = [regex]::Match($summary, 'Destroy deadline:\s*(.+)').Groups[1].Value.Trim()
$summaryDeadline = [datetimeoffset]::Parse($summaryDeadlineText)
$summaryEnvironment = [regex]::Match($summary, 'Environment:\s*(\w+)').Groups[1].Value
$summaryStage = [regex]::Match($summary, 'Stage:\s*(\w+)').Groups[1].Value
$actualChecksum = (Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash.ToLowerInvariant()

if ($ApprovedChecksum -ne $summaryChecksum -or $ApprovedChecksum -ne $actualChecksum) {
  throw 'Approved, summarized, and actual saved-plan checksums do not match.'
}
if ($ApprovedDestroyDeadline -ne $summaryDeadline) {
  throw 'Approved destroy deadline does not match the reviewed summary.'
}
if ($summaryEnvironment -ne $Environment) {
  throw 'Requested environment does not match the reviewed summary.'
}
if ($summaryStage -ne $Stage) {
  throw 'Requested stage does not match the reviewed summary.'
}
if ($summaryDeadline -le [datetimeoffset]::Now) {
  throw 'The approved destroy deadline has expired.'
}

$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $identity.Account -ne $summaryAccount) {
  throw 'Current AWS identity does not match the reviewed plan.'
}

New-Item -ItemType Directory -Force -Path (Join-Path $helmHome 'repository') | Out-Null
$env:HELM_REPOSITORY_CONFIG = Join-Path $helmHome 'repositories.yaml'
$env:HELM_REPOSITORY_CACHE = Join-Path $helmHome 'repository'

try {
  helm repo add eks https://aws.github.io/eks-charts --force-update | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to configure the pinned EKS chart repository.' }
  helm repo add argo https://argoproj.github.io/argo-helm --force-update | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to configure the pinned Argo chart repository.' }
  helm repo update eks argo | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to refresh the isolated Helm repository cache.' }

  $applyLog = Join-Path $root "apply-$Environment-$Stage.private.log"
  terraform "-chdir=$environmentRoot" apply -input=false -no-color $planPath 2>&1 | Tee-Object -LiteralPath $applyLog
  if ($LASTEXITCODE -ne 0) { throw 'Reviewed Terraform apply failed.' }
}
finally {
  Remove-Item Env:HELM_REPOSITORY_CONFIG, Env:HELM_REPOSITORY_CACHE -ErrorAction SilentlyContinue
}
