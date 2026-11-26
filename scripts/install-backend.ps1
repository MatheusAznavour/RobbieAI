[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$VenvPath = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendDir = Join-Path $RepoRoot "backend"
$AgentSDir = Join-Path $RepoRoot "vendor\Agent-S"

if (-not $VenvPath) {
    $VenvPath = Join-Path $BackendDir ".venv"
}

if (-not (Test-Path (Join-Path $AgentSDir "setup.py"))) {
    Write-Error "Agent-S submodule not found at '$AgentSDir'. Run: git submodule update --init --recursive"
    exit 1
}

$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Python virtual environment at $VenvPath..."
    & $PythonExe -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$PyVer = (& $VenvPython --version 2>&1).Trim()
Write-Host "Using virtual environment: $VenvPython ($PyVer)"

if ($PyVer -notmatch "Python 3\.(11|12)\.") {
    Write-Warning "Detected $PyVer. Python 3.11 is recommended for Agent-S compatibility."
}

Write-Host "Installing backend dependencies from backend/requirements.txt..."
Push-Location $BackendDir
try {
    & $VenvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}

Write-Host "Verifying core Python imports..."
& $VenvPython -c "import robbie, fastapi, uvicorn, pydantic, httpx, dotenv, websockets, gui_agents; from gui_agents.s3.agents.agent_s import AgentS3; print('Backend import verification succeeded:', robbie.__version__)"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Backend installation complete."
exit 0
