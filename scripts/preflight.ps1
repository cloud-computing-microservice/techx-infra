param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [string]$Region = 'us-east-1',
  [string]$KubernetesVersion = '1.35',
  [ValidateCount(0, 4)][string[]]$PublicAccessCidrs = @(),
  [ValidateSet('foundation', 'recovery', 'edge', 'hardening')][string]$Stage = 'foundation'
)

$ErrorActionPreference = 'Stop'
if (@($PublicAccessCidrs | Where-Object { $_ -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}/(24|32)$' }).Count -gt 0) {
  throw 'PublicAccessCidrs must contain only IPv4 /24 or /32 CIDRs.'
}
$resourcePrefix = "techx-$Environment"
$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if (-not $identity.Account) { throw 'Unable to resolve AWS caller identity.' }
$ip = (Invoke-RestMethod -Uri 'https://checkip.amazonaws.com').Trim()
if ($ip -notmatch '^([0-9]{1,3}\.){3}[0-9]{1,3}$') { throw 'Unable to resolve caller public IPv4.' }
$effectivePublicAccessCidrs = if ($PublicAccessCidrs.Count -gt 0) { @($PublicAccessCidrs) } else { @("$ip/32") }

$versions = aws eks describe-cluster-versions --region $Region --no-include-all --output json | ConvertFrom-Json
$selected = $versions.clusterVersions | Where-Object { $_.clusterVersion -eq $KubernetesVersion -and $_.status -eq 'STANDARD_SUPPORT' }
if (-not $selected) { throw "EKS $KubernetesVersion is not in standard support in $Region." }
$zones = aws ec2 describe-availability-zones --region $Region --filters Name=state,Values=available --output json | ConvertFrom-Json
if ($zones.AvailabilityZones.Count -lt 2) { throw 'Fewer than two available AZs were returned.' }
$offerings = aws ec2 describe-instance-type-offerings --region $Region --location-type availability-zone --filters Name=instance-type,Values=t3.medium --output json | ConvertFrom-Json
if ($offerings.InstanceTypeOfferings.Count -lt 2) { throw 't3.medium is unavailable in at least two AZs.' }

$clusterQuota = aws service-quotas get-service-quota --service-code eks --quota-code L-1194D53C --region $Region --output json | ConvertFrom-Json
$vcpuQuota = aws service-quotas get-service-quota --service-code ec2 --quota-code L-1216C47A --region $Region --output json | ConvertFrom-Json
if ($clusterQuota.Quota.Value -lt 1 -or $vcpuQuota.Quota.Value -lt 2) { throw 'Required EKS or EC2 quota is unavailable.' }

$addons = @{}
foreach ($addon in @('vpc-cni', 'kube-proxy', 'coredns')) {
  $response = aws eks describe-addon-versions --region $Region --kubernetes-version $KubernetesVersion --addon-name $addon --output json | ConvertFrom-Json
  $default = $response.addons[0].addonVersions | Where-Object { $_.compatibilities[0].defaultVersion } | Select-Object -First 1
  if (-not $default.addonVersion) { throw "No compatible default version found for $addon." }
  $addons[$addon] = $default.addonVersion
}
$budgets = aws budgets describe-budgets --account-id $identity.Account --output json | ConvertFrom-Json

$conflicts = @()
$internalAlbArn = ''
$clusters = @(aws eks list-clusters --region $Region --query 'clusters' --output json | ConvertFrom-Json)
if ($Stage -eq 'foundation' -and $clusters -contains $resourcePrefix) { $conflicts += "EKS cluster $resourcePrefix" }
if ($Stage -eq 'recovery' -and $clusters -notcontains $resourcePrefix) { throw "Recovery stage requires the partially created $resourcePrefix foundation." }
if ($Stage -eq 'edge' -and $clusters -notcontains $resourcePrefix) { throw "Edge stage requires the $resourcePrefix foundation." }
if ($Stage -eq 'hardening' -and $clusters -notcontains $resourcePrefix) { throw "Hardening stage requires the deployed $resourcePrefix environment." }
$repositories = @(aws ecr describe-repositories --region $Region --query 'repositories[].repositoryName' --output json | ConvertFrom-Json)
foreach ($name in @('techx/frontend', 'techx/catalog', 'techx/order')) {
  if ($Stage -eq 'foundation' -and $repositories -contains $name) { $conflicts += "ECR repository $name" }
}
$roles = @(aws iam list-roles --query 'Roles[].RoleName' --output json | ConvertFrom-Json)
foreach ($name in @("$resourcePrefix-cluster", "$resourcePrefix-node", "$resourcePrefix-aws-load-balancer-controller")) {
  if ($Stage -eq 'foundation' -and $roles -contains $name) { $conflicts += "IAM role $name" }
}
$logGroups = @(aws logs describe-log-groups --region $Region --log-group-name-prefix "/aws/eks/$resourcePrefix/cluster" --query 'logGroups[].logGroupName' --output json | ConvertFrom-Json)
if ($Stage -eq 'foundation' -and $logGroups -contains "/aws/eks/$resourcePrefix/cluster") { $conflicts += "CloudWatch log group /aws/eks/$resourcePrefix/cluster" }
if ($Stage -eq 'foundation' -and @($budgets.Budgets.BudgetName) -contains "$resourcePrefix-hard-cap") { $conflicts += "AWS Budget $resourcePrefix-hard-cap" }
if ($Stage -in @('edge', 'hardening')) {
  $clientVpn = @(aws ec2 describe-client-vpn-endpoints --region $Region `
      --filters Name=tag:Project,Values=techx "Name=tag:Environment,Values=$Environment" `
      --query 'ClientVpnEndpoints[].ClientVpnEndpointId' --output json | ConvertFrom-Json)
  $privateZones = @(aws route53 list-hosted-zones --query 'HostedZones[?Config.PrivateZone==`true`].Name' --output json | ConvertFrom-Json)
  $distributions = aws cloudfront list-distributions --output json | ConvertFrom-Json
  $domainDistributions = @($distributions.DistributionList.Items | Where-Object { $_.Aliases.Items -contains 'shop.dinhminhkhoa.id.vn' })
  if ($Stage -eq 'edge') {
    if ($clientVpn.Count -gt 0) { $conflicts += 'Existing AWS Client VPN endpoint' }
    if ($privateZones -contains 'shop.dinhminhkhoa.id.vn.') { $conflicts += 'Private hosted zone shop.dinhminhkhoa.id.vn' }
    if ($domainDistributions.Count -gt 0) { $conflicts += 'CloudFront alias shop.dinhminhkhoa.id.vn' }
  }
  else {
    if ($clientVpn.Count -ne 1) { throw "Hardening stage requires one Client VPN endpoint; found $($clientVpn.Count)." }
    if (@($privateZones | Where-Object { $_ -eq 'shop.dinhminhkhoa.id.vn.' }).Count -ne 1) { throw 'Hardening stage requires the private hosted zone.' }
    if ($domainDistributions.Count -ne 1) { throw "Hardening stage requires one domain CloudFront distribution; found $($domainDistributions.Count)." }
  }
  $taggedAlbs = aws resourcegroupstaggingapi get-resources --region $Region `
    --tag-filters Key=Project,Values=techx "Key=Environment,Values=$Environment" `
    --resource-type-filters elasticloadbalancing:loadbalancer --output json | ConvertFrom-Json
  $internalAlbs = @($taggedAlbs.ResourceTagMappingList.ResourceARN)
  if ($internalAlbs.Count -ne 1) { throw "Edge stage requires exactly one environment-scoped internal ALB; found $($internalAlbs.Count)." }
  $internalAlbArn = $internalAlbs[0]
}
if ($conflicts.Count -gt 0) { throw "Pre-existing resource name conflicts found: $($conflicts -join ', ')" }

$requiredActions = if ($Stage -eq 'foundation') {
  @('eks:CreateCluster', 'eks:DeleteCluster', 'ec2:CreateVpc', 'ec2:RunInstances', 'iam:CreateRole', 'iam:CreatePolicy', 'ecr:CreateRepository', 'budgets:ModifyBudget', 'elasticloadbalancing:CreateLoadBalancer')
} elseif ($Stage -eq 'recovery') {
  @('eks:UpdateClusterConfig', 'elasticloadbalancing:CreateLoadBalancer')
} elseif ($Stage -eq 'edge') {
  @('cloudfront:CreateDistribution', 'cloudfront:CreateVpcOrigin', 'ec2:CreateClientVpnEndpoint', 'ec2:AssociateClientVpnTargetNetwork', 'route53:CreateHostedZone', 'route53:ChangeResourceRecordSets')
} else {
  @('cloudfront:UpdateDistribution', 'cloudfront:UpdateFunction', 'cloudfront:PublishFunction', 'eks:UpdateClusterConfig', 'iam:UpdateOpenIDConnectProviderThumbprint')
}
$simulation = aws iam simulate-principal-policy --policy-source-arn $identity.Arn --action-names $requiredActions --output json | ConvertFrom-Json
$denied = @($simulation.EvaluationResults | Where-Object { $_.EvalDecision -ne 'allowed' })
if ($denied.Count -gt 0) { throw "Permission simulation did not allow: $($denied.EvalActionName -join ', ')" }

[pscustomobject]@{
  accountId = $identity.Account; callerArn = $identity.Arn; region = $Region; environment = $Environment; stage = $Stage
  callerPublicIp = $ip; callerPublicCidrs = $effectivePublicAccessCidrs; kubernetesVersion = $selected.clusterVersion
  kubernetesStatus = $selected.status; standardSupportEnds = $selected.endOfStandardSupportDate
  availableZones = @($zones.AvailabilityZones.ZoneName); t3MediumZones = @($offerings.InstanceTypeOfferings.Location)
  eksClusterQuota = $clusterQuota.Quota.Value; standardOnDemandVcpuQuota = $vcpuQuota.Quota.Value
  compatibleDefaultAddonVersion = $addons
  existingBudgets = @($budgets.Budgets | ForEach-Object { @{ name = $_.BudgetName; amount = $_.BudgetLimit.Amount; unit = $_.BudgetLimit.Unit } })
  permissionSimulation = @($simulation.EvaluationResults | ForEach-Object { @{ action = $_.EvalActionName; decision = $_.EvalDecision } })
  resourceNameConflicts = $conflicts
  internalAlbArn = $internalAlbArn
} | ConvertTo-Json -Depth 6
