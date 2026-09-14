param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [string]$Region = 'us-east-1',
  [string]$ClusterName = '',
  [string]$NodeGroupName = '',
  [string]$Application = '',
  [Parameter(Mandatory)][datetimeoffset]$RetentionDeadline,
  [ValidateRange(60, 1800)][int]$TimeoutSeconds = 900
)

$ErrorActionPreference = 'Stop'
if (-not $ClusterName) { $ClusterName = "techx-$Environment" }
if (-not $NodeGroupName) { $NodeGroupName = $Environment }
if (-not $Application) { $Application = "techx-$Environment" }
$namespace = "techx-$Environment"
if ($RetentionDeadline -le [datetimeoffset]::Now -or $RetentionDeadline -gt [datetimeoffset]::Now.AddDays(7)) {
  throw 'RetentionDeadline must be in the future and no more than seven days away.'
}

$inventory = & (Join-Path $PSScriptRoot 'domain-vpn-inventory.ps1') -Environment $Environment -Region $Region | ConvertFrom-Json
if ($inventory.environment -ne $Environment) { throw 'Inventory does not match the selected environment.' }
if ($inventory.clientVpn.Count -gt 0 -or $inventory.cloudFront.Count -gt 0 -or $inventory.privateHostedZones.Count -gt 0) {
  throw 'The domain/VPN profile cannot use baseline IDLE: paid edge resources would remain. Disconnect VPN, remove the public CNAME, then use destroy-domain-vpn-environment.ps1.'
}

aws eks describe-cluster --region $Region --name $ClusterName --output json | Out-Null
if ($LASTEXITCODE -ne 0) { throw "EKS cluster $ClusterName is unavailable." }
aws eks update-kubeconfig --region $Region --name $ClusterName | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to configure kubectl.' }

$applicationExists = kubectl -n argocd get application $Application --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the Argo CD Application.' }
if ($applicationExists) {
  kubectl -n argocd delete application $Application --cascade=foreground --wait=true --timeout="${TimeoutSeconds}s"
  if ($LASTEXITCODE -ne 0) { throw 'Argo CD Application removal failed.' }
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
  $ingress = kubectl -n $namespace get ingress --ignore-not-found -o name
  if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect workload Ingress resources.' }
  $loadBalancers = aws resourcegroupstaggingapi get-resources --region $Region `
    --tag-filters Key=Project,Values=techx "Key=Environment,Values=$Environment" `
    --resource-type-filters elasticloadbalancing:loadbalancer `
    --query 'ResourceTagMappingList[].ResourceARN' --output json | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect environment-scoped AWS load balancers.' }
  if (-not $ingress -and @($loadBalancers).Count -eq 0) { break }
  Start-Sleep -Seconds 10
} while ((Get-Date) -lt $deadline)
if ($ingress -or @($loadBalancers).Count -gt 0) { throw 'Ingress or TechX load balancer still exists; worker scale-down was not attempted.' }

aws eks update-nodegroup-config --region $Region --cluster-name $ClusterName --nodegroup-name $NodeGroupName --scaling-config minSize=0,maxSize=1,desiredSize=0 --output json | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Node group scale-down request failed.' }
aws eks wait nodegroup-active --region $Region --cluster-name $ClusterName --nodegroup-name $NodeGroupName
if ($LASTEXITCODE -ne 0) { throw 'Node group did not return to ACTIVE after scale-down.' }

$state = [ordered]@{
  state = 'IDLE'
  environment = $Environment
  region = $Region
  clusterName = $ClusterName
  nodeGroupName = $NodeGroupName
  suspendedAt = [datetimeoffset]::Now.ToString('o')
  retentionDeadline = $RetentionDeadline.ToString('o')
}
$state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) "environment-state-$Environment.private.json") -Encoding utf8
$state | ConvertTo-Json
