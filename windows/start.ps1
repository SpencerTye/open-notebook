# Starts the whole Open Notebook stack on this PC from a terminal:
#   1. Docker Desktop (if it is not already running)
#   2. the notebook's llama-server in router mode (Gemma 4 chat model + Qwen3 embedder)
#   3. the Open Notebook containers (database + app)
# Then waits until the app answers and prints the URLs.
#
# The same thing the tray icon's "Start notebook" does; the logic lives in
# notebook-control\NotebookControl.psm1.
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File "S:\RAG Notebooks\start.ps1"

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'notebook-control\NotebookControl.psm1') -Force
try {
    $r = Start-Notebook
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
Write-Output ''
Write-Output 'Open Notebook UI : http://localhost:8502'
Write-Output 'REST API docs    : http://localhost:5055/docs'
Write-Output 'llama-server     : http://localhost:8080/v1/models'
