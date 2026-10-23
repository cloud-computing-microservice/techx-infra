function Assert-TechxInventoryEmpty([object]$Inventory, [string]$ExpectedAccountId, [string]$Environment, [string]$Region) {
  if ($Inventory.accountId -ne $ExpectedAccountId -or $Inventory.environment -ne $Environment -or $Inventory.region -ne $Region) {
    throw 'Inventory does not match the expected account/environment/region.'
  }
  $required = Get-Content -Raw (Join-Path $PSScriptRoot '../contracts/inventory-fields.json') | ConvertFrom-Json
  foreach ($field in $required) {
    if ($null -eq $Inventory.$field -or $Inventory.$field -isnot [array]) {
      throw "Inventory field is missing or invalid: $field"
    }
    if ($Inventory.$field.Count -ne 0) { throw "Inventory is not empty: $field" }
  }
}
