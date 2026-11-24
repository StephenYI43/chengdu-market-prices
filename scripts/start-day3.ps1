param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot 'build-app.ps1')
    }
    docker compose up -d --wait --wait-timeout 180 kafka api
    if ($LASTEXITCODE -ne 0) { throw 'Application startup failed' }
    $overview = Invoke-RestMethod http://127.0.0.1:18880/api/v1/overview
    if (-not $overview.meta.verified) { throw 'No verified warehouse is available' }
    $page = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18880/
    if ($page.StatusCode -ne 200) { throw 'Frontend page did not respond successfully' }
    Write-Output 'Ready: http://127.0.0.1:18880/'
} finally { Pop-Location }
