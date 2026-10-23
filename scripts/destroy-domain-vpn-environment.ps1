param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [Parameter(Mandatory)][ValidatePattern('^[0-9]{12}$')][string]$ExpectedAccountId,
  [Parameter(Mandatory)][switch]$ConfirmDestroy,
  [string]$Region = 'us-east-1',
  [string]$PrivateVarFile = '',
  [ValidateRange(300, 3600)][int]$TimeoutSeconds = 1800
)

$ErrorActionPreference = 'Stop'
if (-not $ConfirmDestroy) { throw 'ConfirmDestroy is required.' }

$root = Split-Path -Parent $PSScriptRoot
$environmentRoot = Join-Path $root "environments/$Environment"
$privateVars = if ($PrivateVarFile) { $PrivateVarFile } else { Join-Path $environmentRoot 'domain-vpn.private.tfvars' }
$helmHome = Join-Path $root '.terraform-helm'
$resourcePrefix = "techx-$Environment"
$certificateStatePath = Join-Path $root "certificate-state-$Environment.private.json"
$planRoot = Join-Path $root '.plans'
$kubeconfigPath = Join-Path ([IO.Path]::GetTempPath()) "techx-teardown-$Environment-$PID.kubeconfig"
$previousHelmConfig = $env:HELM_REPOSITORY_CONFIG
$previousHelmCache = $env:HELM_REPOSITORY_CACHE
if (-not (Test-Path -LiteralPath $privateVars)) { throw "Private variable file is missing: $privateVars" }

$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $identity.Account -ne $ExpectedAccountId) {
  throw 'Current AWS identity does not match ExpectedAccountId.'
}

# Absence must be established by a successful list call, never a swallowed error.
$clusterNames = @(aws eks list-clusters --region $Region --query clusters --output json | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw 'Unable to determine whether the staging cluster exists.' }
$cluster = $null
$context = ''
$cidrValues = @('203.0.113.254/32') # Valid placeholder only for deletion when no cluster exists.
if ($clusterNames -contains $resourcePrefix) {
  $cluster = aws eks describe-cluster --region $Region --name $resourcePrefix --output json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Unable to read the live EKS endpoint configuration.' }
  $context = $cluster.cluster.arn
  $cidrValues = @($cluster.cluster.resourcesVpcConfig.publicAccessCidrs)
}
$cidrs = $cidrValues | ConvertTo-Json -Compress -AsArray
$planVariables = @("-var-file=$privateVars", '-var=enable_domain_vpn_foundation=true',
  '-var=enable_domain_vpn_edge=false', "-var=public_access_cidrs=$cidrs")
New-Item -ItemType Directory -Force -Path $planRoot | Out-Null

function Invoke-ReviewedTeardown([string]$Stage, [switch]$Destroy) {
  $planPath = Join-Path $planRoot "$Environment-$Stage.tfplan"
  $arguments = @("-chdir=$environmentRoot", 'plan', '-input=false', '-no-color', '-lock-timeout=60s', "-out=$planPath")
  if ($Destroy) { $arguments += '-destroy' }
  & terraform @arguments @planVariables
  if ($LASTEXITCODE -ne 0) { throw "$Stage plan failed." }
  $planJson = terraform "-chdir=$environmentRoot" show -json $planPath
  if ($LASTEXITCODE -ne 0) { throw "Unable to inspect $Stage plan." }
  $planJson | python (Join-Path $PSScriptRoot 'review-plan.py') $Stage
  if ($LASTEXITCODE -ne 0) { throw "$Stage scope review failed; nothing was applied." }
  Write-Host "Applying reviewed saved plan SHA-256: $((Get-FileHash -LiteralPath $planPath -Algorithm SHA256).Hash)"
  terraform "-chdir=$environmentRoot" apply -input=false -no-color '-lock-timeout=60s' $planPath
  if ($LASTEXITCODE -ne 0) { throw "$Stage apply failed." }
}

python (Join-Path $PSScriptRoot 'public-dns.py')
if ($LASTEXITCODE -ne 0) { throw 'Public DNS is live or could not be verified; teardown refused.' }

New-Item -ItemType Directory -Force -Path (Join-Path $helmHome 'repository') | Out-Null
$env:HELM_REPOSITORY_CONFIG = Join-Path $helmHome 'repositories.yaml'
$env:HELM_REPOSITORY_CACHE = Join-Path $helmHome 'repository'

