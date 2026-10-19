param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [Parameter(Mandatory)][ValidatePattern('^[0-9]{12}$')][string]$ExpectedAccountId,
  [Parameter(Mandatory)][string]$BudgetAlertEmail,
  [Parameter(Mandatory)][datetimeoffset]$DestroyDeadline,
  [ValidateRange(1, 24)][int]$MaximumHours = 12,
  [ValidateSet('foundation', 'recovery', 'edge', 'hardening', 'maintenance')][string]$Stage = 'maintenance',
  [ValidateCount(0, 4)][string[]]$PublicAccessCidrs = @(),
  [string]$PrivateVarFile = ''
)

$ErrorActionPreference = 'Stop'
$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $identity.Account -ne $ExpectedAccountId) {
  throw 'Current AWS identity does not match ExpectedAccountId.'
}

# An explicit invocation authorizes execution. Machine review, account, cost,
# checksum and deadline checks still run; no sentence needs to be pasted back.
& (Join-Path $PSScriptRoot 'create-reviewed-plan.ps1') `
  -Environment $Environment -Stage $Stage -BudgetAlertEmail $BudgetAlertEmail `
  -DestroyDeadline $DestroyDeadline -MaximumHours $MaximumHours `
  -PublicAccessCidrs $PublicAccessCidrs -PrivateVarFile $PrivateVarFile

$root = Split-Path -Parent $PSScriptRoot
$summary = Get-Content -Raw -LiteralPath (Join-Path $root "approval-summary-$Environment-$Stage.private.txt")
$checksum = [regex]::Match($summary, 'SHA256:\s*([0-9a-f]{64})').Groups[1].Value
if (-not $checksum) { throw 'Reviewed plan summary has no checksum.' }
& (Join-Path $PSScriptRoot 'apply-reviewed-plan.ps1') `
  -Environment $Environment -Stage $Stage -ApprovedChecksum $checksum `
  -ApprovedDestroyDeadline $DestroyDeadline
