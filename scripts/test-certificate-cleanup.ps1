$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'certificate-cleanup.ps1')
$script:rootArn = 'arn:aws:acm:us-east-1:058114477594:certificate/11111111-1111-1111-1111-111111111111'
$script:publicArn = 'arn:aws:acm:us-east-1:058114477594:certificate/22222222-2222-2222-2222-222222222222'
$script:certificateCase = 'normal'
$script:deleted = [System.Collections.Generic.List[string]]::new()
function aws {
  $awsArgs = @($args)
  $global:LASTEXITCODE = 0
  $operation = "$($awsArgs[0]) $($awsArgs[1])"
  switch ($operation) {
    'sts get-caller-identity' { return '{"Account":"058114477594"}' }
    'resourcegroupstaggingapi get-resources' {
      if ($script:certificateCase -eq 'discoveryDenied') { $global:LASTEXITCODE = 255; return 'AccessDenied' }
      return @{ResourceTagMappingList = @(@{ResourceARN = $script:rootArn})} | ConvertTo-Json -Depth 5 -Compress
    }
    'acm list-certificates' { throw 'Default ListCertificates must not be used as a presence gate.' }
    'acm describe-certificate' {
      $arn = $awsArgs[[array]::IndexOf($awsArgs, '--certificate-arn') + 1]
      if ($script:certificateCase -eq 'denied') { $global:LASTEXITCODE = 255; return 'An error occurred (AccessDeniedException) when calling the DescribeCertificate operation: denied' }
      if ($arn -ne $script:rootArn -or $script:certificateCase -eq 'gone') {
        $global:LASTEXITCODE = 255
        return 'An error occurred (ResourceNotFoundException) when calling the DescribeCertificate operation: missing'
      }
      $consumers = if ($script:certificateCase -eq 'inUse') { @('arn:aws:elasticloadbalancing:us-east-1:058114477594:loadbalancer/app/shared/id') } else { @() }
      $details = @{CertificateArn = $arn; InUseBy = @($consumers); DomainName = $null}
      if ($script:certificateCase -eq 'invalid') { $details.InUseBy = $null }
      return @{Certificate = $details} | ConvertTo-Json -Depth 5 -Compress
    }
    'acm list-tags-for-certificate' {
      $project = if ($script:certificateCase -eq 'retagged') { 'another-project' } else { 'techx' }
      return @{Tags = @(@{Key='Project'; Value=$project}, @{Key='Environment'; Value='staging'})} | ConvertTo-Json -Depth 5 -Compress
    }
    'acm delete-certificate' {
      $arn = $awsArgs[[array]::IndexOf($awsArgs, '--certificate-arn') + 1]
      $script:deleted.Add($arn)
      return
    }
    default { throw "Unexpected AWS call in isolated certificate cleanup test: $operation" }
  }
}
$statePath = [IO.Path]::GetTempFileName()
try {
  @{environment='staging'; accountId='058114477594'; region='us-east-1'; publicCertificateArn=$script:publicArn;
    clientVpnRootCertificateArn=$script:rootArn} | ConvertTo-Json | Set-Content -LiteralPath $statePath
  $result = @(Remove-TechxEnvironmentCertificates -ExpectedAccountId 058114477594 -Environment staging -StatePath $statePath)
  if ($result.Count -ne 1 -or $result[0] -ne $script:rootArn -or $script:deleted.Count -ne 1) {
    throw 'The imported client certificate absent from default ACM listing was not removed exactly once.'
  }
  foreach ($case in @('denied', 'inUse', 'retagged', 'invalid', 'discoveryDenied')) {
    $script:certificateCase = $case
    $script:deleted.Clear()
    $failed = $false
    try { Remove-TechxEnvironmentCertificates -ExpectedAccountId 058114477594 -Environment staging -StatePath $statePath | Out-Null }
    catch { $failed = $true }
    if (-not $failed -or $script:deleted.Count -ne 0 -or -not (Test-Path -LiteralPath $statePath)) {
      throw "Certificate ownership/consumer/API failure was not preserved safely: $case"
    }
  }
  $script:certificateCase = 'gone'
  $script:deleted.Clear()
  Remove-TechxEnvironmentCertificates -ExpectedAccountId 058114477594 -Environment staging -StatePath $statePath | Out-Null
  if ($script:deleted.Count -ne 0) { throw 'A missing certificate should be an idempotent no-op.' }
  Write-Host 'Imported-client-certificate discovery, ownership, consumers, API errors and retry tests passed (AWS mocked).'
} finally {
  Remove-Item -LiteralPath $statePath -Force
  Remove-Item Function:aws
}
