# Starts the whole Open Notebook stack on this PC:
#   1. Docker Desktop (if it is not already running)
#   2. llama-server in router mode (Gemma 4 chat model + Qwen3 embedder)
#   3. the Open Notebook containers (database + app)
# Then waits until the app answers and prints the URLs.
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File "S:\RAG Notebooks\start.ps1"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dockerBin = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin"
if (Test-Path $dockerBin) { $env:Path = "$dockerBin;$env:Path" }   # docker.exe and its credential helper

function Test-DockerEngine {
    try { $null = docker version --format '{{.Server.Version}}' 2>$null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

Write-Output "[1/3] Docker Desktop"
if (-not (Test-DockerEngine)) {
    $dd = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"
    if (-not (Test-Path $dd)) { Write-Error "Docker Desktop not found at $dd"; exit 1 }
    Write-Output "      starting Docker Desktop..."
    Start-Process -FilePath $dd | Out-Null
    $deadline = (Get-Date).AddMinutes(3)
    while (-not (Test-DockerEngine)) {
        if ((Get-Date) -gt $deadline) { Write-Error "Docker engine did not come up within 3 minutes"; exit 1 }
        Start-Sleep -Seconds 5
    }
}
Write-Output "      engine ready"

Write-Output "[2/3] llama-server"
powershell -ExecutionPolicy Bypass -File (Join-Path $root "llama-server\start-llama-server.ps1")

Write-Output "[3/3] Open Notebook containers"
Push-Location (Join-Path $root "open-notebook")
docker compose up -d
$composeExit = $LASTEXITCODE
Pop-Location
if ($composeExit -ne 0) { Write-Error "docker compose up failed (exit $composeExit)"; exit 1 }

Write-Output "      waiting for the API..."
$deadline = (Get-Date).AddMinutes(3)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $h = Invoke-RestMethod -Uri "http://localhost:5055/health" -TimeoutSec 5
        if ($h.status -eq "healthy") { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
}
if (-not $ready) { Write-Warning "API not healthy yet; check: docker compose -f `"$root\open-notebook\docker-compose.yml`" logs -f open_notebook" }

Write-Output ""
Write-Output "Open Notebook UI : http://localhost:8502"
Write-Output "REST API docs    : http://localhost:5055/docs"
Write-Output "llama-server     : http://localhost:8080/v1/models"
