$ErrorActionPreference = 'Stop'
# Pin both the version and official release checksum; install in the user's
# cache so verification works without changing PATH or requiring administrator.
$version = '0.76.0'
if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') {
  throw 'The pinned scanner bootstrap currently supports x64 Windows/Linux.'
}
if ($IsWindows) {
  $asset = "trivy_${version}_windows-64bit.zip"
  $checksum = '7b4762a5a5326cb4b83dfd2bb5d9aaf15a1ed867c75cad7dd8b38840a28e7be6'
  $cacheRoot = Join-Path $env:LOCALAPPDATA 'TechX/tools'
  $executable = 'trivy.exe'
} elseif ($IsLinux) {
  $asset = "trivy_${version}_Linux-64bit.tar.gz"
  $checksum = 'e24b1c51293c1b15e4ba8d45bbc7891ba053f90278ec2698f3562c993e145fe7'
  $cacheRoot = Join-Path $HOME '.cache/techx/tools'
  $executable = 'trivy'
} else { throw 'The pinned scanner bootstrap currently supports Windows/Linux.' }
$directory = Join-Path $cacheRoot "trivy/$version"
$binary = Join-Path $directory $executable
if (-not (Test-Path -LiteralPath $binary)) {
  New-Item -ItemType Directory -Force -Path $directory | Out-Null
  $archive = Join-Path $directory $asset
  Invoke-WebRequest -Uri "https://github.com/aquasecurity/trivy/releases/download/v$version/$asset" -OutFile $archive -TimeoutSec 120
  if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checksum) {
    throw 'Trivy release checksum mismatch.'
  }
  if ($IsWindows) { Expand-Archive -LiteralPath $archive -DestinationPath $directory -Force }
  else {
    tar -xzf $archive -C $directory $executable
    if ($LASTEXITCODE -ne 0) { throw 'Trivy extraction failed.' }
  }
  Remove-Item -LiteralPath $archive -Force
}
$info = & $binary --version --format json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $info.Version -ne $version) { throw 'Pinned Trivy installation is invalid.' }
Write-Output $binary
