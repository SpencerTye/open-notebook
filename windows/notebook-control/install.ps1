# Puts the "Notebook" tray icon in place:
#   - writes notebook.ico (the icon the shortcuts show)
#   - creates the Startup-folder shortcut, so the dot appears at every logon
#   - creates a desktop shortcut, to bring the dot back if it was closed
#   - starts the tray icon now (unless -NoStart)
# Safe to run again; it just rewrites the shortcuts.
#
# Run:  powershell -ExecutionPolicy Bypass -File windows\notebook-control\install.ps1

param([switch]$NoStart, [switch]$NoLogon)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'tray-common.ps1')
$p = Get-TrayShortcutPaths

New-DotIconFile -Path $p.IconFile -Color $script:TrayColors.file
Write-Output "icon      : $($p.IconFile)"

New-NotebookShortcut -Path $p.Desktop
Write-Output "desktop   : $($p.Desktop)"

if (-not $NoLogon) {
    New-NotebookShortcut -Path $p.Startup
    Write-Output "at logon  : $($p.Startup)"
}

if (-not $NoStart) {
    Start-Process -FilePath (Join-Path $env:WINDIR 'System32\wscript.exe') -ArgumentList @(
        ('"{0}"' -f (Join-Path $PSScriptRoot 'run-hidden.vbs')),
        ('"{0}"' -f (Join-Path $PSScriptRoot 'tray.ps1'))
    ) | Out-Null
    Write-Output 'tray icon : started (look next to the clock)'
}
