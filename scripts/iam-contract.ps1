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
