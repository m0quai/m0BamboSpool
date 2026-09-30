param(
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$now = Get-Date
$buildNumber = $now.ToString("yyMMdd.HHmm")
$buildCommit = (git rev-parse --short HEAD).Trim()
$env:BUILD_NUMBER = $buildNumber
Write-Host "Building m0BamboSpool with BUILD_NUMBER=$buildNumber BUILD_COMMIT=$buildCommit"

docker compose build --no-cache --build-arg "BUILD_NUMBER=$buildNumber" --build-arg "BUILD_COMMIT=$buildCommit" m0bambospool
if (-not $NoStart) {
    docker compose up -d m0bambospool
}
