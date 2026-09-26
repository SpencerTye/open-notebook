# Builds the open-notebook-custom:1.14.0 image from this fork's source
# (see Dockerfile in this folder), then leaves the running stack alone.
# Start or update it afterwards with `docker compose up -d open_notebook`
# from this folder (start.ps1 does the same on every start). The data in
# surreal_data and notebook_data is not touched by a rebuild.
#
# Run:  powershell -ExecutionPolicy Bypass -File windows\open-notebook\build.ps1 [-NoCache]

param([switch]$NoCache)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$dockerBin = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin"
if (Test-Path $dockerBin) { $env:Path = "$dockerBin;$env:Path" }

Push-Location $here
try {
    $buildArgs = @("compose", "build")
    if ($NoCache) { $buildArgs += "--no-cache" }
    $buildArgs += "open_notebook"
    docker @buildArgs
    if ($LASTEXITCODE -ne 0) { Write-Error "docker compose build failed (exit $LASTEXITCODE)"; exit 1 }
} finally {
    Pop-Location
}

Write-Output ""
Write-Output "Built. Start or update the running container with:"
Write-Output "  docker compose up -d open_notebook     (from $here)"
