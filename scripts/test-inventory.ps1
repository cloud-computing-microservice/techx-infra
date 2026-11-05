$ErrorActionPreference = 'Stop'
$previousAwsBlock = (Get-Item Function:\aws -ErrorAction SilentlyContinue).ScriptBlock
$previousExitCode = $global:LASTEXITCODE
$testState = @{ hasRepository = $false; hasVolume = $false; awsFailure = $false; failOperation = '';
  originPages = @{}; functionPages = @{}; distributionPages = @{}; logGroups = @(); badLogs = $false; workloadTags = @{} }

# Every request is intercepted; unexpected requests fail instead of using AWS.
Set-Item Function:global:aws -Value ({
  $arguments = @($args)
  $global:LASTEXITCODE = 0
  if ($testState.awsFailure) {
    $global:LASTEXITCODE = 1
    return '{}'
  }
  $operation = "$($arguments[0]) $($arguments[1])"
  if ($operation -eq $testState.failOperation) { $global:LASTEXITCODE = 1; return '{}' }
  $marker = 'first'
  if ($arguments -contains '--marker') { $marker = $arguments[[array]::IndexOf($arguments, '--marker') + 1] }
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
    'ec2 describe-instances' { return '{"Reservations":[]}' }
    'ec2 describe-volumes' {
      if ($testState.hasVolume) { return '{"Volumes":[{"VolumeId":"vol-leftover"}]}' }
      return '{"Volumes":[]}'
    }
    'ec2 describe-vpcs' { return '{"Vpcs":[]}' }
    'ec2 describe-addresses' { return '{"Addresses":[]}' }
    'secretsmanager list-secrets' { return '{"SecretList":[]}' }
    'cloudfront list-distributions' {
      if ($testState.distributionPages.ContainsKey($marker)) { return $testState.distributionPages[$marker] | ConvertTo-Json -Depth 10 -Compress }
      return '{"DistributionList":{"Quantity":0,"IsTruncated":false,"Items":[]}}'
    }
    'cloudfront list-vpc-origins' {
      if ($testState.originPages.ContainsKey($marker)) { return $testState.originPages[$marker] | ConvertTo-Json -Depth 10 -Compress }
      return '{"VpcOriginList":{"Quantity":0,"IsTruncated":false,"Items":[]}}'
    }
    'cloudfront list-functions' {
      if ($testState.functionPages.ContainsKey($marker)) { return $testState.functionPages[$marker] | ConvertTo-Json -Depth 10 -Compress }
      return '{"FunctionList":{"Quantity":0,"Items":[]}}'
    }
    'cloudfront list-tags-for-resource' {
      $arn = $arguments[[array]::IndexOf($arguments, '--resource') + 1]
      if ($testState.workloadTags.ContainsKey($arn)) { return '{"Tags":{"Items":[{"Key":"Project","Value":"techx"},{"Key":"Environment","Value":"staging"}]}}' }
      return '{"Tags":{"Items":[{"Key":"Project","Value":"other"}]}}'
    }
    'logs describe-log-groups' {
      if ($testState.badLogs) { return '{}' }
      $prefix = $arguments[[array]::IndexOf($arguments, '--log-group-name-prefix') + 1]
      return @{ logGroups=@($testState.logGroups | Where-Object { $_.logGroupName.StartsWith($prefix) }) } | ConvertTo-Json -Depth 5 -Compress
    }
    'route53 list-hosted-zones-by-name' { return '{"HostedZones":[]}' }
    'iam list-roles' { return '[]' }
    'iam list-policies' { return '[]' }
    default { throw "Unexpected mocked AWS operation: $operation" }
  }
}.GetNewClosure())

