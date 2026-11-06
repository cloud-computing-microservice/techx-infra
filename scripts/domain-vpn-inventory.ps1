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

function Test-VpcOriginEmptyPage($List) {
  # Live ListVpcOrigins can omit IsTruncated/Marker on an empty first page.
  # Require its explicit positive empty envelope; never infer zero from a
  # partial/nonempty/paginated response or extend this exception to other APIs.
  if ($null -eq $List) { return $false }
  $fields = @($List.PSObject.Properties.Name)
  foreach ($required in @('MaxItems', 'Quantity', 'Items')) {
    if ($required -notin $fields) { return $false }
  }
  if (@($fields | Where-Object { $_ -notin @('MaxItems', 'Quantity', 'Items', 'Marker') }).Count -ne 0) { return $false }
  if (($List.MaxItems -isnot [int] -and $List.MaxItems -isnot [long]) -or $List.MaxItems -lt 1 -or $List.MaxItems -gt 100 -or
      ($List.Quantity -isnot [int] -and $List.Quantity -isnot [long]) -or $List.Quantity -ne 0 -or
      $List.Items -isnot [array] -or $List.Items.Count -ne 0) { return $false }
  return ('Marker' -notin $fields -or ($List.Marker -is [string] -and $List.Marker.Length -eq 0))
}

function Get-CloudFrontItems([string]$Operation, [string]$Field) {
  $marker = $null
  $seen = @{}
  for ($page = 0; $page -lt 20; $page++) {
    $arguments = @('cloudfront', $Operation, '--no-paginate')
    if ($marker) { $arguments += @('--marker', $marker) }
    $list = (Invoke-AwsJson $arguments).$Field
    $items = @($list.Items | Where-Object { $null -ne $_ })
    $emptyVpcPage = $Operation -eq 'list-vpc-origins' -and $page -eq 0 -and (Test-VpcOriginEmptyPage $list)
    if ($null -eq $list -or $list.Quantity -isnot [long] -and $list.Quantity -isnot [int] -or
        $list.Quantity -ne $items.Count -or
        ($Operation -ne 'list-functions' -and $list.IsTruncated -isnot [bool] -and -not $emptyVpcPage)) {
      throw "Incomplete CloudFront inventory: $Operation"
    }
    $items
    $marker = $list.NextMarker
    if ($Operation -ne 'list-functions' -and -not $emptyVpcPage -and $list.IsTruncated -ne [bool]$marker) {
      throw "Inconsistent CloudFront pagination: $Operation"
    }
    if (-not $marker) { return }
    if ($seen.ContainsKey($marker)) { throw "Repeated CloudFront inventory marker: $Operation" }
    $seen[$marker] = $true
  }
  throw "CloudFront inventory page limit exceeded: $Operation"
}

function Test-WorkloadCloudFrontTags([string]$Arn) {
  $tags = (Invoke-AwsJson @('cloudfront', 'list-tags-for-resource', '--resource', $Arn)).Tags
  if ($null -eq $tags) { throw 'CloudFront tags are unavailable.' }
  return (@($tags.Items | Where-Object { $_.Key -eq 'Project' -and $_.Value -eq 'techx' }).Count -eq 1 -and
    @($tags.Items | Where-Object { $_.Key -eq 'Environment' -and $_.Value -eq $Environment }).Count -eq 1)
}

$identity = Invoke-AwsJson @('sts', 'get-caller-identity')
$resourcePrefix = "techx-$Environment"
$tagFilters = @('Name=tag:Project,Values=techx', "Name=tag:Environment,Values=$Environment")
$instances = @((Invoke-AwsJson (@('ec2', 'describe-instances', '--region', $Region, '--filters') +
      $tagFilters + @('Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down'))).Reservations.Instances |
    Where-Object { $_.InstanceId } | ForEach-Object { @{ id = $_.InstanceId; state = $_.State.Name } })
$volumes = @((Invoke-AwsJson (@('ec2', 'describe-volumes', '--region', $Region, '--filters') + $tagFilters)).Volumes |
    Where-Object { $_.VolumeId } | ForEach-Object { $_.VolumeId })
$vpcs = @((Invoke-AwsJson (@('ec2', 'describe-vpcs', '--region', $Region, '--filters') + $tagFilters)).Vpcs |
    Where-Object { $_.VpcId } | ForEach-Object { $_.VpcId })
$addresses = @((Invoke-AwsJson (@('ec2', 'describe-addresses', '--region', $Region, '--filters') + $tagFilters)).Addresses |
    Where-Object { $_.AllocationId } | ForEach-Object { $_.AllocationId })
$allEndpoints = @((Invoke-AwsJson (@('ec2', 'describe-vpc-endpoints', '--region', $Region, '--filters') + $tagFilters)).VpcEndpoints |
    Where-Object { $_.VpcEndpointId } | ForEach-Object { $_.VpcEndpointId })
