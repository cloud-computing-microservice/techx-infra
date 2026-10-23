function Get-TechxTeardownStrategy([bool]$ClusterPresent, [string[]]$Addresses, [string]$ClusterStatus = 'ACTIVE') {
  $helm = @($Addresses | Where-Object { $_ -match '\.helm_release\.' })
  $knownHelm = @('module.argocd.helm_release.this', 'module.aws_load_balancer_controller.helm_release.this')
  if (@($helm | Where-Object { $_ -notin $knownHelm }).Count -gt 0) {
    throw 'Unexpected Helm state is outside the cleanup contract.'
  }
  if ($ClusterPresent -and $ClusterStatus -ne 'ACTIVE' -and $helm.Count -gt 0) {
    throw 'Existing Helm releases require a usable control plane; retry after the EKS transition completes.'
  }
  return @{
    EdgePresent = @($Addresses | Where-Object { $_ -match '^module\.(cloudfront|private_dns|client_vpn)\.' }).Count -gt 0
    ConfigureKubernetes = $ClusterPresent -and $ClusterStatus -eq 'ACTIVE'
    OrphanedHelm = if ($ClusterPresent) { @() } else { $helm }
  }
}
