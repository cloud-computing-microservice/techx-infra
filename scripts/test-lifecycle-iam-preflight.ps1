$ErrorActionPreference = 'Stop'
$preflightTestState = @{ calls = @() }
function aws {
  $global:LASTEXITCODE = 0
  if ($args[0] -eq 'sts') { return '{"Account":"058114477594"}' }
  $index = [array]::IndexOf($args, '--cli-input-json')
  $request = Get-Content -LiteralPath ($args[$index + 1].Substring(7)) -Raw | ConvertFrom-Json
  $preflightTestState.calls += $request.PolicySourceArn
  return @{ EvaluationResults = @(@{
        EvalActionName = $request.ActionNames[0]; EvalDecision = 'allowed'; EvalResourceName = $request.ResourceArns[0]; MissingContextValues = @()
      }) } | ConvertTo-Json -Depth 8
}
$directory = Join-Path ([System.IO.Path]::GetTempPath()) ("techx-preflight-test-" + [guid]::NewGuid().ToString('N'))
[System.IO.Directory]::CreateDirectory($directory) | Out-Null
$path = Join-Path $directory 'outputs.json'
try {
  $matrix = @{}
  foreach ($name in @('manager', 'executor', 'dispatcher')) {
    $matrix[$name] = @{
      principalArn = "arn:aws:iam::058114477594:role/techx-automation-lifecycle-$name"
      cases = @(@{ name = "fixture-$name"; action = 's3:PutObject'; resources = @('arn:aws:s3:::techx-test-control/staging/terraform.tfstate'); expected = 'allowed' })
    }
  }
  @{ lifecycle_iam_simulation_cases_json = @{ value = ($matrix | ConvertTo-Json -Depth 15 -Compress) } } |
    ConvertTo-Json -Depth 18 | Set-Content -LiteralPath $path -Encoding utf8NoBOM
  $evidence = & (Join-Path $PSScriptRoot 'check-lifecycle-iam.ps1') -BootstrapOutputsFile $path
  if (@($evidence).Count -ne 3 -or @($preflightTestState.calls | Sort-Object -Unique).Count -ne 3) { throw 'Preflight omitted a lifecycle role.' }
  $matrix.executor.principalArn = 'arn:aws:iam::058114477594:role/unrelated-admin'
  @{ lifecycle_iam_simulation_cases_json = @{ value = ($matrix | ConvertTo-Json -Depth 15 -Compress) } } |
    ConvertTo-Json -Depth 18 | Set-Content -LiteralPath $path -Encoding utf8NoBOM
  $failed = $false
  try { & (Join-Path $PSScriptRoot 'check-lifecycle-iam.ps1') -BootstrapOutputsFile $path | Out-Null } catch { $failed = $true }
  if (-not $failed) { throw 'Preflight accepted a substituted IAM principal.' }
} finally {
  Remove-Item -LiteralPath $path -ErrorAction SilentlyContinue
  [System.IO.Directory]::Delete($directory)
}
Write-Host 'Lifecycle IAM preflight output/role contract tests passed.'
