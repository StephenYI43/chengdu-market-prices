param([string]$RunId = (Get-Date -Format 'yyyyMMddHHmmss'))
$ErrorActionPreference='Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    docker compose up -d --wait --wait-timeout 180 kafka api
    if ($LASTEXITCODE -ne 0) { throw 'Kafka/API startup failed' }
    docker compose run --rm --no-deps worker /app/.venv/bin/python scripts/publish_sample.py --run-id $RunId
    if ($LASTEXITCODE -ne 0) { throw 'Real sample publication failed' }
    docker compose run --rm --no-deps worker /opt/spark/bin/spark-submit --master 'local[2]' --driver-memory 2g --conf 'spark.jars.ivy=/opt/spark/ivy' --packages 'org.apache.spark:spark-sql-kafka-0-10_2.13:4.3.0' scripts/spark_smoke.py --run-id $RunId
    if ($LASTEXITCODE -ne 0) { throw 'Spark stream verification failed' }
    $reportPath = Join-Path $projectRoot "artifacts/day1/$RunId/verification.json"
    $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $report.passed) { throw 'Reconciliation report did not pass' }
    Write-Output "PASS: $reportPath"
} finally { Pop-Location }