try {
  $inventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json
  foreach ($field in (Get-Content -Raw (Join-Path $PSScriptRoot '../contracts/inventory-fields.json') | ConvertFrom-Json)) {
    if ($null -eq $inventory.$field -or $inventory.$field.Count -ne 0) {
      throw "Empty inventory must produce [] for $field, not null or [null]."
    }
  }
  . (Join-Path $PSScriptRoot 'inventory-contract.ps1')
  Assert-TechxInventoryEmpty $inventory '000000000000' 'staging' 'us-east-1'
  $testState.hasVolume = $true
  $diskInventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json
  $rejected = $false
  try { Assert-TechxInventoryEmpty $diskInventory '000000000000' 'staging' 'us-east-1' } catch { $rejected = $true }
  if (-not $rejected -or $diskInventory.ebsVolumes.Count -ne 1) { throw 'A remaining EBS volume must prevent zero-inventory success.' }
  $testState.hasVolume = $false
  $testState.hasRepository = $true
  $inventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json
  if ($inventory.ecrRepositories.Count -ne 1 -or $inventory.ecrRepositories[0] -ne 'techx/order') {
    throw 'A remaining ECR repository must be reported.'
  }
  $testState.hasRepository = $false
  function Assert-InventoryRejected([string]$Field) {
    $value = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json
    if ($value.$Field.Count -ne 1) { throw "Expected one leftover in $Field" }
    $rejected = $false
    try { Assert-TechxInventoryEmpty $value '000000000000' 'staging' 'us-east-1' } catch { $rejected = $true }
    if (-not $rejected) { throw "Leftover $Field must prevent cleanup completion" }
  }
  function Assert-InventoryReadFails {
    $rejected = $false
    try { & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Incomplete or failed inventory must not succeed' }
  }
  $originArn = 'arn:aws:cloudfront::000000000000:vpcorigin/orphan'
  $testState.workloadTags[$originArn] = $true
  $origin = @{ Id='orphan'; Arn=$originArn; Name='techx-staging-internal-alb'; Status='Deployed' }
  $testState.originPages.first = @{ VpcOriginList=@{ Quantity=0; Items=@(); IsTruncated=$true; NextMarker='later' } }
  $testState.originPages.later = @{ VpcOriginList=@{ Quantity=1; Items=@($origin); IsTruncated=$false } }
  Assert-InventoryRejected 'cloudFrontVpcOrigins'
  $testState.originPages.later.VpcOriginList.IsTruncated = $true
  $testState.originPages.later.VpcOriginList.NextMarker = 'later'
  Assert-InventoryReadFails
  $testState.originPages = @{ first=@{ VpcOriginList=@{ Quantity=0; IsTruncated=$true; Items=@() } } }
  Assert-InventoryReadFails
  $testState.originPages = @{}
  $function = @{ Name='techx-staging-block-argocd'; FunctionMetadata=@{ FunctionARN='arn:aws:cloudfront::000000000000:function/techx-staging-block-argocd'; Stage='LIVE' } }
  $testState.functionPages = @{ first=@{ FunctionList=@{ Quantity=0; Items=@(); NextMarker='later' } }; later=@{ FunctionList=@{ Quantity=1; Items=@($function) } } }
  Assert-InventoryRejected 'cloudFrontFunctions'
  $testState.functionPages.first.FunctionList.Quantity = 1
  Assert-InventoryReadFails
  $testState.functionPages = @{}
  $distributionArn = 'arn:aws:cloudfront::000000000000:distribution/ORPHAN'
  $testState.workloadTags[$distributionArn] = $true
  $testState.distributionPages.first = @{ DistributionList=@{ Quantity=1; Items=@(@{ Id='ORPHAN'; DomainName='orphan.cloudfront.net'; Aliases=@{Items=@()}; Enabled=$false }); IsTruncated=$false } }
  Assert-InventoryRejected 'cloudFront'
  $testState.distributionPages = @{}
  $testState.originPages.first = @{ VpcOriginList=@{ Quantity=1; Items=@(@{ Id='other'; Arn='arn:aws:cloudfront::000000000000:vpcorigin/other'; Name='other' }); IsTruncated=$false } }
  $testState.functionPages.first = @{ FunctionList=@{ Quantity=1; Items=@(@{ Name='techx-demo-block-argocd'; FunctionMetadata=@{ FunctionARN='arn:aws:cloudfront::000000000000:function/techx-demo-block-argocd' } }) } }
  Assert-TechxInventoryEmpty (& (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json) '000000000000' 'staging' 'us-east-1'
  $testState.originPages = @{}
  $testState.functionPages = @{}
  $testState.logGroups = @(@{ logGroupName='/aws/eks/techx-staging/cluster'; arn='arn:aws:logs:us-east-1:000000000000:log-group:/aws/eks/techx-staging/cluster'; storedBytes=12 })
  Assert-InventoryRejected 'cloudWatchLogGroups'
  $testState.logGroups = @(@{ logGroupName='/aws/client-vpn/techx-staging'; arn='arn:aws:logs:us-east-1:000000000000:log-group:/aws/client-vpn/techx-staging'; storedBytes=2 })
  Assert-InventoryRejected 'cloudWatchLogGroups'
  $testState.logGroups = @(@{ logGroupName='/aws/client-vpn/techx-staging2'; arn='unrelated' }, @{ logGroupName='/aws/codebuild/techx-automation-cleanup'; arn='control' })
  Assert-TechxInventoryEmpty (& (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') | ConvertFrom-Json) '000000000000' 'staging' 'us-east-1'
  $testState.badLogs = $true
  Assert-InventoryReadFails
  $testState.badLogs = $false
  foreach ($operation in @('cloudfront list-vpc-origins','cloudfront list-functions','logs describe-log-groups')) {
    $testState.failOperation = $operation
    Assert-InventoryReadFails
  }
  $testState.failOperation = ''
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
Write-Host 'Inventory empty/nonempty, orphan edge/logs, pagination, API-failure, scope and command-restoration regression tests passed.'
