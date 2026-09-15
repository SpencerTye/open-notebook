# Stops the notebook's llama-server router and the model helper processes it
# spawned, and waits until they have actually exited (the GPU memory is
# released only then).
#
# Only the notebook's own server is touched. It is identified by the process
# identity recorded by start-llama-server.ps1 (process id + start time), or,
# if that record is missing, by a command line that names this folder's
# models.ini. Its models are unloaded through its own API on port 8080 first
# (after checking that the process listening there is ours), then that one
# process and its helpers (children of that process) are ended. Any other
# llama-server.exe on this PC is left alone.

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'notebook-control\NotebookControl.psm1') -Force
try {
    $r = Stop-NotebookLlamaServer
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
if (-not $r.WasRunning) { exit 0 }
if (-not $r.AllGone) { exit 1 }
