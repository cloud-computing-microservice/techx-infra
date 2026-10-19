$ErrorActionPreference = 'Stop'
$previousAwsBlock = (Get-Item Function:\aws -ErrorAction SilentlyContinue).ScriptBlock
$previousExitCode = $global:LASTEXITCODE
$testState = @{ hasRepository = $false; awsFailure = $false }

# Every request is intercepted; unexpected requests fail instead of using AWS.
Set-Item Function:global:aws -Value ({
  $arguments = @($args)
  $global:LASTEXITCODE = 0
  if ($testState.awsFailure) {
    $global:LASTEXITCODE = 1
    return '{}'
  }
  $operation = "$($arguments[0]) $($arguments[1])"
  switch ($operation) {
    'sts get-caller-identity' { return '{"Account":"000000000000"}' }
    'eks list-clusters' { return '{"clusters":[]}' }
    'resourcegroupstaggingapi get-resources' {
      if ($testState.hasRepository -and $arguments -contains 'ecr:repository') {
        return '{"ResourceTagMappingList":[{"ResourceARN":"arn:aws:ecr:us-east-1:000000000000:repository/techx/order"}]}'
      }
      return '{"ResourceTagMappingList":[]}'
    }
    'ec2 describe-client-vpn-endpoints' { return '{"ClientVpnEndpoints":[]}' }
    'ec2 describe-vpc-endpoints' { return '{"VpcEndpoints":[]}' }
    'cloudfront list-distributions' { return '{"DistributionList":{"Items":[]}}' }
    'route53 list-hosted-zones-by-name' { return '{"HostedZones":[]}' }
    'iam list-roles' { return '[]' }
    default { throw "Unexpected mocked AWS operation: $operation" }
  }
}.GetNewClosure())

try {
  $inventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json
  foreach ($field in @('clusters', 'nodeGroups', 'loadBalancerArns', 'clientVpn', 'cloudFront',
      'privateHostedZones', 'ecrRepositories', 'dynamodbTables', 'dynamodbVpcEndpoints',
      'orderApiIamRoles', 'acmCertificates')) {
    if ($null -eq $inventory.$field -or $inventory.$field.Count -ne 0) {
      throw "Empty inventory must produce [] for $field, not null or [null]."
    }
  }
  $testState.hasRepository = $true
  $inventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json
  if ($inventory.ecrRepositories.Count -ne 1 -or $inventory.ecrRepositories[0] -ne 'techx/order') {
    throw 'A remaining ECR repository must be reported.'
  }
  $testState.awsFailure = $true
  $failedClosed = $false
  try { & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | Out-Null }
  catch { $failedClosed = $_.Exception.Message -like 'aws failed:*' }
  if (-not $failedClosed) { throw 'AWS API failures must not become an empty successful inventory.' }
}
finally {
  if ($previousAwsBlock) { Set-Item Function:global:aws $previousAwsBlock }
  else { Remove-Item Function:\aws -ErrorAction SilentlyContinue }
  $global:LASTEXITCODE = $previousExitCode
}

$restoredAws = Get-Item Function:\aws -ErrorAction SilentlyContinue
if (($previousAwsBlock -and $restoredAws.ScriptBlock.ToString() -ne $previousAwsBlock.ToString()) -or
    (-not $previousAwsBlock -and $restoredAws)) {
  throw 'Inventory tests must restore the original AWS command.'
}
Write-Host 'Inventory empty/nonempty/API-failure and command-restoration regression tests passed.'
