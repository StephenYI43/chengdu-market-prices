param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & (Join-Path $PSScriptRoot 'start-day5.ps1') -SkipBuild:$SkipBuild
    $status = Invoke-RestMethod http://127.0.0.1:18880/api/v1/system/status
    $alerts = Invoke-RestMethod http://127.0.0.1:18880/api/v1/alerts
    if (-not $status.kafka.online -or -not $status.stream.online) { throw 'Message or stream service is not ready' }
    Write-Output "Ready: http://127.0.0.1:18880/#alerts ($($alerts.total) current-week review events)"
} finally { Pop-Location }
