# tray.ps1 - the "Notebook" tray icon.
#
# A coloured dot by the clock: grey = notebook off, green = on, amber = busy
# or partly up. Click it for a menu: Start notebook, Stop notebook, Open
# notebook, one line per model (click to load / unload), GPU memory in use,
# "Show this icon at logon", "Open log folder", Exit.
#
# The dot is a remote control, not the notebook: Exit leaves the notebook as
# it is. Start / Stop / load / unload run action.ps1 as a separate hidden
# process (so a two-minute start never freezes the icon); the result is read
# from run\last-action.json, and only if that file was written by this very
# action, then shown as a notification. Status is read every 10 seconds (every
# 3 while busy) in a background runspace; the checks are read-only and never
# load or wake a model. A status check that hangs (Docker wedged) is abandoned
# after 90 s and the runspace rebuilt.
#
# Started by the Startup-folder / desktop shortcut through run-hidden.vbs
# (no console window). Only one instance runs at a time. Anything that goes
# wrong before the icon exists is written to notebook-control\logs and shown
# in a message box, since there is no console to see it in.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Write-EarlyFailure([string]$Text) {
    try {
        $dir = Join-Path $PSScriptRoot 'logs'
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -Path (Join-Path $dir ('control-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))) -Value ('{0}  tray: FAILED: {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text) -Encoding UTF8
    } catch { }
    try { [void][System.Windows.Forms.MessageBox]::Show("The Notebook tray icon could not start:`n`n$Text", 'Notebook', 'OK', 'Error') } catch { }
}

try {
    . (Join-Path $PSScriptRoot 'tray-common.ps1')
    Import-Module (Join-Path $PSScriptRoot 'NotebookControl.psm1') -Force
    $paths = Get-NotebookControlPaths
    $shortcuts = Get-TrayShortcutPaths
    New-Item -ItemType Directory -Force -Path $paths.RunDir | Out-Null
    $trayStatusPath = Join-Path $paths.RunDir 'tray-status.json'
    $actionScript = Join-Path $PSScriptRoot 'action.ps1'
} catch {
    Write-EarlyFailure $_.Exception.Message
    exit 1
}

$mutex = New-Object System.Threading.Mutex($false, 'Local\RAGNotebooks.NotebookTray')
$gotMutex = $false
try { $gotMutex = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $gotMutex = $true }   # a killed tray left it; it is ours now
if (-not $gotMutex) { exit 0 }   # already running

Write-ControlLog 'tray: started' -Quiet

# ---- icons and menu ---------------------------------------------------------

$icons = @{}
foreach ($k in 'on', 'off', 'busy', 'partial') { $icons[$k] = New-DotIcon -Color $script:TrayColors[$k] }

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = $icons.off
$notify.Text = 'Notebook: checking...'
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$miState = New-Object System.Windows.Forms.ToolStripMenuItem 'Notebook: checking...'
$miState.Enabled = $false
$miGpu = New-Object System.Windows.Forms.ToolStripMenuItem 'GPU memory in use: ...'
$miGpu.Enabled = $false
$sepModels = New-Object System.Windows.Forms.ToolStripSeparator
$miStart = New-Object System.Windows.Forms.ToolStripMenuItem 'Start notebook'
$miStop = New-Object System.Windows.Forms.ToolStripMenuItem 'Stop notebook (unloads its models)'
$miOpen = New-Object System.Windows.Forms.ToolStripMenuItem 'Open notebook in browser'
$sepBottom = New-Object System.Windows.Forms.ToolStripSeparator
$miLogon = New-Object System.Windows.Forms.ToolStripMenuItem 'Show this icon at logon'
$miLogon.CheckOnClick = $true
$miLogon.Checked = (Test-Path $shortcuts.Startup)
$miLogs = New-Object System.Windows.Forms.ToolStripMenuItem 'Open log folder'
$miExit = New-Object System.Windows.Forms.ToolStripMenuItem 'Exit (leaves the notebook as it is)'

[void]$menu.Items.AddRange([System.Windows.Forms.ToolStripItem[]]@($miState, $miGpu, $sepModels, $miStart, $miStop, $miOpen, $sepBottom, $miLogon, $miLogs, $miExit))
$notify.ContextMenuStrip = $menu

$script:modelItems = @()
$script:pendingModels = $null
$script:actionProc = $null
$script:actionLaunchedAt = [datetime]::MinValue
$script:busyLabel = ''
$script:lastStatus = $null
$script:pollHandle = $null
$script:lastPollStarted = [datetime]::MinValue
$script:poller = $null
$script:runspace = $null

function Show-Note([string]$Text, [string]$Kind = 'Info', [int]$Ms = 6000) {
    try { $notify.ShowBalloonTip($Ms, 'Notebook', $Text, [System.Windows.Forms.ToolTipIcon]$Kind) } catch { }
}

# ---- background status polling ---------------------------------------------

function Initialize-Poller {
    try { if ($script:poller) { $script:poller.Dispose() } } catch { }
    try { if ($script:runspace) { $script:runspace.Close(); $script:runspace.Dispose() } } catch { }
    $script:runspace = [runspacefactory]::CreateRunspace()
    $script:runspace.Open()
    $script:runspace.SessionStateProxy.SetVariable('ModulePath', (Join-Path $PSScriptRoot 'NotebookControl.psm1'))
    $script:poller = [powershell]::Create()
    $script:poller.Runspace = $script:runspace
    [void]$script:poller.AddScript('Import-Module $ModulePath -Force')
    [void]$script:poller.Invoke()
    $script:poller.Commands.Clear()
    $script:pollHandle = $null
}

function Start-StatusPoll {
    if ($script:pollHandle) { return }
    $script:poller.Commands.Clear()
    [void]$script:poller.AddScript('Get-NotebookStatus')
    $script:lastPollStarted = Get-Date
    $script:pollHandle = $script:poller.BeginInvoke()
}

function Complete-StatusPoll {
    if (-not $script:pollHandle) { return }
    if (-not $script:pollHandle.IsCompleted) {
        if (((Get-Date) - $script:lastPollStarted).TotalSeconds -gt 90) {
            Write-ControlLog 'tray: status check took more than 90 s; abandoning it and rebuilding the checker' -Quiet
            try { $script:poller.Stop() } catch { }
            Initialize-Poller
            $miState.Text = 'Notebook: status check timed out (is Docker responding?)'
            $notify.Text = 'Notebook: status check timed out'
        }
        return
    }
    try {
        $out = $script:poller.EndInvoke($script:pollHandle)
        if ($script:poller.HadErrors) {
            foreach ($e in $script:poller.Streams.Error) { Write-ControlLog "tray: status error: $e" -Quiet }
            $script:poller.Streams.Error.Clear()
        }
        if ($out.Count -gt 0) { Update-Ui -Status $out[$out.Count - 1] }
    } catch {
        Write-ControlLog "tray: status poll failed: $($_.Exception.Message)" -Quiet
    } finally {
        $script:pollHandle = $null
    }
}

# ---- actions ----------------------------------------------------------------

function Start-TrayAction {
    param([string]$Action, [string]$Model = '', [string]$Label = '', [switch]$OpenBrowser)
    try {
        if ($script:actionProc -and -not $script:actionProc.HasExited) {
            Show-Note 'Another start or stop is still running.' 'Info' 4000
            return
        }
        Remove-Item -Path $paths.LastActionPath -Force -ErrorAction SilentlyContinue
        $argumentList = New-ActionArgumentList -ScriptPath $actionScript -Action $Action -Model $Model -OpenBrowser:$OpenBrowser
        $script:busyLabel = switch ($Action) {
            'start'  { 'Starting the notebook (about two minutes)...' }
            'stop'   { 'Stopping the notebook and unloading its models...' }
            'load'   { "Loading $Label..." }
            'unload' { "Unloading $Label..." }
        }
        Write-ControlLog "tray: launching action $Action $Model" -Quiet
        $script:actionLaunchedAt = Get-Date
        $script:actionProc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentList -WindowStyle Hidden -PassThru
        Set-Busy $true
        Show-Note $script:busyLabel 'Info' 4000
    } catch {
        Write-ControlLog "tray: could not launch $Action : $($_.Exception.Message)" -Quiet
        Show-Note "Could not start the action: $($_.Exception.Message)" 'Error' 8000
    }
}

function Complete-TrayAction {
    if (-not $script:actionProc -or -not $script:actionProc.HasExited) { return }
    $exitCode = -1
    try { $exitCode = $script:actionProc.ExitCode } catch { }
    $script:actionProc = $null
    $outcome = Resolve-ActionOutcome -LastActionPath $paths.LastActionPath -LaunchedAt $script:actionLaunchedAt -ExitCode $exitCode
    Write-ControlLog "tray: action finished exit=$exitCode ran=$($outcome.Ran) ok=$($outcome.Ok) : $($outcome.Message)" -Quiet
    Set-Busy $false
    $kind = 'Info'
    if (-not $outcome.Ok) { $kind = 'Error' }
    Show-Note $outcome.Message $kind 10000
    Start-StatusPoll
}

function Set-Busy([bool]$Busy) {
    $miStart.Enabled = -not $Busy
    $miStop.Enabled = -not $Busy
    foreach ($mi in $script:modelItems) { $mi.Enabled = (-not $Busy) -and ($null -ne $mi.Tag.Action) }
    if ($Busy) {
        $notify.Icon = $icons.busy
        $notify.Text = (Truncate63 $script:busyLabel)
        $miState.Text = $script:busyLabel
    } elseif ($script:lastStatus) {
        Update-Ui -Status $script:lastStatus
    }
}

function Truncate63([string]$Text) {
    if ($Text.Length -gt 63) { return $Text.Substring(0, 60) + '...' }
    return $Text
}

# ---- rendering --------------------------------------------------------------

function Update-Ui {
    param($Status)
    $script:lastStatus = $Status
    try { $Status | ConvertTo-Json -Depth 5 | Set-Content -Path $trayStatusPath -Encoding UTF8 } catch { }

    $busy = ($script:actionProc -and -not $script:actionProc.HasExited)
    if (-not $busy) {
        $key = "$($Status.Overall)"
        if (-not $icons.ContainsKey($key)) { $key = 'off' }
        $notify.Icon = $icons[$key]
        $notify.Text = (Truncate63 "$($Status.Summary)")
        $state = switch ("$($Status.Overall)") {
            'on'      { 'Notebook: on' }
            'partial' { 'Notebook: partly up' }
            default   { 'Notebook: off' }
        }
        if ("$($Status.ServerState)" -eq 'port conflict') { $state += ' (port 8080 is taken by another program)' }
        if ([int]$Status.Orphans -gt 0) { $state += " ($($Status.Orphans) leftover model process(es) from a dead server; not touched, see log)" }
        $miState.Text = $state
    }
    if ($Status.Gpu) {
        $miGpu.Text = "GPU memory in use: $(Format-GpuGb $Status.Gpu.UsedMiB) of $(Format-GpuGb $Status.Gpu.TotalMiB)"
    } else {
        $miGpu.Text = 'GPU memory in use: unknown'
    }

    # Rebuilding the model lines while the menu is open would change the text
    # under the cursor; keep the new list and apply it when the menu closes.
    if ($menu.Visible) { $script:pendingModels = @($Status.Models); return }
    Set-ModelItems -Models @($Status.Models) -Busy $busy
}

function Set-ModelItems {
    param([object[]]$Models, [bool]$Busy)
    foreach ($mi in $script:modelItems) { $menu.Items.Remove($mi) }
    $script:modelItems = @()
    $index = $menu.Items.IndexOf($miGpu) + 1
    foreach ($m in @($Models)) {
        if ($null -eq $m) { continue }
        $text = "$($m.Label): $($m.Status)"
        if ($m.Status -eq 'loaded' -and $m.SizeBytes) { $text += ", $(Format-Bytes $m.SizeBytes)" }
        $action = $null
        switch ("$($m.Status)") {
            'loaded'   { $text += '   click to unload'; $action = 'unload' }
            'sleeping' { $text += '   click to unload'; $action = 'unload' }
            'unloaded' { $text += '   click to load';   $action = 'load' }
            'loading'  { $text += '...' }
        }
        $mi = New-Object System.Windows.Forms.ToolStripMenuItem $text
        $mi.Tag = @{ Id = "$($m.Id)"; Action = $action; Label = "$($m.Label)" }
        $mi.Enabled = ($null -ne $action) -and (-not $Busy)
        $mi.Add_Click({
            try {
                $tag = $this.Tag
                if ($tag.Action) { Start-TrayAction -Action $tag.Action -Model $tag.Id -Label $tag.Label }
            } catch { Show-Note "Could not act on the model: $($_.Exception.Message)" 'Error' 8000 }
        })
        $menu.Items.Insert($index, $mi)
        $index++
        $script:modelItems += $mi
    }
}

# ---- menu handlers -----------------------------------------------------------

$miStart.Add_Click({ Start-TrayAction -Action 'start' -OpenBrowser })
$miStop.Add_Click({ Start-TrayAction -Action 'stop' })
$miOpen.Add_Click({ try { Start-Process $paths.UiUrl | Out-Null } catch { Show-Note "Could not open the browser: $($_.Exception.Message)" 'Error' } })
$miLogs.Add_Click({
    try {
        New-Item -ItemType Directory -Force -Path $paths.LogDir | Out-Null
        Start-Process explorer.exe -ArgumentList "`"$($paths.LogDir)`"" | Out-Null
    } catch { Show-Note "Could not open the log folder: $($_.Exception.Message)" 'Error' }
})
$miLogon.Add_CheckedChanged({
    try {
        if ($miLogon.Checked) { New-NotebookShortcut -Path $shortcuts.Startup }
        elseif (Test-Path $shortcuts.Startup) { Remove-Item $shortcuts.Startup -Force }
        Write-ControlLog "tray: show at logon = $($miLogon.Checked)" -Quiet
    } catch {
        Show-Note "Could not change the logon setting: $($_.Exception.Message)" 'Error' 8000
    }
})
$miExit.Add_Click({ [System.Windows.Forms.Application]::Exit() })
$menu.Add_Closed({
    if ($null -ne $script:pendingModels) {
        $models = $script:pendingModels
        $script:pendingModels = $null
        try { Set-ModelItems -Models $models -Busy ($script:actionProc -and -not $script:actionProc.HasExited) } catch { }
    }
})

# Left click opens the same menu as right click.
$notify.Add_MouseClick({
    param($sender, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        $method = $notify.GetType().GetMethod('ShowContextMenu', [System.Reflection.BindingFlags]'Instance,NonPublic')
        if ($method) { [void]$method.Invoke($notify, $null) }
    }
})

# ---- timer: drives polling and action completion ------------------------------

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    try {
        Complete-TrayAction
        Complete-StatusPoll
        $busy = ($script:actionProc -and -not $script:actionProc.HasExited)
        $every = 10
        if ($busy) { $every = 3 }
        if (-not $script:pollHandle -and ((Get-Date) - $script:lastPollStarted).TotalSeconds -ge $every) { Start-StatusPoll }
    } catch {
        Write-ControlLog "tray: tick error: $($_.Exception.Message)" -Quiet
    }
})

try {
    Initialize-Poller
    $timer.Start()
    Start-StatusPoll
    [System.Windows.Forms.Application]::Run()
} catch {
    Write-ControlLog "tray: FAILED: $($_.Exception.Message)" -Quiet
    Write-EarlyFailure $_.Exception.Message
} finally {
    $timer.Stop()
    $notify.Visible = $false
    $notify.Dispose()
    try { if ($script:poller) { $script:poller.Stop(); $script:poller.Dispose() } } catch { }
    try { if ($script:runspace) { $script:runspace.Close() } } catch { }
    $mutex.ReleaseMutex() | Out-Null
    Write-ControlLog 'tray: exited' -Quiet
}
