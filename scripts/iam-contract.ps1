function Get-SimulationPrincipalArn([object]$Identity) {
  if ($Identity.Arn -match '^arn:aws:sts::([0-9]{12}):assumed-role/(.+)/[^/]+$') {
    $account = $Matches[1]
    $roleName = ($Matches[2] -split '/')[-1]
    if ($account -ne $Identity.Account -or $roleName -notmatch '^[A-Za-z0-9+=,.@_-]{1,64}$') {
      throw 'Unexpected assumed-role identity.'
    }
    $role = aws iam get-role --role-name $roleName --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $role.Role.Arn -notmatch "^arn:aws:iam::$account`:role/(?:.*/)?$([regex]::Escape($roleName))$") {
      throw 'Unable to resolve the IAM role behind the temporary session.'
    }
    return $role.Role.Arn
  }
  if ($Identity.Arn -notmatch '^arn:aws:iam::[0-9]{12}:(user|role)/') {
    throw 'Unsupported identity for IAM policy simulation.'
  }
  return $Identity.Arn
}

function Invoke-TechxScopedSimulation {
  param(
    [Parameter(Mandatory)][string]$PrincipalArn,
    [Parameter(Mandatory)][object[]]$Cases
  )
  if ($PrincipalArn -notmatch '^arn:aws:iam::058114477594:role/techx-automation-[A-Za-z0-9-]+$' -or $Cases.Count -lt 1) {
    throw 'Scoped automation IAM role and explicit simulation cases are required.'
  }
  $directory = Join-Path ([System.IO.Path]::GetTempPath()) ("techx-iam-" + [guid]::NewGuid().ToString('N'))
  [System.IO.Directory]::CreateDirectory($directory) | Out-Null
  $requestPath = Join-Path $directory 'request.json'
  try {
    foreach ($case in $Cases) {
      if ($case.name -notmatch '^[A-Za-z0-9-]{1,80}$' -or $case.action -notmatch '^[a-z0-9-]+:[A-Za-z0-9]+$' -or
          @($case.resources).Count -lt 1 -or $case.expected -notin @('allowed', 'denied')) {
        throw 'Each IAM case needs one concrete action, resource ARNs, context and an expected decision.'
      }
      foreach ($resource in @($case.resources)) {
        if ($resource -eq '*' -or $resource -notmatch '^arn:aws:[^\s]+$') { throw 'Scoped simulations cannot use wildcard resource placeholders.' }
      }
      $request = @{ PolicySourceArn = $PrincipalArn; ActionNames = @($case.action); ResourceArns = @($case.resources) }
      if ($case.context) { $request.ContextEntries = @($case.context) }
      $request | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $requestPath -Encoding utf8NoBOM
      $json = aws iam simulate-principal-policy --cli-input-json "file://$requestPath" --output json
      if ($LASTEXITCODE -ne 0) { throw "IAM simulation API failed for $($case.name)." }
      $response = $json | ConvertFrom-Json
      $results = @($response.EvaluationResults)
      if ($response.IsTruncated -or $results.Count -ne 1 -or $results[0].EvalActionName -ne $case.action -or
          @($results[0].MissingContextValues | Where-Object { $null -ne $_ }).Count -gt 0) {
        throw "IAM simulation is incomplete or missing context for $($case.name)."
      }
      $decision = $results[0].EvalDecision
      if ($decision -notin @('allowed', 'implicitDeny', 'explicitDeny')) { throw 'Unknown IAM simulation decision.' }
      if (($case.expected -eq 'allowed' -and $decision -ne 'allowed') -or ($case.expected -eq 'denied' -and $decision -eq 'allowed')) {
        throw "Unexpected IAM decision for $($case.name): $decision."
      }
      $perResource = @($results[0].ResourceSpecificResults | Where-Object { $null -ne $_ })
      if ($perResource.Count -eq 0 -and @($case.resources).Count -eq 1 -and $results[0].EvalResourceName -eq $case.resources[0]) {
        $perResource = @([pscustomobject]@{ EvalResourceName = $results[0].EvalResourceName; EvalResourceDecision = $decision })
      }
      if ($perResource.Count -ne @($case.resources).Count -or @($perResource.EvalResourceName | Sort-Object -Unique).Count -ne @($case.resources).Count) {
        throw "IAM simulation omitted resource-level results for $($case.name)."
      }
      foreach ($row in $perResource) {
        if ($row.EvalResourceName -notin $case.resources -or @($row.MissingContextValues | Where-Object { $null -ne $_ }).Count -gt 0 -or
            $row.EvalResourceDecision -notin @('allowed', 'implicitDeny', 'explicitDeny') -or
            ($case.expected -eq 'allowed' -and $row.EvalResourceDecision -ne 'allowed') -or
            ($case.expected -eq 'denied' -and $row.EvalResourceDecision -eq 'allowed')) {
          throw "Resource/context decision is incomplete for $($case.name)."
        }
      }
      [pscustomobject]@{ name = $case.name; action = $case.action; decision = $decision; resources = @($case.resources); simulatedOnly = $true }
    }
  } finally {
    if (Test-Path -LiteralPath $requestPath) { Remove-Item -LiteralPath $requestPath }
    [System.IO.Directory]::Delete($directory)
  }
}
