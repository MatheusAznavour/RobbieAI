[CmdletBinding()]
param(
    [string]$VenvPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"

if (-not $VenvPath) {
    $VenvPath = Join-Path $BackendDir ".venv"
}

$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Error "Backend virtual environment not found at '$VenvPath'. Run .\scripts\install-backend.ps1 first."
    exit 1
}

Push-Location $BackendDir
try {
    Write-Host "Running Ruff lint check..."
    & $VenvPython -m ruff check robbie tests
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    Write-Host "Running Ruff format check..."
    & $VenvPython -m ruff format --check robbie tests
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    Write-Host "Running Pytest..."
    & $VenvPython -m pytest
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

Write-Host "Backend checks and tests passed."
exit 0
