param(
    [string]$SourceDir = 'D:/bigdatasource',
    [string]$RunId = (Get-Date -Format 'yyyyMMddHHmmss'),
    [switch]$SkipBuild
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
$previousPythonPath = $env:PYTHONPATH
$previousPythonEncoding = $env:PYTHONIOENCODING
try {
    $python = Join-Path $projectRoot '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw 'Run uv sync --frozen first' }
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $env:PYTHONIOENCODING = 'utf-8'
    & $python scripts/stage_day2.py --source-dir $SourceDir --run-id $RunId
    if ($LASTEXITCODE -ne 0) { throw 'Source staging or wide/long reconciliation failed' }
    if (-not $SkipBuild) {
        docker compose build api
        if ($LASTEXITCODE -ne 0) { throw 'Worker image build failed' }
    }
    docker compose run --rm --no-deps worker /opt/spark/bin/spark-submit --master 'local[2]' --driver-memory 2g scripts/batch_day2.py --run-id $RunId
    if ($LASTEXITCODE -ne 0) { throw 'Spark curation failed; active warehouse unchanged' }
    & $python scripts/verify_day2.py --source-dir $SourceDir --run-id $RunId
    if ($LASTEXITCODE -ne 0) { throw 'Independent verification failed; active warehouse unchanged' }
    Write-Output "PASS: runtime/warehouse/runs/$RunId/verification.json"
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PYTHONIOENCODING = $previousPythonEncoding
    Pop-Location
}
