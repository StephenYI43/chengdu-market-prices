param([ValidateSet('check','stop')][string]$Action='check')
$ErrorActionPreference='Stop'
$OutputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding=$OutputEncoding
Push-Location -LiteralPath $PSScriptRoot
try {
    if(-not (Test-Path -LiteralPath '.bundle-session.json')) {throw '请先运行start.cmd。'}
    $session=Get-Content -LiteralPath '.bundle-session.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    $env:MARKET_HTTP_PORT=[string]$session.port
    if($Action -eq 'stop') {
        & docker compose -p $session.project -f compose.yaml stop
        if($LASTEXITCODE -ne 0) {throw '停止失败，请查看Docker Desktop。'}
        Write-Host '已停止本运行包的服务，数据和模型均保留。'
    } else {
        & docker compose -p $session.project -f compose.yaml ps
        try {
            $status=Invoke-RestMethod "http://127.0.0.1:$($session.port)/api/v1/system/status" -TimeoutSec 10
            $status | ConvertTo-Json -Depth 7
        } catch {Write-Host '网页服务尚未就绪，以下是本运行包的最近日志。'}
        & docker compose -p $session.project -f compose.yaml logs --tail 60
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {Pop-Location}
