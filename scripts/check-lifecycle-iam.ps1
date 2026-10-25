param([Parameter(Mandatory)][string]$BootstrapOutputsFile)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'iam-contract.ps1')
$identity = aws sts get-caller-identity --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $identity.Account -ne '058114477594') { throw 'Unexpected IAM preflight account.' }
$outputs = Get-Content -LiteralPath $BootstrapOutputsFile -Raw | ConvertFrom-Json
$matrix = $outputs.lifecycle_iam_simulation_cases_json.value | ConvertFrom-Json
if (@($matrix.PSObject.Properties.Name | Sort-Object) -join ',' -ne 'dispatcher,executor,manager') {
  throw 'The complete reviewed lifecycle IAM matrix is required.'
}
foreach ($name in @('manager', 'executor', 'dispatcher')) {
  $group = $matrix.$name
  $suffix = if ($name -eq 'executor') { 'executor' } else { $name }
  if ($group.principalArn -ne "arn:aws:iam::058114477594:role/techx-automation-lifecycle-$suffix") { throw 'Unexpected simulated role.' }
  Invoke-TechxScopedSimulation -PrincipalArn $group.principalArn -Cases @($group.cases)
}
# Simulation omits service behavior, SCP/resource-policy effects and networking.
# A successful matrix is a preflight gate, never the live IAM acceptance result.
