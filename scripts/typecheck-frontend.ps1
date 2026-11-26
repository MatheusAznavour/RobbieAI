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
    Write-Host "Running frontend TypeScript type check..."
    npm run typecheck
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    Write-Host "Running frontend ESLint check..."
    npm run lint
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

Write-Host "Frontend type checks and lint passed."
exit 0
