$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'iam-contract.ps1')
$iamTest = @{ denied = $false; calls = 0 }
function aws {
  $iamTest.calls++
  $global:LASTEXITCODE = if ($iamTest.denied) { 1 } else { 0 }
  return '{"Role":{"Arn":"arn:aws:iam::058114477594:role/automation/techx-automation-lifecycle"}}'
}
$identity = @{ Account = '058114477594'; Arn = 'arn:aws:sts::058114477594:assumed-role/techx-automation-lifecycle/session' }
if ((Get-SimulationPrincipalArn $identity) -ne 'arn:aws:iam::058114477594:role/automation/techx-automation-lifecycle') {
  throw 'IAM simulation must use the real IAM role ARN, including its path.'
}
$iamTest.denied = $true
$rejected = $false
try { Get-SimulationPrincipalArn $identity } catch { $rejected = $true }
if (-not $rejected) { throw 'A denied GetRole must not fall back to an invalid STS ARN.' }
$identity.Arn = 'arn:aws:iam::058114477594:user/operator'
if ((Get-SimulationPrincipalArn $identity) -ne $identity.Arn -or $iamTest.calls -ne 2) {
  throw 'Existing IAM user preflight should remain read-only and compatible.'
}
Write-Host 'OIDC/CodeBuild assumed-role IAM simulation identity tests passed.'
