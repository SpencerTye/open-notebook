# Stops the Open Notebook containers and the notebook's own llama-server from
# a terminal. Data is kept (open-notebook\surreal_data and
# open-notebook\notebook_data are untouched). Docker Desktop is left running.
#
# Only the notebook's own model server is touched: it is identified by the
# process identity recorded at start (or by a command line naming our
# models.ini), its models are unloaded through its API, and then that one
# process and its helpers are ended. Other llama-server processes on this PC
# are never touched. The logic lives in notebook-control\NotebookControl.psm1.
#
# Run:  powershell -ExecutionPolicy Bypass -File windows\stop.ps1

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'notebook-control\NotebookControl.psm1') -Force
try {
    $r = Stop-Notebook
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
