param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [string]$Region = 'us-east-1',
  [string]$ClusterName = '',
  [string]$NodeGroupName = '',
  [ValidateRange(120, 1800)][int]$TimeoutSeconds = 900
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $ClusterName) { $ClusterName = "techx-$Environment" }
if (-not $NodeGroupName) { $NodeGroupName = $Environment }
$namespace = "techx-$Environment"
$secretName = "techx-$Environment-secrets"
$statePath = Join-Path $root "environment-state-$Environment.private.json"
if (-not (Test-Path -LiteralPath $statePath)) { throw 'The private environment state file is missing.' }
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
if ($state.environment -ne $Environment -or $state.clusterName -ne $ClusterName -or $state.nodeGroupName -ne $NodeGroupName) {
  throw 'The private environment state does not match the selected environment.'
}
if ($state.state -ne 'IDLE') { throw 'The environment is not recorded as IDLE.' }
if ([datetimeoffset]::Parse($state.retentionDeadline) -le [datetimeoffset]::Now) { throw 'The retention deadline has expired; do not resume before reviewing cost and teardown.' }

aws eks update-nodegroup-config --region $Region --cluster-name $ClusterName --nodegroup-name $NodeGroupName --scaling-config minSize=0,maxSize=1,desiredSize=1 --output json | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Node group scale-up request failed.' }
aws eks wait nodegroup-active --region $Region --cluster-name $ClusterName --nodegroup-name $NodeGroupName
if ($LASTEXITCODE -ne 0) { throw 'Node group did not return to ACTIVE after scale-up.' }
aws eks update-kubeconfig --region $Region --name $ClusterName | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to configure kubectl.' }
kubectl wait --for=condition=Ready nodes --all --timeout="${TimeoutSeconds}s"
if ($LASTEXITCODE -ne 0) { throw 'The worker node did not become Ready.' }

$secret = kubectl -n $namespace get secret $secretName --ignore-not-found -o name
if ($LASTEXITCODE -ne 0 -or -not $secret) { throw "Required Secret $secretName is missing; restore it before applying the Argo CD Application." }
$applicationPath = Join-Path (Split-Path -Parent $root) "techx-chart/gitops/clusters/$Environment/application.yaml"
kubectl apply -f $applicationPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to apply the Argo CD Application.' }

$state.state = 'ACTIVE'
$state | Add-Member -NotePropertyName resumedAt -NotePropertyValue ([datetimeoffset]::Now.ToString('o')) -Force
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
$state | ConvertTo-Json
