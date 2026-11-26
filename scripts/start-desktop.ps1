[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DesktopDir = Join-Path $RepoRoot "apps\desktop"

if (-not (Test-Path (Join-Path $DesktopDir "node_modules"))) {
    Write-Error "Desktop dependencies are not installed. Run 'npm install' inside apps\desktop first."
    exit 1
}

Push-Location $DesktopDir
try {
    Write-Host "Starting Robbie desktop application in development mode..."
    npm run dev
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
