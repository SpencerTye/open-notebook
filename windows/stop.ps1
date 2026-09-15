# Stops the Open Notebook containers and llama-server. Data is kept
# (open-notebook\surreal_data and open-notebook\notebook_data are untouched).
# Docker Desktop itself is left running; quit it from its tray icon if you want.
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File "S:\RAG Notebooks\stop.ps1"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dockerBin = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin"
if (Test-Path $dockerBin) { $env:Path = "$dockerBin;$env:Path" }

Write-Output "[1/2] Open Notebook containers"
Push-Location (Join-Path $root "open-notebook")
docker compose down
Pop-Location

Write-Output "[2/2] llama-server"
powershell -ExecutionPolicy Bypass -File (Join-Path $root "llama-server\stop-llama-server.ps1")
