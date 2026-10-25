function Remove-TechxEnvironmentCertificates {
  param(
    [Parameter(Mandatory)][ValidateSet('058114477594')][string]$ExpectedAccountId,
    [Parameter(Mandatory)][ValidateSet('demo', 'staging')][string]$Environment,
    [ValidateSet('us-east-1')][string]$Region = 'us-east-1',
    [Parameter(Mandatory)][string]$StatePath
  )
  $identity = aws sts get-caller-identity --output json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or $identity.Account -ne $ExpectedAccountId) { throw 'Certificate cleanup account mismatch.' }
  $certificateArns = @()
  if (Test-Path -LiteralPath $StatePath) {
    $certificateState = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    if ($certificateState.environment -ne $Environment -or $certificateState.accountId -ne $ExpectedAccountId -or $certificateState.region -ne $Region) {
      throw 'Certificate state does not match the selected environment, account, and region.'
    }
    $certificateArns += @($certificateState.publicCertificateArn, $certificateState.clientVpnServerCertificateArn,
      $certificateState.clientVpnRootCertificateArn) | Where-Object { $_ }
  }
  # ListCertificates can omit imported client certificates without a DNS name.
  # Discover by ownership tags and also retain every exact ARN from our snapshot.
  $tagged = aws resourcegroupstaggingapi get-resources --region $Region `
    --tag-filters Key=Project,Values=techx "Key=Environment,Values=$Environment" `
    --resource-type-filters acm:certificate --output json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Unable to discover owned ACM certificates.' }
  $certificateArns += @($tagged.ResourceTagMappingList.ResourceARN) | Where-Object { $_ }
  $deleted = @()
  foreach ($certificateArn in @($certificateArns | Sort-Object -Unique)) {
    if ($certificateArn -notmatch "^arn:aws:acm:${Region}:$ExpectedAccountId`:certificate/[a-f0-9-]{36}$") {
      throw 'Certificate ARN is outside the approved account/region.'
    }
    $raw = aws acm describe-certificate --region $Region --certificate-arn $certificateArn --output json 2>&1
    if ($LASTEXITCODE -ne 0) {
      if (($raw -join "`n") -match '(?m)^An error occurred \(ResourceNotFoundException\) when calling the DescribeCertificate operation') { continue }
      throw "Unable to verify certificate presence: $certificateArn"
    }
    $certificate = ($raw -join "`n") | ConvertFrom-Json
    if ($certificate.Certificate.CertificateArn -ne $certificateArn -or $null -eq $certificate.Certificate.InUseBy) {
      throw 'ACM certificate presence/consumer response is invalid.'
    }
    $tags = aws acm list-tags-for-certificate --region $Region --certificate-arn $certificateArn --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Unable to verify certificate ownership: $certificateArn" }
    $projectMatches = @($tags.Tags | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1
    $environmentMatches = @($tags.Tags | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1
    if (-not ($projectMatches -and $environmentMatches)) { throw 'ACM ownership changed; certificate deletion refused.' }
    if (@($certificate.Certificate.InUseBy).Count -ne 0) { throw "Certificate still has consumers; deletion refused: $certificateArn" }
    aws acm delete-certificate --region $Region --certificate-arn $certificateArn
    if ($LASTEXITCODE -ne 0) { throw "Unable to delete certificate: $certificateArn" }
    $deleted += $certificateArn
  }
  # Keep the snapshot until the caller has confirmed state AND inventory empty.
  return $deleted
}
