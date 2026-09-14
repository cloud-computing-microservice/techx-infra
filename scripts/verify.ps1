$ErrorActionPreference = 'Stop'

foreach ($path in @(
    '.gitignore', 'LICENSE', 'README.md',
    'scripts/apply-reviewed-plan.ps1',
    'scripts/create-reviewed-plan.ps1',
    'scripts/domain-vpn-inventory.ps1',
    'scripts/destroy-domain-vpn-environment.ps1',
    'scripts/new-client-vpn-pki.ps1',
    'scripts/prepare-domain-certificates.ps1',
    'scripts/export-client-vpn-profile.ps1'
  )) {
  if (-not (Test-Path -LiteralPath $path)) {
    throw "Missing required bootstrap file: $path"
  }
}

$root = Split-Path -Parent $PSScriptRoot
terraform "-chdir=$root" fmt -check -recursive
if ($LASTEXITCODE -ne 0) { throw 'terraform fmt -check failed.' }
foreach ($environment in @('demo', 'staging')) {
  $environmentRoot = Join-Path $root "environments/$environment"
  terraform "-chdir=$environmentRoot" init -backend=false
  if ($LASTEXITCODE -ne 0) { throw "terraform init failed for $environment." }
  terraform "-chdir=$environmentRoot" validate
  if ($LASTEXITCODE -ne 0) { throw "terraform validate failed for $environment." }
}
python (Join-Path $PSScriptRoot 'static-test.py')
if ($LASTEXITCODE -ne 0) { throw 'Terraform static assertions failed.' }
& (Join-Path $PSScriptRoot 'cost-estimate.ps1') -Hours 12 | Out-Null
& (Join-Path $PSScriptRoot 'security-scan.ps1')

foreach ($script in @(
    'apply-reviewed-plan.ps1', 'create-reviewed-plan.ps1', 'preflight.ps1',
    'domain-vpn-inventory.ps1', 'destroy-domain-vpn-environment.ps1',
    'new-client-vpn-pki.ps1', 'prepare-domain-certificates.ps1',
    'export-client-vpn-profile.ps1', 'suspend-environment.ps1',
    'resume-environment.ps1'
  )) {
  $tokens = $null
  $parseErrors = $null
  [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $PSScriptRoot $script), [ref]$tokens, [ref]$parseErrors
  ) | Out-Null
  if ($parseErrors) { throw "$script has syntax errors: $($parseErrors.Message -join '; ')" }
}

$readmePaths = @(
  (Join-Path (Split-Path -Parent $root) 'techx-platform/README.md'),
  (Join-Path (Split-Path -Parent $root) 'techx-chart/README.md'),
  (Join-Path $root 'README.md')
)
$contractTables = foreach ($readmePath in $readmePaths) {
  if (-not (Test-Path -LiteralPath $readmePath)) { throw "Shared contract README is missing: $readmePath" }
  $match = [regex]::Match((Get-Content -LiteralPath $readmePath -Raw), '(?ms)^\| Contract item.*?(?=\r?\n\r?\n)')
  if (-not $match.Success) { throw "Shared deployment contract table is missing: $readmePath" }
  (($match.Value -split '\r?\n') | ForEach-Object {
      ($_ -replace '\s*\|\s*$', '|') -replace '\|\s+', '| '
    }) -join "`n"
}
if (($contractTables | Sort-Object -Unique).Count -ne 1) {
  throw 'Shared deployment contract tables differ across techx-platform, techx-chart, and techx-infra.'
}

$forbidden = git ls-files | Select-String -Pattern '(^|/)(\.env$|\.terraform/)|\.(tfstate|tfplan|ovpn|key|crt|pem|p12|pfx)$|terraform\.tfvars$|\.private\.tfvars$'
if ($forbidden) {
  throw "Forbidden generated or sensitive path is tracked: $($forbidden -join ', ')"
}

Write-Host 'techx-infra offline verification passed.'
