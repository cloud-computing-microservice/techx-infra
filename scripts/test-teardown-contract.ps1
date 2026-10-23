$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'teardown-contract.ps1')
$partial = Get-TechxTeardownStrategy $false @('module.vpc.aws_vpc.this', 'module.eks.aws_iam_role.cluster')
if ($partial.EdgePresent -or $partial.ConfigureKubernetes -or $partial.OrphanedHelm.Count) {
  throw 'Partial foundation must not run a create-producing edge plan or require Kubernetes.'
}
$creating = Get-TechxTeardownStrategy $true @('module.eks.aws_eks_cluster.this') 'CREATING'
if ($creating.ConfigureKubernetes) { throw 'An incomplete foundation without Helm must not require a usable Kubernetes API.' }
$orphan = Get-TechxTeardownStrategy $false @('module.argocd.helm_release.this')
if ($orphan.OrphanedHelm.Count -ne 1) { throw 'Known Helm state must be classified for confirmed absent clusters.' }
$active = Get-TechxTeardownStrategy $true @('module.argocd.helm_release.this', 'module.cloudfront.aws_cloudfront_distribution.this[0]')
if (-not $active.EdgePresent -or -not $active.ConfigureKubernetes -or $active.OrphanedHelm.Count) {
  throw 'Active clusters must retain Helm state and remove edge before workloads.'
}
$rejected = $false
try { Get-TechxTeardownStrategy $false @('module.other.helm_release.unexpected') } catch { $rejected = $true }
if (-not $rejected) { throw 'Unknown Helm resources must not be removed from state.' }
Write-Host 'Partial-foundation, active-cluster and orphaned-Helm cleanup classification passed.'
