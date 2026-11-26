[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$DesktopDir = Join-Path $RepoRoot "apps\desktop"
$AgentSDir = Join-Path $RepoRoot "vendor\Agent-S"
$VenvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"

$HasErrors = $false

function Write-Check {
    param(
        [string]$Status,
        [string]$Label,
        [string]$Detail
    )
    Write-Host ("[{0,-4}] {1}: {2}" -f $Status, $Label, $Detail)
}

Write-Host "Robbie Development Environment Check"
Write-Host "Repository root: $RepoRoot"
Write-Host ""

# 1. Python
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($PythonCmd) {
    $PyVer = (& python --version 2>&1).Trim()
    if ($PyVer -match "Python 3\.(11|12)\.") {
        Write-Check "OK" "Python" "$PyVer ($($PythonCmd.Source))"
    } else {
        Write-Check "WARN" "Python" "$PyVer (Python 3.11 is recommended for Agent-S compatibility)"
    }
} else {
    Write-Check "FAIL" "Python" "python not found on PATH"
    $HasErrors = $true
}

# 2. Backend virtual environment
if (Test-Path $VenvPython) {
    $VenvVer = (& $VenvPython --version 2>&1).Trim()
    Write-Check "OK" "Backend venv" "$VenvVer ($VenvPython)"
} else {
    Write-Check "WARN" "Backend venv" "Not found at $VenvPython (run .\scripts\install-backend.ps1)"
}

# 3. Node.js & npm
$NodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($NodeCmd) {
    $NodeVer = (& node -v 2>&1).Trim()
    Write-Check "OK" "Node.js" "$NodeVer ($($NodeCmd.Source))"
} else {
    Write-Check "FAIL" "Node.js" "node not found on PATH"
    $HasErrors = $true
}

$NpmCmd = Get-Command npm -ErrorAction SilentlyContinue
if ($NpmCmd) {
    $NpmVer = (& npm -v 2>&1).Trim()
    Write-Check "OK" "npm" "v$NpmVer"
} else {
    Write-Check "FAIL" "npm" "npm not found on PATH"
    $HasErrors = $true
}

# 4. Desktop node_modules
$DesktopNodeModules = Join-Path $DesktopDir "node_modules"
if (Test-Path $DesktopNodeModules) {
    Write-Check "OK" "Desktop deps" "node_modules present"
} else {
    Write-Check "WARN" "Desktop deps" "node_modules missing (run 'npm install' in apps\desktop)"
}

# 5. Agent-S submodule
if (Test-Path (Join-Path $AgentSDir "setup.py")) {
    Write-Check "OK" "Agent-S" "Submodule populated at vendor\Agent-S"
} else {
    Write-Check "FAIL" "Agent-S" "Submodule missing or uninitialized (run 'git submodule update --init --recursive')"
    $HasErrors = $true
}

# 6. Git
$GitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($GitCmd) {
    $GitVer = (& git --version 2>&1).Trim()
    Write-Check "OK" "Git" "$GitVer"
} else {
    Write-Check "WARN" "Git" "git not found on PATH"
}

# 7. .env file
$EnvFile = Join-Path $RepoRoot ".env"
if ((Test-Path $EnvFile) -and ((Get-Item $EnvFile).Length -gt 0)) {
    Write-Check "OK" ".env" "Present and non-empty"
} else {
    Write-Check "WARN" ".env" "Missing or empty (copy .env.example to .env and configure providers)"
}

# 8. Optional Tesseract OCR binary
$TesseractCmd = Get-Command tesseract -ErrorAction SilentlyContinue
if ($TesseractCmd) {
    Write-Check "OK" "Tesseract OCR" "$($TesseractCmd.Source)"
} else {
    Write-Check "WARN" "Tesseract OCR" "Not found on PATH (optional; required only for Agent-S OCR text-span actions)"
}

# 9. OneDrive advisory
if ($RepoRoot -match "OneDrive") {
    Write-Check "WARN" "Workspace path" "Repository is inside OneDrive; sync locks can affect .git, .venv, and node_modules"
}

Write-Host ""
if ($HasErrors) {
    Write-Error "Environment check failed due to missing required tools or files."
    exit 1
}

Write-Host "Environment check completed."
exit 0
