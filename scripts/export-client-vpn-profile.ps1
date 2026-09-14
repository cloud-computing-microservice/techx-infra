param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [Parameter(Mandatory)][ValidatePattern('^cvpn-endpoint-[0-9a-f]+$')][string]$ClientVpnEndpointId,
  [Parameter(Mandatory)][ValidatePattern('^[0-9]{12}$')][string]$ExpectedAccountId,
  [string]$Region = 'us-east-1',
  [string]$PkiDirectory = '',
  [string]$OutputPath = ''
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $PkiDirectory) { $PkiDirectory = Join-Path $env:LOCALAPPDATA "TechX/client-vpn-pki-$Environment" }
if (-not $OutputPath) { $OutputPath = Join-Path $env:LOCALAPPDATA "TechX/client-vpn/techx-$Environment.private.ovpn" }
$output = [IO.Path]::GetFullPath($OutputPath)
if ($output.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
  throw 'The completed VPN profile must be outside the Git workspace.'
}
if (Test-Path -LiteralPath $output) { throw "Refusing to overwrite an existing VPN profile: $output" }

$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $identity.Account -ne $ExpectedAccountId) {
  throw 'Current AWS identity does not match ExpectedAccountId.'
}
$endpoint = aws ec2 describe-client-vpn-endpoints --region $Region `
  --client-vpn-endpoint-ids $ClientVpnEndpointId --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $endpoint.ClientVpnEndpoints.Count -ne 1 -or $endpoint.ClientVpnEndpoints[0].Status.Code -ne 'available') {
  throw 'Client VPN endpoint is not available.'
}

$certificatePath = Join-Path $PkiDirectory 'operator.crt'
$privateKeyPath = Join-Path $PkiDirectory 'operator.key'
if (-not (Test-Path -LiteralPath $certificatePath) -or -not (Test-Path -LiteralPath $privateKeyPath)) {
  throw 'Operator certificate or private key is missing from the external PKI directory.'
}

$configuration = aws ec2 export-client-vpn-client-configuration --region $Region `
  --client-vpn-endpoint-id $ClientVpnEndpointId --output text
if ($LASTEXITCODE -ne 0 -or -not $configuration) { throw 'Unable to export the Client VPN configuration.' }
$text = $configuration -join "`n"
if ($text -match '<cert>|<key>') { throw 'Exported profile unexpectedly contains client credentials.' }
$text = [regex]::Replace(
  $text,
  '(?m)^remote\s+(cvpn-endpoint-[^\s]+)\s+(443)\s*$',
  'remote techx.$1 $2'
)
if ($text -notmatch '(?m)^remote\s+techx\.cvpn-endpoint-') {
  throw 'Unable to normalize the Client VPN remote hostname.'
}

$certificate = Get-Content -LiteralPath $certificatePath -Raw
$privateKey = Get-Content -LiteralPath $privateKeyPath -Raw
$completed = "$($text.TrimEnd())`n<cert>`n$($certificate.Trim())`n</cert>`n<key>`n$($privateKey.Trim())`n</key>`n"
$directory = Split-Path -Parent $output
New-Item -ItemType Directory -Force -Path $directory | Out-Null
icacls $directory /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to restrict VPN profile directory ACL.' }
$completed | Set-Content -LiteralPath $output -Encoding ascii -NoNewline

[ordered]@{
  environment = $Environment
  endpointId = $ClientVpnEndpointId
  profilePath = $output
  warning = 'This file contains a client private key. Do not copy it into Git, chat, logs, screenshots, or report evidence.'
} | ConvertTo-Json
