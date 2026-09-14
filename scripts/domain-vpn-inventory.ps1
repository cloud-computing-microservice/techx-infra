param(
  [ValidateSet('demo', 'staging')][string]$Environment = 'staging',
  [string]$Region = 'us-east-1',
  [string]$DomainName = 'shop.dinhminhkhoa.id.vn'
)

$ErrorActionPreference = 'Stop'

function Invoke-AwsJson([string[]]$Arguments) {
  $raw = & aws @Arguments --output json
  if ($LASTEXITCODE -ne 0) { throw "aws failed: $($Arguments -join ' ')" }
  return $raw | ConvertFrom-Json
}

$identity = Invoke-AwsJson @('sts', 'get-caller-identity')
$resourcePrefix = "techx-$Environment"
$clusters = @(@(Invoke-AwsJson @('eks', 'list-clusters', '--region', $Region)).clusters |
    Where-Object { $_ -eq $resourcePrefix })

$nodeGroups = @()
foreach ($cluster in $clusters) {
  $names = @(Invoke-AwsJson @('eks', 'list-nodegroups', '--region', $Region, '--cluster-name', $cluster)).nodegroups
  foreach ($name in $names) {
    $node = (Invoke-AwsJson @('eks', 'describe-nodegroup', '--region', $Region, '--cluster-name', $cluster, '--nodegroup-name', $name)).nodegroup
    $nodeGroups += [ordered]@{
      cluster = $cluster
      name = $name
      status = $node.status
      desired = $node.scalingConfig.desiredSize
      minimum = $node.scalingConfig.minSize
      maximum = $node.scalingConfig.maxSize
    }
  }
}

$taggedLoadBalancers = Invoke-AwsJson @(
  'resourcegroupstaggingapi', 'get-resources', '--region', $Region,
  '--tag-filters', 'Key=Project,Values=techx', "Key=Environment,Values=$Environment",
  '--resource-type-filters', 'elasticloadbalancing:loadbalancer'
)
$loadBalancers = @($taggedLoadBalancers.ResourceTagMappingList | ForEach-Object { $_.ResourceARN } | Where-Object { $_ })

$vpnEndpoints = @((Invoke-AwsJson @(
      'ec2', 'describe-client-vpn-endpoints', '--region', $Region,
      '--filters', 'Name=tag:Project,Values=techx', "Name=tag:Environment,Values=$Environment"
    )).ClientVpnEndpoints)
$vpn = @()
foreach ($endpoint in $vpnEndpoints) {
  $associations = @((Invoke-AwsJson @(
        'ec2', 'describe-client-vpn-target-networks', '--region', $Region,
        '--client-vpn-endpoint-id', $endpoint.ClientVpnEndpointId
      )).ClientVpnTargetNetworks)
  $connections = @((Invoke-AwsJson @(
        'ec2', 'describe-client-vpn-connections', '--region', $Region,
        '--client-vpn-endpoint-id', $endpoint.ClientVpnEndpointId
      )).Connections | Where-Object { $_.Status.Code -eq 'active' })
  $vpn += [ordered]@{
    id = $endpoint.ClientVpnEndpointId
    status = $endpoint.Status.Code
    associations = @($associations | ForEach-Object { @{ id = $_.AssociationId; status = $_.Status.Code; subnetId = $_.TargetNetworkId } })
    activeConnections = $connections.Count
  }
}

