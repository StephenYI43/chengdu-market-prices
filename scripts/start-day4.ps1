param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build-app.ps1') }
    docker compose up -d --wait --wait-timeout 240 kafka api stream
    if ($LASTEXITCODE -ne 0) { throw 'Application or stream worker startup failed' }
    $service = Invoke-RestMethod http://127.0.0.1:18880/api/v1/imports/service
    if (-not $service.worker.online) { throw 'Stream worker has no recent online heartbeat' }
    Write-Output 'Ready: http://127.0.0.1:18880/#data-management'
} finally { Pop-Location }
