$ErrorActionPreference = 'Stop'
$backendTest = @{ calls = @(); exit = 0 }
$testKeyArn = 'arn:aws:kms:us-east-1:058114477594:key/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
function terraform {
  $backendTest.calls += ,@($args)
  $global:LASTEXITCODE = $backendTest.exit
}
function Expect-Rejection([scriptblock]$Action, [string]$Message) {
  $rejected = $false
  try { & $Action } catch { $rejected = $true }
  if (-not $rejected) { throw $Message }
}
Expect-Rejection { & "$PSScriptRoot/init-backend.ps1" -Bucket '' } 'A missing backend bucket must fail.'
Expect-Rejection { & "$PSScriptRoot/init-backend.ps1" -Bucket 'bad/bucket' } 'An invalid backend bucket must fail.'
Expect-Rejection { & "$PSScriptRoot/init-backend.ps1" -Bucket 'techx-test-state' -KmsKeyArn '' } 'A missing encryption key must fail.'
& "$PSScriptRoot/init-backend.ps1" -Bucket 'techx-test-state' -KmsKeyArn $testKeyArn
if ($backendTest.calls.Count -ne 1 -or $backendTest.calls[0] -notcontains '-backend-config=bucket=techx-test-state' -or $backendTest.calls[0] -contains '-backend=false') {
  throw 'Staging must initialize the selected remote backend.'
}
$backendTest.exit = 1
Expect-Rejection { & "$PSScriptRoot/init-backend.ps1" -Bucket 'techx-test-state' -KmsKeyArn $testKeyArn } 'Backend init failures must propagate.'
Write-Host 'Remote backend configuration and failure regression tests passed (Terraform mocked).'
