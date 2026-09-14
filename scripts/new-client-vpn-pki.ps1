param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [string]$OutputDirectory = '',
  [string]$OperatorName = 'dinh-minh-khoa'
)

$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $env:LOCALAPPDATA "TechX/client-vpn-pki-$Environment" }
$output = [System.IO.Path]::GetFullPath($OutputDirectory)
if ($output.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw 'PKI output must be outside the Git workspace.'
}
if ($OperatorName -notmatch '^[a-z0-9-]+$') { throw 'OperatorName may contain only lowercase letters, digits, and hyphens.' }
if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) { throw 'OpenSSL is required.' }
if (Test-Path -LiteralPath $output) { throw "Refusing to overwrite existing PKI directory: $output" }

New-Item -ItemType Directory -Path $output | Out-Null
$serverName = 'server.shop.dinhminhkhoa.id.vn'
$serverExt = Join-Path $output 'server.ext'
$clientExt = Join-Path $output 'client.ext'

@"
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$serverName
"@ | Set-Content -LiteralPath $serverExt -Encoding ascii
@"
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
"@ | Set-Content -LiteralPath $clientExt -Encoding ascii

Push-Location $output
try {
  & openssl genrsa -out ca.key 2048
  if ($LASTEXITCODE -ne 0) { throw 'Unable to generate the client VPN CA key.' }
  & openssl req -x509 -new -sha256 -days 365 -key ca.key -subj '/CN=TechX Client VPN CA' -out ca.crt
  if ($LASTEXITCODE -ne 0) { throw 'Unable to create the client VPN CA certificate.' }

  & openssl genrsa -out server.key 2048
  & openssl req -new -sha256 -key server.key -subj "/CN=$serverName" -out server.csr
  & openssl x509 -req -sha256 -days 365 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -extfile server.ext -out server.crt
  if ($LASTEXITCODE -ne 0) { throw 'Unable to create the server certificate.' }

  & openssl genrsa -out operator.key 2048
  & openssl req -new -sha256 -key operator.key -subj "/CN=$OperatorName" -out operator.csr
  & openssl x509 -req -sha256 -days 365 -in operator.csr -CA ca.crt -CAkey ca.key -CAcreateserial -extfile client.ext -out operator.crt
  if ($LASTEXITCODE -ne 0) { throw 'Unable to create the operator certificate.' }

  $verification = & openssl verify -CAfile ca.crt server.crt operator.crt 2>&1
  if ($LASTEXITCODE -ne 0) { throw 'Generated certificate chain verification failed.' }
  if (($verification -join "`n") -notmatch 'server\.crt: OK' -or ($verification -join "`n") -notmatch 'operator\.crt: OK') {
    throw 'OpenSSL did not confirm both generated certificates.'
  }
}
finally {
  Pop-Location
}

[ordered]@{
  environment = $Environment
  directory = $output
  serverCertificate = (Join-Path $output 'server.crt')
  serverPrivateKey = (Join-Path $output 'server.key')
  operatorCertificate = (Join-Path $output 'operator.crt')
  operatorPrivateKey = (Join-Path $output 'operator.key')
  certificateAuthority = (Join-Path $output 'ca.crt')
  warning = 'Treat this directory as secret. Do not copy it into Git, chat, logs, screenshots, or report evidence.'
} | ConvertTo-Json
