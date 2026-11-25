param(
    [ValidateRange(1024,65535)][int]$Port=18880,
    [ValidatePattern('^[a-z0-9][a-z0-9_-]{0,55}$')][string]$ProjectName='chengdu-market-demo',
    [switch]$NoBrowser,
    [switch]$ReloadImages
)
$ErrorActionPreference='Stop'
$OutputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding=$OutputEncoding
$root=$PSScriptRoot
$previousPort=$env:MARKET_HTTP_PORT
Push-Location -LiteralPath $root
try {
    if(-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw '请先安装并打开Docker Desktop，再重新运行。下载地址见“先看这里.txt”。'
    }
    $engine=& docker info --format '{{.OSType}} {{.Architecture}}'
    if($LASTEXITCODE -ne 0) {throw 'Docker尚未就绪。请打开Docker Desktop，等待引擎启动完成。'}
    if($engine -notmatch '^linux (x86_64|amd64)$') {throw '请在Windows 64位的Docker Desktop中使用Linux容器模式。'}
    $composeVersion=(& docker compose version --short).Trim().TrimStart('v')
    if($LASTEXITCODE -ne 0 -or [version]$composeVersion -lt [version]'2.24.0') {
        throw '需要Docker Compose 2.24或更高版本，请更新Docker Desktop。'
    }
    $existing=@(& docker ps -a --filter "label=com.docker.compose.project=$ProjectName" --format '{{.ID}}')
    foreach($containerId in $existing) {
        if(-not $containerId) {continue}
        $oldRoot=& docker inspect $containerId --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
        if([IO.Path]::GetFullPath($oldRoot).TrimEnd([char]92,[char]47) -ne [IO.Path]::GetFullPath($root).TrimEnd([char]92,[char]47)) {
            throw "项目名$ProjectName已由另一个目录使用。请先停止旧副本，或用-ProjectName指定不同名称。"
        }
    }
    Write-Host '正在校验运行包，首次校验大镜像文件需要一点时间。'
    function Get-BundleSha256([string]$Path) {
        $stream=[IO.File]::OpenRead($Path)
        $sha=[Security.Cryptography.SHA256]::Create()
        try {return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-','').ToLowerInvariant()}
        finally {$stream.Dispose();$sha.Dispose()}
    }
    $manifest=Get-Content -LiteralPath 'bundle.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach($file in $manifest.files) {
        $path=Join-Path $root $file.path
        if(-not (Test-Path -LiteralPath $path)) {throw "运行包缺少文件：$($file.path)，请完整解压。"}
        if((Get-BundleSha256 $path) -ne $file.sha256) {
            throw "文件校验失败：$($file.path)，请重新接收或解压运行包。"
        }
    }
    function Get-BundleImageMap {
        $map=@{}
        foreach($line in (& docker image ls --no-trunc --format '{{json .}}')) {
            $row=$line | ConvertFrom-Json
            $map[($row.Repository+':'+$row.Tag)]=$row.ID
        }
        return $map
    }
    $available=Get-BundleImageMap
    $needsLoad=$ReloadImages.IsPresent
    foreach($image in $manifest.images) {if($available[$image.tag] -ne $image.id) {$needsLoad=$true}}
    if($needsLoad) {
        Write-Host '正在导入后端、预测和Kafka镜像，首次导入需要几分钟。'
        & docker image load --input (Join-Path $root 'images.tar.gz')
        if($LASTEXITCODE -ne 0) {throw '镜像导入失败，请检查Docker磁盘剩余空间。'}
        $available=Get-BundleImageMap
    }
    foreach($image in $manifest.images) {
        if($available[$image.tag] -ne $image.id) {throw "镜像版本不匹配：$($image.tag)"}
    }
    if(-not (Test-Path -LiteralPath 'runtime/warehouse/current.json')) {
        if((Test-Path -LiteralPath 'runtime') -and @(Get-ChildItem -LiteralPath 'runtime' -Force).Count -gt 0) {
            throw 'runtime目录已有内容但没有完整数据指针。请保留该目录并换一个空目录重新解压运行包。'
        }
        Write-Host '正在初始化真实数据仓库和已有预测模型。'
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory((Join-Path $root 'initial-state.zip'),$root)
    }
    New-Item -ItemType Directory -Path (Join-Path $root 'artifacts') -Force | Out-Null
    $env:MARKET_HTTP_PORT=[string]$Port
    @{project=$ProjectName;port=$Port} | ConvertTo-Json | Set-Content -LiteralPath '.bundle-session.json' -Encoding UTF8
    Write-Host '正在启动4个服务。所有应用依赖均在运行包中，不需要安装Python、Node或Java。'
    & docker compose -p $ProjectName -f compose.yaml up -d --no-build --pull never --wait --wait-timeout 300
    if($LASTEXITCODE -ne 0) {throw '服务启动失败。请运行check.cmd查看状态与日志；若端口被占用可按说明更换端口。'}
    $base="http://127.0.0.1:$Port"
    $deadline=(Get-Date).AddMinutes(10)
    do {
        $status=Invoke-RestMethod "$base/api/v1/system/status" -TimeoutSec 10
        if($status.current_model -and $status.kafka.online -and $status.stream.online -and $status.forecast_service.online) {break}
        Write-Host '正在等待流处理和当前数据对应的预测模型就绪。'
        Start-Sleep -Seconds 3
    } while((Get-Date) -lt $deadline)
    if(-not $status.current_model -or -not $status.stream.online -or -not $status.kafka.online -or -not $status.forecast_service.online) {throw '服务未在预期时间内就绪，请运行check.cmd查看日志。'}
    Write-Host "启动完成：$base"
    Write-Host "真实数据$($status.dataset.totals.source_rows)条，报价截至$($status.dataset.meta.as_of)。"
    Write-Host '以后重新双击start.cmd即可。停止服务请运行stop.cmd。'
    if(-not $NoBrowser) {Start-Process $base}
} catch {
    Write-Host ("启动未完成："+$_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    $env:MARKET_HTTP_PORT=$previousPort
    Pop-Location
}