$distributionList = Invoke-AwsJson @('cloudfront', 'list-distributions')
$distributions = @()
foreach ($distribution in @($distributionList.DistributionList.Items | Where-Object { $_.Aliases.Items -contains $DomainName })) {
  $arn = "arn:aws:cloudfront::$($identity.Account):distribution/$($distribution.Id)"
  $tags = (Invoke-AwsJson @('cloudfront', 'list-tags-for-resource', '--resource', $arn)).Tags.Items
  $projectTag = @($tags | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1
  $environmentTag = @($tags | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1
  if ($projectTag -and $environmentTag) {
    $distributions += @{ id = $distribution.Id; domainName = $distribution.DomainName; status = $distribution.Status; enabled = $distribution.Enabled }
  }
}
$zones = @()
foreach ($zone in @((Invoke-AwsJson @('route53', 'list-hosted-zones-by-name', '--dns-name', $DomainName)).HostedZones |
    Where-Object { $_.Name.TrimEnd('.') -eq $DomainName -and $_.Config.PrivateZone })) {
  $zoneId = $zone.Id -replace '^/hostedzone/', ''
  $tags = (Invoke-AwsJson @('route53', 'list-tags-for-resource', '--resource-type', 'hostedzone', '--resource-id', $zoneId)).ResourceTagSet.Tags
  $projectTag = @($tags | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1
  $environmentTag = @($tags | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1
  if ($projectTag -and $environmentTag) { $zones += $zone }
}
$taggedRepositories = Invoke-AwsJson @(
  'resourcegroupstaggingapi', 'get-resources', '--region', $Region,
  '--tag-filters', 'Key=Project,Values=techx', "Key=Environment,Values=$Environment",
  '--resource-type-filters', 'ecr:repository'
)
$repositories = @($taggedRepositories.ResourceTagMappingList.ResourceARN | ForEach-Object { ($_ -split ':repository/')[1] })
$taggedTables = Invoke-AwsJson @(
  'resourcegroupstaggingapi', 'get-resources', '--region', $Region,
  '--tag-filters', 'Key=Project,Values=techx', "Key=Environment,Values=$Environment",
  '--resource-type-filters', 'dynamodb:table'
)
$tables = @($taggedTables.ResourceTagMappingList | ForEach-Object { ($_.ResourceARN -split ':table/')[1] } | Where-Object { $_ })
$vpcEndpoints = @((Invoke-AwsJson @(
      'ec2', 'describe-vpc-endpoints', '--region', $Region,
      '--filters', 'Name=tag:Project,Values=techx', "Name=tag:Environment,Values=$Environment",
      'Name=service-name,Values=com.amazonaws.us-east-1.dynamodb'
    )).VpcEndpoints | ForEach-Object { $_.VpcEndpointId })
$roleName = "$resourcePrefix-order-api"
$roles = @((Invoke-AwsJson @(
      'iam', 'list-roles', '--query', "Roles[?RoleName=='$roleName'].RoleName"
    )))
$certificateTags = Invoke-AwsJson @(
  'resourcegroupstaggingapi', 'get-resources', '--region', $Region,
  '--tag-filters', 'Key=Project,Values=techx', "Key=Environment,Values=$Environment",
  '--resource-type-filters', 'acm:certificate'
)
$certificates = @()
foreach ($mapping in @($certificateTags.ResourceTagMappingList)) {
  $certificate = (Invoke-AwsJson @('acm', 'describe-certificate', '--region', $Region, '--certificate-arn', $mapping.ResourceARN)).Certificate
  $role = @($mapping.Tags | Where-Object { $_.Key -eq 'Role' } | Select-Object -First 1).Value
  $certificates += @{ arn = $mapping.ResourceARN; domainName = $certificate.DomainName; status = $certificate.Status; role = $role }
}

[ordered]@{
  checkedAt = [datetimeoffset]::Now.ToString('o')
  accountId = $identity.Account
  region = $Region
  environment = $Environment
  clusters = $clusters
  nodeGroups = $nodeGroups
  loadBalancerArns = $loadBalancers
  clientVpn = $vpn
  cloudFront = $distributions
  privateHostedZones = @($zones | ForEach-Object { @{ id = $_.Id; name = $_.Name } })
  ecrRepositories = $repositories
  dynamodbTables = $tables
  dynamodbVpcEndpoints = $vpcEndpoints
  orderApiIamRoles = $roles
  acmCertificates = $certificates
} | ConvertTo-Json -Depth 8
