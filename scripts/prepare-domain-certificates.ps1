param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [Parameter(Mandatory)][ValidateSet('RequestPublic', 'ImportVpn', 'Status')][string]$Action,
  [Parameter(Mandatory)][ValidatePattern('^[0-9]{12}$')][string]$ExpectedAccountId,
  [string]$Region = 'us-east-1',
  [string]$DomainName = 'shop.dinhminhkhoa.id.vn',
  [string]$PkiDirectory = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $PkiDirectory) { $PkiDirectory = Join-Path $env:LOCALAPPDATA "TechX/client-vpn-pki-$Environment" }
$statePath = Join-Path $root "certificate-state-$Environment.private.json"

function Invoke-AwsJson([string[]]$Arguments) {
  $raw = & aws @Arguments --output json
  if ($LASTEXITCODE -ne 0) { throw "aws failed: $($Arguments -join ' ')" }
  return $raw | ConvertFrom-Json
}

$identity = Invoke-AwsJson @('sts', 'get-caller-identity')
if ($identity.Account -ne $ExpectedAccountId) { throw 'Current AWS identity does not match ExpectedAccountId.' }

function Get-PublicCertificate {
  $list = Invoke-AwsJson @('acm', 'list-certificates', '--region', $Region)
  foreach ($certificate in @($list.CertificateSummaryList | Where-Object { $_.DomainName -eq $DomainName })) {
    $tags = Invoke-AwsJson @('acm', 'list-tags-for-certificate', '--region', $Region, '--certificate-arn', $certificate.CertificateArn)
    $projectTag = @($tags.Tags | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1
    $environmentTag = @($tags.Tags | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1
    if ($projectTag -and $environmentTag) {
      return $certificate
    }
  }
  return $null
}

function Get-PublicCertificateStatus {
  $certificate = Get-PublicCertificate
  if ($certificate.Count -ne 1) {
    return [ordered]@{ certificateArn = $null; status = 'NOT_REQUESTED'; validation = $null }
  }
  $details = (Invoke-AwsJson @('acm', 'describe-certificate', '--region', $Region, '--certificate-arn', $certificate[0].CertificateArn)).Certificate
  $record = @($details.DomainValidationOptions.ResourceRecord | Where-Object { $_ } | Select-Object -First 1)
  return [ordered]@{
    certificateArn = $details.CertificateArn
    status = $details.Status
    validation = if ($record.Count -eq 1) {
      @{ name = $record[0].Name; type = $record[0].Type; value = $record[0].Value; cloudflareProxy = $false }
    } else { $null }
  }
}

if ($Action -eq 'RequestPublic') {
  $certificate = Get-PublicCertificate
  if ($certificate.Count -eq 0) {
    $idempotencyToken = "techx$([datetime]::UtcNow.ToString('yyyyMMddHH'))"
    Invoke-AwsJson @(
      'acm', 'request-certificate', '--region', $Region,
      '--domain-name', $DomainName,
      '--validation-method', 'DNS',
      '--key-algorithm', 'RSA_2048',
      '--idempotency-token', $idempotencyToken,
      '--tags', 'Key=Project,Value=techx', "Key=Environment,Value=$Environment"
    ) | Out-Null
    Start-Sleep -Seconds 5
  }
  Get-PublicCertificateStatus | ConvertTo-Json -Depth 5
  exit 0
}

if ($Action -eq 'Status') {
  Get-PublicCertificateStatus | ConvertTo-Json -Depth 5
  exit 0
}

if (Test-Path -LiteralPath $statePath) { throw "Certificate state already exists; refusing duplicate import: $statePath" }
$paths = @{
  serverCertificate = Join-Path $PkiDirectory 'server.crt'
  serverPrivateKey = Join-Path $PkiDirectory 'server.key'
  operatorCertificate = Join-Path $PkiDirectory 'operator.crt'
  operatorPrivateKey = Join-Path $PkiDirectory 'operator.key'
  chain = Join-Path $PkiDirectory 'ca.crt'
}
foreach ($path in $paths.Values) {
  if (-not (Test-Path -LiteralPath $path)) { throw "Required PKI file is missing: $path" }
}
$public = Get-PublicCertificateStatus
if ($public.status -ne 'ISSUED') {
  throw "Public certificate must be ISSUED before VPN certificate import; current status is $($public.status)."
}

$server = Invoke-AwsJson @(
  'acm', 'import-certificate', '--region', $Region,
  '--certificate', "fileb://$($paths.serverCertificate)",
  '--private-key', "fileb://$($paths.serverPrivateKey)",
  '--certificate-chain', "fileb://$($paths.chain)",
  '--tags', 'Key=Project,Value=techx', "Key=Environment,Value=$Environment", 'Key=Role,Value=client-vpn-server'
)
try {
  $reference = Invoke-AwsJson @(
    'acm', 'import-certificate', '--region', $Region,
    '--certificate', "fileb://$($paths.operatorCertificate)",
    '--private-key', "fileb://$($paths.operatorPrivateKey)",
    '--certificate-chain', "fileb://$($paths.chain)",
    '--tags', 'Key=Project,Value=techx', "Key=Environment,Value=$Environment", 'Key=Role,Value=client-vpn-reference'
  )
}
catch {
  aws acm delete-certificate --region $Region --certificate-arn $server.CertificateArn | Out-Null
  throw
}
$state = [ordered]@{
  createdAt = [datetimeoffset]::Now.ToString('o')
  environment = $Environment
  accountId = $identity.Account
  region = $Region
  domainName = $DomainName
  publicCertificateArn = $public.certificateArn
  publicCertificateStatus = $public.status
  clientVpnServerCertificateArn = $server.CertificateArn
  clientVpnRootCertificateArn = $reference.CertificateArn
}
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
$state | ConvertTo-Json
