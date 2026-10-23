param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [string]$Bucket = $env:TECHX_STATE_BUCKET,
  [string]$KmsKeyArn = $env:TECHX_STATE_KMS_KEY_ARN
)
$ErrorActionPreference = 'Stop'
$environmentRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "environments/$Environment"
if ($Environment -eq 'demo') {
  terraform "-chdir=$environmentRoot" init -backend=false -input=false | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Demo offline initialization failed.' }
  return
}
if ($Bucket -notmatch '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$') {
  throw 'Set TECHX_STATE_BUCKET to the bootstrapped private S3 state bucket before any staging mutation.'
}
if ($KmsKeyArn -notmatch '^arn:aws:kms:us-east-1:058114477594:key/[a-f0-9-]{36}$') {
  throw 'Set TECHX_STATE_KMS_KEY_ARN to the bootstrapped state encryption key ARN.'
}
$localStatePath = Join-Path $environmentRoot 'terraform.tfstate'
if (Test-Path -LiteralPath $localStatePath) {
  $localState = Get-Content -Raw -LiteralPath $localStatePath | ConvertFrom-Json
  if (@($localState.resources).Count -gt 0) {
    throw 'Local state still owns resources. Back up and explicitly migrate state before remote execution; automatic reconfiguration refused.'
  }
}
terraform "-chdir=$environmentRoot" init -input=false -reconfigure "-backend-config=bucket=$Bucket" "-backend-config=kms_key_id=$KmsKeyArn" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Remote S3 backend initialization failed.' }
