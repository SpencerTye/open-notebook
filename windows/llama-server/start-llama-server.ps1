# Starts llama-server in router mode as a detached background process.
# Serves every model listed in models.ini on http://localhost:8080 (and on
# 0.0.0.0 so the Open Notebook containers can reach it as
# http://host.docker.internal:8080/v1). Logs go to .\logs\.
#
# Run:   powershell -ExecutionPolicy Bypass -File start-llama-server.ps1
# Stop:  powershell -ExecutionPolicy Bypass -File stop-llama-server.ps1

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $root "bin\llama-server.exe"
$ini = Join-Path $root "models.ini"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null

if (-not (Test-Path $exe)) { Write-Error "llama-server.exe not found at $exe"; exit 1 }
if (-not (Test-Path $ini)) { Write-Error "models.ini not found at $ini"; exit 1 }

function Test-LlamaHealth {
    try { $h = Invoke-RestMethod -Uri "http://localhost:8080/health" -TimeoutSec 3; return ($h.status -eq "ok") } catch { return $false }
}

# "Running" means answering on port 8080, not merely present in the process
# list: a server that is still shutting down, or one that crashed on startup,
# shows up in the list but must not stop us from launching a fresh one.
$running = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    if (Test-LlamaHealth) {
        Write-Output "llama-server is already running and healthy (pid $($running[0].Id)). Nothing to do."
        exit 0
    }
    Write-Output "Found $($running.Count) llama-server process(es) but nothing healthy on port 8080; stopping them first."
    $running | Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Process llama-server -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$args = @(
    "--models-preset", "`"$ini`"",
    "--host", "0.0.0.0",
    "--port", "8080",
    "--models-max", "2"
)
$p = Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $root `
    -RedirectStandardOutput (Join-Path $logDir "llama-server-$stamp.out.log") `
    -RedirectStandardError  (Join-Path $logDir "llama-server-$stamp.err.log") `
    -WindowStyle Hidden -PassThru

$deadline = (Get-Date).AddSeconds(30)
while (-not (Test-LlamaHealth) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
if (-not (Test-LlamaHealth)) {
    Write-Warning "llama-server (pid $($p.Id)) started but is not answering on port 8080 yet. Check $logDir\llama-server-$stamp.err.log"
    exit 1
}
Write-Output "Started llama-server (pid $($p.Id)); router is up, the chat model loads in the background (about 30 s). Logs: $logDir\llama-server-$stamp.*.log"
Write-Output "Health: http://localhost:8080/health   Models: http://localhost:8080/v1/models"