$iamRoles = @((Invoke-AwsJson @('iam', 'list-roles', '--query', "Roles[?starts_with(RoleName, '$resourcePrefix-')].RoleName")))
$iamPolicies = @((Invoke-AwsJson @('iam', 'list-policies', '--scope', 'Local', '--query', "Policies[?starts_with(PolicyName, '$resourcePrefix-')].Arn")))
$secrets = @((Invoke-AwsJson @('secretsmanager', 'list-secrets', '--region', $Region, '--include-planned-deletion',
      '--filters', "Key=name,Values=techx/$Environment/")).SecretList |
    Where-Object { $_.Name -like "techx/$Environment/*" } |
    ForEach-Object { @{ arn = $_.ARN; name = $_.Name; deletionDate = $_.DeletedDate } })
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

$distributions = @()
foreach ($distribution in @(Get-CloudFrontItems 'list-distributions' 'DistributionList')) {
  if (-not $distribution.Id) { throw 'Invalid CloudFront distribution identity.' }
  $arn = "arn:aws:cloudfront::$($identity.Account):distribution/$($distribution.Id)"
  if (Test-WorkloadCloudFrontTags $arn) {
    $distributions += @{ id = $distribution.Id; domainName = $distribution.DomainName; status = $distribution.Status; enabled = $distribution.Enabled }
  }
}
$origins = @()
foreach ($origin in @(Get-CloudFrontItems 'list-vpc-origins' 'VpcOriginList')) {
  if (-not $origin.Id -or $origin.Arn -notlike "arn:aws:cloudfront::$($identity.Account):vpcorigin/*") {
    throw 'Invalid CloudFront VPC origin identity.'
  }
  if (Test-WorkloadCloudFrontTags $origin.Arn) {
    $origins += @{ id = $origin.Id; arn = $origin.Arn; name = $origin.Name; status = $origin.Status }
  }
}
# Functions are not tagged by this module; their environment-specific names
# are the same ownership scope used by the Terraform/IAM contract.
$functions = @()
foreach ($function in @(Get-CloudFrontItems 'list-functions' 'FunctionList')) {
    if (-not $function.Name -or $function.FunctionMetadata.FunctionARN -cne "arn:aws:cloudfront::$($identity.Account):function/$($function.Name)") {
      throw 'Invalid workload CloudFront Function identity.'
    }
    if ($function.Name -clike "$resourcePrefix-*") {
      $functions += @{ name = $function.Name; arn = $function.FunctionMetadata.FunctionARN; stage = $function.FunctionMetadata.Stage }
    }
}
$logGroups = @()
foreach ($prefix in @("/aws/eks/$resourcePrefix/", "/aws/client-vpn/$resourcePrefix")) {
  $response = Invoke-AwsJson @('logs', 'describe-log-groups', '--region', $Region, '--log-group-name-prefix', $prefix)
  if ($null -eq $response.logGroups -or $response.logGroups -isnot [array] -or $response.nextToken) {
    throw 'CloudWatch Logs inventory is incomplete.'
  }
  if (@($response.logGroups | Where-Object { -not $_.logGroupName -or -not $_.arn }).Count) {
    throw 'CloudWatch Logs inventory has an invalid group identity.'
  }
  $logGroups += @($response.logGroups | Where-Object {
      $_.logGroupName -clike "/aws/eks/$resourcePrefix/*" -or
      $_.logGroupName -ceq "/aws/client-vpn/$resourcePrefix" -or
      $_.logGroupName -clike "/aws/client-vpn/$resourcePrefix-*"
    } | ForEach-Object { @{ name = $_.logGroupName; arn = $_.arn; storedBytes = $_.storedBytes } })
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
$repositories = @($taggedRepositories.ResourceTagMappingList | ForEach-Object {
    if ($_.ResourceARN) { ($_.ResourceARN -split ':repository/')[1] }
  } | Where-Object { $_ })
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
  ec2Instances = $instances
  ebsVolumes = $volumes
  vpcs = $vpcs
  publicAddresses = $addresses
  iamRoles = $iamRoles
  iamPolicies = $iamPolicies
  vpcEndpoints = $allEndpoints
  secrets = $secrets
  clusters = $clusters
  nodeGroups = $nodeGroups
  loadBalancerArns = $loadBalancers
  clientVpn = $vpn
  cloudFront = $distributions
  cloudFrontVpcOrigins = $origins
  cloudFrontFunctions = $functions
  cloudWatchLogGroups = $logGroups
  privateHostedZones = @($zones | ForEach-Object { @{ id = $_.Id; name = $_.Name } })
  ecrRepositories = $repositories
  dynamodbTables = $tables
  dynamodbVpcEndpoints = $vpcEndpoints
  orderApiIamRoles = $roles
  acmCertificates = $certificates
} | ConvertTo-Json -Depth 8