try {
  helm repo add eks https://aws.github.io/eks-charts --force-update | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to configure the EKS Helm repository.' }
  helm repo add argo https://argoproj.github.io/argo-helm --force-update | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to configure the Argo Helm repository.' }
  helm repo update eks argo | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to update Helm repositories.' }
  & (Join-Path $PSScriptRoot 'init-backend.ps1') -Environment $Environment
  if ($LASTEXITCODE -ne 0) { throw 'terraform init failed.' }

  . (Join-Path $PSScriptRoot 'teardown-contract.ps1')
  $presence = python (Join-Path $PSScriptRoot 'state-presence.py') --bucket $env:TECHX_STATE_BUCKET | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Remote state presence is unknown; cleanup refused.' }
  $stateAddresses = @()
  if ($presence.exists) {
    $stateAddresses = @(terraform "-chdir=$environmentRoot" state list)
    if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect remote state before cleanup.' }
  }
  $strategy = Get-TechxTeardownStrategy ($null -ne $cluster) $stateAddresses ([string]$cluster.cluster.status)
  if ($strategy.OrphanedHelm.Count -gt 0) {
    # The control plane is confirmed absent; these releases no longer have a server.
    # Preserve the old state privately and remove only the two known orphan addresses.
    $confirmed = @(aws eks list-clusters --region $Region --query clusters --output json | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $confirmed -contains $resourcePrefix) { throw 'Cluster absence could not be reconfirmed; state was not changed.' }
    $snapshot = terraform "-chdir=$environmentRoot" state pull
    if ($LASTEXITCODE -ne 0) { throw 'Unable to back up orphaned Helm state.' }
    $snapshot | Set-Content -LiteralPath (Join-Path $planRoot "state-before-orphan-removal-$([guid]::NewGuid()).private.json") -Encoding utf8
    foreach ($address in $strategy.OrphanedHelm) {
      terraform "-chdir=$environmentRoot" state rm -lock-timeout=60s $address
      if ($LASTEXITCODE -ne 0) { throw 'Unable to remove a confirmed orphaned Helm address.' }
    }
  }
  if ($strategy.EdgePresent) { Invoke-ReviewedTeardown -Stage 'teardown-edge' }

  if ($strategy.ConfigureKubernetes) {
    aws eks update-kubeconfig --region $Region --name $resourcePrefix --kubeconfig $kubeconfigPath | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to configure the selected cluster for workload cleanup.' }
    $authorized = $false
    foreach ($attempt in 1..12) {
      kubectl --kubeconfig $kubeconfigPath --context $context --request-timeout=10s auth can-i delete deployments.apps --namespace argocd --quiet
      if ($LASTEXITCODE -eq 0) { $authorized = $true; break }
      Start-Sleep -Seconds 5
    }
    if (-not $authorized) { throw 'Cleanup Kubernetes access did not become effective.' }
    kubectl --kubeconfig $kubeconfigPath --context $context -n argocd delete application $resourcePrefix --cascade=foreground --ignore-not-found --wait=true --timeout="${TimeoutSeconds}s"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to remove the workload Application.' }
    kubectl --kubeconfig $kubeconfigPath --context $context -n argocd delete ingress --all --ignore-not-found --wait=true --timeout="${TimeoutSeconds}s"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to remove the Argo CD Ingress.' }
  }

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $loadBalancers = @(aws resourcegroupstaggingapi get-resources --region $Region `
        --tag-filters Key=Project,Values=techx "Key=Environment,Values=$Environment" `
        --resource-type-filters elasticloadbalancing:loadbalancer `
        --query 'ResourceTagMappingList[].ResourceARN' --output json | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw 'Unable to inventory environment-scoped TechX load balancers.' }
    if ($loadBalancers.Count -eq 0) { break }
    Start-Sleep -Seconds 10
  } while ((Get-Date) -lt $deadline)
  if ($loadBalancers.Count -gt 0) { throw "A $Environment TechX ALB still exists; foundation destroy was not started." }

  # Remove Helm releases while cleanup EKS access still exists. Otherwise a
  # destroy graph could revoke this role before the Helm provider finishes.
  if ($strategy.ConfigureKubernetes) {
    helm uninstall argocd --namespace argocd --kubeconfig $kubeconfigPath --kube-context $context --ignore-not-found --wait --timeout "${TimeoutSeconds}s"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to uninstall Argo CD before removing cleanup access.' }
    helm uninstall aws-load-balancer-controller --namespace kube-system --kubeconfig $kubeconfigPath --kube-context $context --ignore-not-found --wait --timeout "${TimeoutSeconds}s"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to uninstall the ALB controller before removing cleanup access.' }
  }

  if ($presence.managedCount -gt 0) { Invoke-ReviewedTeardown -Stage 'teardown-foundation' -Destroy }

  $certificateArns = @()
  if (Test-Path -LiteralPath $certificateStatePath) {
    $certificateState = Get-Content -LiteralPath $certificateStatePath -Raw | ConvertFrom-Json
    if ($certificateState.environment -ne $Environment -or $certificateState.accountId -ne $ExpectedAccountId -or $certificateState.region -ne $Region) {
      throw 'Certificate state does not match the selected environment, account, and region.'
    }
    $certificateArns += @(
      $certificateState.publicCertificateArn,
      $certificateState.clientVpnServerCertificateArn,
      $certificateState.clientVpnRootCertificateArn
    ) | Where-Object { $_ }
  }
  $publicCertificates = aws acm list-certificates --region $Region --output json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Unable to inventory ACM certificates.' }
  foreach ($certificate in @($publicCertificates.CertificateSummaryList | Where-Object { $_.DomainName -eq 'shop.dinhminhkhoa.id.vn' })) {
    $certificateTags = aws acm list-tags-for-certificate --region $Region --certificate-arn $certificate.CertificateArn --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect ACM certificate tags for $($certificate.CertificateArn)." }
    $projectTag = @($certificateTags.Tags | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1
    $environmentTag = @($certificateTags.Tags | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1
    if ($projectTag -and $environmentTag) { $certificateArns += $certificate.CertificateArn }
  }
  $existingCertificateArns = @($publicCertificates.CertificateSummaryList.CertificateArn)
  foreach ($certificateArn in @($certificateArns | Sort-Object -Unique)) {
    if ($certificateArn -notmatch "^arn:aws:acm:${Region}:$ExpectedAccountId`:certificate/") {
      throw "Refusing to delete an ACM certificate outside the approved account/region: $certificateArn"
    }
    if ($existingCertificateArns -notcontains $certificateArn) { continue } # Retry after an earlier successful deletion.
    $certificateTags = aws acm list-tags-for-certificate --region $Region --certificate-arn $certificateArn --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect ACM certificate tags for $certificateArn." }
    $projectTag = @($certificateTags.Tags | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1
    $environmentTag = @($certificateTags.Tags | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1
    if (-not ($projectTag -and $environmentTag)) {
      throw "Refusing to delete ACM certificate without matching project and environment tags: $certificateArn"
    }
    aws acm delete-certificate --region $Region --certificate-arn $certificateArn
    if ($LASTEXITCODE -ne 0) { throw "Unable to delete ACM certificate $certificateArn." }
  }
  if (Test-Path -LiteralPath $certificateStatePath) { [IO.File]::Delete($certificateStatePath) }

  if ($presence.exists) {
    $remainingState = @(terraform "-chdir=$environmentRoot" state list)
    if ($LASTEXITCODE -ne 0 -or $remainingState.Count -gt 0) { throw 'Terraform workload state is not confirmed empty.' }
  }
  . (Join-Path $PSScriptRoot 'inventory-contract.ps1')
  $inventoryDeadline = (Get-Date).AddSeconds($TimeoutSeconds)
  do {
    $inventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') -Environment $Environment -Region $Region | ConvertFrom-Json
    try {
      Assert-TechxInventoryEmpty $inventory $ExpectedAccountId $Environment $Region
      break
    } catch {
      if ($_.Exception.Message -notlike 'Inventory is not empty:*' -or (Get-Date) -ge $inventoryDeadline) { throw }
      Start-Sleep -Seconds 10
    }
  } while ($true)

  $inventory | ConvertTo-Json -Depth 8
}
finally {
  if ($null -eq $previousHelmConfig) { Remove-Item Env:HELM_REPOSITORY_CONFIG -ErrorAction SilentlyContinue }
  else { $env:HELM_REPOSITORY_CONFIG = $previousHelmConfig }
  if ($null -eq $previousHelmCache) { Remove-Item Env:HELM_REPOSITORY_CACHE -ErrorAction SilentlyContinue }
  else { $env:HELM_REPOSITORY_CACHE = $previousHelmCache }
}
