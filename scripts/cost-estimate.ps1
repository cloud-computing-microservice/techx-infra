param(
  [ValidateRange(1, 24)][int]$Hours = 12,
  [ValidateSet('baseline', 'domainVpn')][string]$Profile = 'domainVpn',
  [bool]$IncludeAutomation = $true
)

$ErrorActionPreference = 'Stop'
$hourly = [ordered]@{ eksControlPlane = 0.10; t3Medium = 0.042; publicIpv4Node = 0.005; alb = 0.0225; albLcu = 0.008 }
$fixed = [ordered]@{ ebs20GiBProrated = 20 * 0.08 * ($Hours / 730); ecrAndScanning = 0.25; cloudWatchLogs = 0.50; dynamodbOnDemandRequestsAndStorage = 1.00; dynamodbGatewayEndpointHourly = 0.00; dataTransfer = 1.00 }
if ($Profile -eq 'domainVpn') {
  $hourly.clientVpnAssociation = 0.10
  $hourly.oneClientVpnConnection = 0.05
  $hourly.clientVpnPublicIpv4 = 0.005
  $fixed.privateHostedZone = 0.50
  $fixed.cloudFrontRequestsAndTransfer = 2.00
}
if ($IncludeAutomation) {
  # Conservative run-window allowance, not a guarantee against delayed cleanup.
  $fixed.automationBuildMinutes = ($Hours + 3) * 60 * 0.005
  $fixed.automationPrivateProbe = $Hours * (0.021 + 0.005) + (8 * 0.08 * $Hours / 730)
  $fixed.automationKmsMonthlyAllowance = 1.00
  $fixed.automationStateArtifactsSecretsAndLogs = 2.00
}
$usage = (($hourly.Values | Measure-Object -Sum).Sum * $Hours) + ($fixed.Values | Measure-Object -Sum).Sum
$contingency = [Math]::Max(5.00, $usage * 0.50)
$upper = [Math]::Ceiling(($usage + $contingency) * 100) / 100
if ($upper -gt 60) { throw "Estimated upper bound $upper USD exceeds the 60 USD apply gate." }
[pscustomobject]@{ region = 'us-east-1'; profile = $Profile; maximumHours = $Hours; includesAutomation = $IncludeAutomation; hourlyAssumptions = $hourly; fixedAllowances = $fixed; estimatedUsageUsd = [Math]::Round($usage, 2); contingencyUsd = [Math]::Round($contingency, 2); upperBoundUsd = $upper; applyGateUsd = 60; hardCapUsd = 80; approvedBudgetUsd = 80; alertsDoNotStopSpending = $true; pricingReferences = @('https://aws.amazon.com/ec2/instance-types/t3/', 'https://aws.amazon.com/codebuild/pricing/', 'https://aws.amazon.com/kms/pricing/') } | ConvertTo-Json -Depth 4
