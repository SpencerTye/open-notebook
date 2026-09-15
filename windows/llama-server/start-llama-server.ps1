# Starts the notebook's llama-server in router mode as a detached background
# process. Serves every model listed in models.ini on http://localhost:8080
# (and on 0.0.0.0 so the Open Notebook containers can reach it as
# http://host.docker.internal:8080/v1). Logs go to .\logs\.
#
# The process id and start time of the server it launches are recorded in
# .\run\notebook-server.json so that stop-llama-server.ps1 can end exactly
# that server and nothing else. If another program already owns port 8080,
# this script refuses to start a second server there.
#
# Run:   powershell -ExecutionPolicy Bypass -File start-llama-server.ps1
# Stop:  powershell -ExecutionPolicy Bypass -File stop-llama-server.ps1

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'notebook-control\NotebookControl.psm1') -Force
try {
    $r = Start-NotebookLlamaServer
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
Write-Output 'Health: http://localhost:8080/health   Models: http://localhost:8080/v1/models'
