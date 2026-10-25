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
foreach ($environment in @('environments/demo', 'environments/staging', 'bootstrap/automation')) {
  $environmentRoot = Join-Path $root $environment
  $previousDataDirectory = $env:TF_DATA_DIR
  try {
    $env:TF_DATA_DIR = Join-Path $root ".terraform-offline/$environment"
    terraform "-chdir=$environmentRoot" init -backend=false -input=false
    if ($LASTEXITCODE -ne 0) { throw "terraform init failed for $environment." }
    terraform "-chdir=$environmentRoot" validate
    if ($LASTEXITCODE -ne 0) { throw "terraform validate failed for $environment." }
    if ($environment -eq 'bootstrap/automation') {
      terraform "-chdir=$environmentRoot" test -no-color
      if ($LASTEXITCODE -ne 0) { throw 'Automation control-plane Terraform mock plans failed.' }
    }
  } finally {
    if ($null -eq $previousDataDirectory) { Remove-Item Env:TF_DATA_DIR -ErrorAction SilentlyContinue }
    else { $env:TF_DATA_DIR = $previousDataDirectory }
  }
}
python (Join-Path $PSScriptRoot 'test-public-dns.py')
if ($LASTEXITCODE -ne 0) { throw 'Public DNS guard regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-automation-session.py')
if ($LASTEXITCODE -ne 0) { throw 'Automation session lease regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-leased-command.py')
if ($LASTEXITCODE -ne 0) { throw 'Leased command heartbeat/termination regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-cleanup-launcher.py')
if ($LASTEXITCODE -ne 0) { throw 'Independent cleanup launcher regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-cleanup-loader.py')
if ($LASTEXITCODE -ne 0) { throw 'Independent cleanup bundle loader regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-cleanup-execution.py')
if ($LASTEXITCODE -ne 0) { throw 'Cleanup fencing, ownership, network and inventory regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-run-cleanup.py')
if ($LASTEXITCODE -ne 0) { throw 'Cleanup worker ordering and credential-isolation regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-automation-inputs.py')
if ($LASTEXITCODE -ne 0) { throw 'Automation deployment input contract regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-bootstrap-assets.py')
if ($LASTEXITCODE -ne 0) { throw 'Cold bootstrap certificate/Secret/PKI regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-certificate-recovery.py')
if ($LASTEXITCODE -ne 0) { throw 'Certificate intent recovery/completion regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-secret-recovery.py')
if ($LASTEXITCODE -ne 0) { throw 'Secret intent recovery/completion regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-bootstrap-publish.py')
if ($LASTEXITCODE -ne 0) { throw 'Cold publish source/CI/lease/artifact regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-bootstrap-proposal.py')
if ($LASTEXITCODE -ne 0) { throw 'Cold chart proposal baseline/manifest/ref/recovery tests failed.' }
python (Join-Path $PSScriptRoot 'test-chart-selection.py')
if ($LASTEXITCODE -ne 0) { throw 'Protected chart PR/CI/tree/manifest selection tests failed.' }
python (Join-Path $PSScriptRoot 'test-gitops-bootstrap.py')
if ($LASTEXITCODE -ne 0) { throw 'GitOps bootstrap verification failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-coordinator.py')
if ($LASTEXITCODE -ne 0) { throw 'Lifecycle startup, cleanup-arming and uncertain-response regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-admission.py')
if ($LASTEXITCODE -ne 0) { throw 'Human-reviewed startup, source/backend/window and inventory admission tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-resume.py')
if ($LASTEXITCODE -ne 0) { throw 'Approved lifecycle resume and typed manager-loader regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-dispatch.py')
if ($LASTEXITCODE -ne 0) { throw 'Lifecycle continuation, manager recovery and single-consumption dispatch tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-continuation.py')
if ($LASTEXITCODE -ne 0) { throw 'Protected lifecycle continuation and bounded same-build observation tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-destroy.py')
if ($LASTEXITCODE -ne 0) { throw 'Independent cleanup dispatch, stale-session and protected inventory evidence tests failed.' }
python (Join-Path $PSScriptRoot 'test-chart-reuse.py')
if ($LASTEXITCODE -ne 0) { throw 'Zero-diff chart proposal, human-reviewed reuse and immutable selection tests failed.' }
python (Join-Path $PSScriptRoot 'test-workflow-handoff.py')
if ($LASTEXITCODE -ne 0) { throw 'Protected GitHub workflow handoff, uncertain-call recovery and child run admission tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-orchestration.py')
if ($LASTEXITCODE -ne 0) { throw 'Automatic lifecycle observation, review consumption and callback binding tests failed.' }
python (Join-Path $PSScriptRoot 'test-workload-state.py')
if ($LASTEXITCODE -ne 0) { throw 'Empty workload state initialization and lineage regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-deployment-runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Deployment runtime identity, backend, input and saved-plan observation tests failed.' }
python (Join-Path $PSScriptRoot 'test-deployment-manager.py')
if ($LASTEXITCODE -ne 0) { throw 'SSM task dispatch, single-consumer handoff and terminal observation tests failed.' }
python (Join-Path $PSScriptRoot 'test-deployment-executor.py')
if ($LASTEXITCODE -ne 0) { throw 'Executor launch, placement, readiness and uncertain-response tests failed.' }
python (Join-Path $PSScriptRoot 'test-deployment-task-loader.py')
if ($LASTEXITCODE -ne 0) { throw 'Immutable EC2 task admission, result and private diagnostic tests failed.' }
python (Join-Path $PSScriptRoot 'test-plan-approval.py')
if ($LASTEXITCODE -ne 0) { throw 'Saved-plan approval context and expiry regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-plan-review-bridge.py')
if ($LASTEXITCODE -ne 0) { throw 'Private plan review storage and GitHub approval bridge regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-lifecycle-stage.py')
if ($LASTEXITCODE -ne 0) { throw 'Durable lifecycle stage consumption and uncertain-apply regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-cleanup-schedule.py')
if ($LASTEXITCODE -ne 0) { throw 'Independent cleanup schedule regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-state-presence.py')
if ($LASTEXITCODE -ne 0) { throw 'Remote-state absence/failure regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-automation-bundle.py')
if ($LASTEXITCODE -ne 0) { throw 'Immutable source bundle regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-automation-tools.py')
if ($LASTEXITCODE -ne 0) { throw 'Pinned automation toolchain regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-executor-image.py')
if ($LASTEXITCODE -ne 0) { throw 'Executor image packaging/hash parity regression tests failed.' }
python (Join-Path $PSScriptRoot 'test-cloudflare-dns.py')
if ($LASTEXITCODE -ne 0) { throw 'Cloudflare DNS ownership regression tests failed.' }
& (Join-Path $PSScriptRoot 'test-init-backend.ps1')
& (Join-Path $PSScriptRoot 'test-iam-contract.ps1')
& (Join-Path $PSScriptRoot 'test-lifecycle-iam-preflight.ps1')
& (Join-Path $PSScriptRoot 'test-teardown-contract.ps1')
& (Join-Path $PSScriptRoot 'test-certificate-cleanup.ps1')
python (Join-Path $PSScriptRoot 'static-test.py')
if ($LASTEXITCODE -ne 0) { throw 'Terraform static assertions failed.' }
python (Join-Path $PSScriptRoot 'test-review-plan.py')
if ($LASTEXITCODE -ne 0) { throw 'Saved-plan review regression tests failed.' }
& (Join-Path $PSScriptRoot 'test-inventory.ps1')
& (Join-Path $PSScriptRoot 'test-cost-estimate.ps1')
& (Join-Path $PSScriptRoot 'cost-estimate.ps1') -Hours 12 | Out-Null
& (Join-Path $PSScriptRoot 'security-scan.ps1')

foreach ($script in @(
    'apply-reviewed-plan.ps1', 'create-reviewed-plan.ps1', 'preflight.ps1',
    'domain-vpn-inventory.ps1', 'destroy-domain-vpn-environment.ps1',
    'new-client-vpn-pki.ps1', 'prepare-domain-certificates.ps1',
    'export-client-vpn-profile.ps1', 'suspend-environment.ps1',
    'resume-environment.ps1', 'deploy-reviewed-environment.ps1', 'check-lifecycle-iam.ps1'
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
