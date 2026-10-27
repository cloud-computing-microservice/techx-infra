param([Parameter(Mandatory)][string]$BootstrapOutputsFile)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'iam-contract.ps1')
$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $identity.Account -ne '058114477594') { throw 'Unexpected IAM preflight account.' }
$outputs = Get-Content -LiteralPath $BootstrapOutputsFile -Raw | ConvertFrom-Json
$matrix = $outputs.lifecycle_iam_simulation_cases_json.value | ConvertFrom-Json
$roles = @{
  manager = 'techx-automation-lifecycle-manager'
  executor = 'techx-automation-lifecycle-executor'
  dispatcher = 'techx-automation-lifecycle-dispatcher'
  public_acceptance = 'techx-automation-public-acceptance'
  publisher = 'techx-automation-publisher'
}
$actualRoles = @($matrix.PSObject.Properties.Name | Sort-Object) -join ','
$expectedRoles = @($roles.Keys | Sort-Object) -join ','
if ($actualRoles -ne $expectedRoles) {
  throw 'The complete reviewed lifecycle IAM matrix is required.'
}
foreach ($name in @($roles.Keys | Sort-Object)) {
  $group = $matrix.$name
  if ($group.principalArn -ne "arn:aws:iam::058114477594:role/$($roles[$name])") { throw 'Unexpected simulated role.' }
  Invoke-TechxScopedSimulation -PrincipalArn $group.principalArn -Cases @($group.cases)
}
# Simulation omits service behavior, SCP/resource-policy effects and networking.
# A successful matrix is a preflight gate, never the live IAM acceptance result.
