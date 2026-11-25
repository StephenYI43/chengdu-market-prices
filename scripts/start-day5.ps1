param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if (-not $SkipBuild) {
        & (Join-Path $PSScriptRoot 'build-app.ps1')
        docker compose build forecast
        if ($LASTEXITCODE -ne 0) { throw 'Forecast image build failed' }
    }
    docker compose up -d --wait --wait-timeout 300 kafka api stream forecast
    if ($LASTEXITCODE -ne 0) { throw 'Application or forecasting startup failed' }
    $deadline = (Get-Date).AddMinutes(10)
    do {
        $catalog = Invoke-RestMethod http://127.0.0.1:18880/api/v1/forecasts/catalog
        if ($catalog.status -eq 'ready') { break }
        if ($catalog.service.status -eq 'failed') { throw $catalog.service.error }
        Write-Output $catalog.service.message
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)
    if ($catalog.status -ne 'ready') { throw 'Forecast not ready within 10 minutes; inspect forecast service logs' }
    Write-Output "Ready: http://127.0.0.1:18880/#forecasts ($($catalog.report.ready_products) eligible products)"
} finally { Pop-Location }
