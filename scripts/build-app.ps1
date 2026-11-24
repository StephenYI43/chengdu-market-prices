param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $statePath = Join-Path $projectRoot '.tools/frontend-install.sha256'
    $lockPath = Join-Path $projectRoot 'frontend/pnpm-lock.yaml'
    $packagePath = Join-Path $projectRoot 'frontend/package.json'
    $inputsHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash + (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $statePath) { (Get-Content -LiteralPath $statePath -Raw).Trim() } else { '' }
    Push-Location -LiteralPath (Join-Path $projectRoot 'frontend')
    try {
        if (-not $SkipInstall -and (-not (Test-Path -LiteralPath 'node_modules') -or $installedHash -ne $inputsHash)) {
            pnpm install --frozen-lockfile --prefer-offline
            if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
            New-Item -ItemType Directory -Path (Split-Path -Parent $statePath) -Force | Out-Null
            Set-Content -LiteralPath $statePath -Value $inputsHash -Encoding ASCII
        }
        pnpm build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend production build failed' }
    } finally { Pop-Location }
    docker compose build api
    if ($LASTEXITCODE -ne 0) { throw 'Application image build failed' }
} finally { Pop-Location }
