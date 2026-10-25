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

$script:scopedMode = 'allowed'
$script:scopedRequest = $null
function aws {
  $index = [array]::IndexOf($args, '--cli-input-json')
  if ($index -lt 0) { throw 'Scoped simulation must send structured JSON, not shell-interpolated arguments.' }
  $script:scopedRequest = Get-Content -LiteralPath ($args[$index + 1].Substring(7)) -Raw | ConvertFrom-Json
  $global:LASTEXITCODE = if ($script:scopedMode -eq 'apiError') { 1 } else { 0 }
  $decision = if ($script:scopedMode -in @('denied', 'partiallyAllowed')) { 'implicitDeny' } else { 'allowed' }
  $row = @{ EvalActionName = $script:scopedRequest.ActionNames[0]; EvalDecision = $decision; MissingContextValues = @() }
  $row.ResourceSpecificResults = @($script:scopedRequest.ResourceArns | ForEach-Object {
      @{ EvalResourceName = $_; EvalResourceDecision = $decision; MissingContextValues = @() }
    })
  if ($script:scopedMode -eq 'missingContext') { $row.MissingContextValues = @('ec2:LaunchTemplate') }
  if ($script:scopedMode -eq 'partiallyAllowed') { $row.ResourceSpecificResults[0].EvalResourceDecision = 'allowed' }
  if ($script:scopedMode -eq 'missingResource') { $row.ResourceSpecificResults = @() }
  if ($script:scopedMode -eq 'singleResource') {
    $row.ResourceSpecificResults = @()
    $row.EvalResourceName = $script:scopedRequest.ResourceArns[0]
  }
  return @{ EvaluationResults = @($row); IsTruncated = ($script:scopedMode -eq 'truncated') } | ConvertTo-Json -Depth 12
}
$case = @{
  name = 'manager-fixed-instance'; action = 'ec2:RunInstances'; expected = 'allowed'
  resources = @('arn:aws:ec2:us-east-1:058114477594:instance/i-1234567890abcdef0')
  context = @(@{ ContextKeyName = 'ec2:LaunchTemplate'; ContextKeyValues = @('arn:aws:ec2:us-east-1:058114477594:launch-template/lt-12345678'); ContextKeyType = 'string' })
}
$role = 'arn:aws:iam::058114477594:role/techx-automation-lifecycle-manager'
$evidence = Invoke-TechxScopedSimulation -PrincipalArn $role -Cases @($case)
if (-not $evidence.simulatedOnly -or $script:scopedRequest.ResourceArns[0] -ne $case.resources[0] -or
    $script:scopedRequest.ContextEntries[0].ContextKeyName -ne 'ec2:LaunchTemplate') { throw 'Scoped IAM request lost its ARN/context contract.' }
$script:scopedMode = 'singleResource'
Invoke-TechxScopedSimulation -PrincipalArn $role -Cases @($case) | Out-Null
foreach ($mode in @('missingContext', 'missingResource', 'truncated', 'apiError', 'denied')) {
  $script:scopedMode = $mode
  $failed = $false
  try { Invoke-TechxScopedSimulation -PrincipalArn $role -Cases @($case) | Out-Null } catch { $failed = $true }
  if (-not $failed) { throw "Scoped simulation accepted incomplete/unexpected decision: $mode" }
}
$case.expected = 'denied'
Invoke-TechxScopedSimulation -PrincipalArn $role -Cases @($case) | Out-Null
$script:scopedMode = 'partiallyAllowed'
$failed = $false
try { Invoke-TechxScopedSimulation -PrincipalArn $role -Cases @($case) | Out-Null } catch { $failed = $true }
if (-not $failed) { throw 'An overall deny must not hide an allowed resource in a negative IAM case.' }
$case.resources = @('*')
$failed = $false
try { Invoke-TechxScopedSimulation -PrincipalArn $role -Cases @($case) | Out-Null } catch { $failed = $true }
if (-not $failed) { throw 'A broad wildcard simulation must not substitute for the actual resource/context checks.' }
Write-Host 'Scoped IAM ARN/context, positive/negative decisions and incomplete-response tests passed.'
