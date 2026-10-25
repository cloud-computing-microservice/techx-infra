$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'cost-estimate.ps1'
foreach ($budget in @(30, 60, 80)) {
  $estimate = & $script -Hours 4 -ApprovedBudgetUsd $budget | ConvertFrom-Json
  if ($estimate.approvedBudgetUsd -ne $budget -or $estimate.applyGateUsd -ne [Math]::Min(60, $budget)) {
    throw 'Estimator did not preserve the approved budget and apply gate.'
  }
  if (-not $estimate.includesAutomation -or $estimate.maximumHours -ne 4 -or $estimate.upperBoundUsd -gt $estimate.applyGateUsd) {
    throw 'Estimator did not cover the requested automation window.'
  }
  if ($estimate.fixedAllowances.automationDeploymentExecutor -le 0 -or $estimate.fixedAllowances.automationPrivateProbe -le 0) {
    throw 'Estimator must cover both the persistent deployment executor and the separate private probe.'
  }
  if ($estimate.fixedAllowances.automationImageBuildAllowance -le 0 -or $estimate.fixedAllowances.automationAmiSnapshotMonthlyAllowance -le 0) {
    throw 'Estimator must include AMI build and retained snapshot costs.'
  }
}
foreach ($budget in @(1, 0, 81, [double]::NaN, [double]::PositiveInfinity)) {
  $failed = $false
  try { & $script -Hours 4 -ApprovedBudgetUsd $budget | Out-Null } catch { $failed = $true }
  if (-not $failed) { throw 'Estimator accepted an invalid or insufficient budget.' }
}
Write-Host 'Budget estimator regression tests passed.'
