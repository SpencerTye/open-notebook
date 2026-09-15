# One start / stop / load / unload run. The tray icon launches this as a
# separate hidden process so that a long start (about two minutes) never
# freezes the icon, and reads the result from run\last-action.json when the
# process exits. Also usable from a terminal:
#   powershell -ExecutionPolicy Bypass -File action.ps1 -Action stop
#
# Only one action runs at a time, across the tray and the terminal scripts:
# the module holds a named lock while it works; a second action fails at once
# with "Another start or stop is already running".

param(
    [Parameter(Mandatory)][ValidateSet('start', 'stop', 'load', 'unload')][string]$Action,
    [string]$Model,
    [switch]$OpenBrowser
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'NotebookControl.psm1') -Force
$paths = Get-NotebookControlPaths
New-Item -ItemType Directory -Force -Path $paths.RunDir | Out-Null
$startedAt = Get-Date

function Write-LastAction([bool]$Ok, [string]$Message, $Details) {
    [pscustomobject]@{
        Action     = $Action
        Model      = $Model
        Ok         = $Ok
        Message    = $Message
        StartedAt  = $startedAt.ToString('o')
        FinishedAt = (Get-Date).ToString('o')
        Details    = $Details
    } | ConvertTo-Json -Depth 6 | Set-Content -Path $paths.LastActionPath -Encoding UTF8
}

try {
    Write-ControlLog "=== action: $Action $Model ==="
    switch ($Action) {
        'start'  { $r = Start-Notebook -OpenBrowser:$OpenBrowser; Write-LastAction ([bool]$r.Ok) $r.Message $r; if (-not $r.Ok) { exit 1 } }
        'stop'   { $r = Stop-Notebook; Write-LastAction ([bool]$r.Ok) $r.Message $r; if (-not $r.Ok) { exit 1 } }
        'load'   { $m = Invoke-NotebookModelAction -Action load -Model $Model; Write-LastAction $true $m $null }
        'unload' { $m = Invoke-NotebookModelAction -Action unload -Model $Model; Write-LastAction $true $m $null }
    }
    exit 0
} catch {
    $msg = "$Action failed: $($_.Exception.Message)"
    Write-ControlLog $msg
    Write-LastAction $false $msg $null
    exit 1
}
