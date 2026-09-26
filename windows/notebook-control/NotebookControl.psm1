# NotebookControl.psm1
#
# Everything that starts, stops and inspects the Open Notebook stack on this PC:
# Docker Desktop, the notebook's own llama-server (llama.cpp in router mode on
# port 8080) and the two compose containers. Used by:
#   - notebook-control\tray.ps1          the tray icon (Start / Stop / status)
#   - notebook-control\action.ps1        one start/stop/load/unload run, launched by the tray
#   - start.ps1, stop.ps1                the terminal entry points in windows\
#   - llama-server\start-llama-server.ps1, stop-llama-server.ps1
#
# The one rule this module exists to enforce: the notebook's model server is
# identified by WHICH PROCESS IT IS (process id + start time recorded at
# launch, or failing that a command line naming OUR models.ini as a whole
# token and port 8080), never by the program name "llama-server.exe". Other
# llama.cpp servers may run on the same PC; those must never be touched. Helper
# processes count only if their parent is our router AND they are younger than
# it (Windows reuses ids and never rewrites a child's parent id). Unload
# requests go to port 8080 only after checking that the process listening
# there is ours. Every process is re-checked (name and start time) right
# before it is ended.
#
# Pure functions (no side effects, unit-tested in tests\NotebookControl.Tests.ps1):
#   Find-NotebookRouter, Test-RecordMatchesProcess, Test-SameProcess,
#   Get-RouterHelpers, Get-PortOwners, Test-PortOwnedBy, Select-ModelsToUnload,
#   Resolve-StopPlan, ConvertFrom-NvidiaSmiMemory, Format-GpuGb,
#   New/Read/Write-ServerRecord, ConvertFrom-DockerPsLines,
#   ConvertTo-CommandLineArgument, New-ActionArgumentList, Resolve-ActionOutcome
# Everything else talks to the machine and is verified live.

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

$script:ControlDir     = $PSScriptRoot
$script:WorkspaceRoot  = Split-Path -Parent $PSScriptRoot
$script:LlamaDir       = Join-Path $script:WorkspaceRoot 'llama-server'
$script:LlamaExe       = Join-Path $script:LlamaDir 'bin\llama-server.exe'
$script:ModelsIni      = Join-Path $script:LlamaDir 'models.ini'
$script:LlamaLogDir    = Join-Path $script:LlamaDir 'logs'
$script:RecordPath     = Join-Path $script:LlamaDir 'run\notebook-server.json'
$script:ComposeDir     = Join-Path $script:WorkspaceRoot 'open-notebook'
$script:ComposeProject = 'open-notebook'
$script:LlamaPort      = 8080
$script:LlamaBaseUrl   = 'http://localhost:8080'
$script:ApiHealthUrl   = 'http://localhost:5055/health'
$script:UiUrl          = 'http://localhost:8502'
$script:RunDir         = Join-Path $PSScriptRoot 'run'
$script:LogDir         = Join-Path $PSScriptRoot 'logs'
$script:LastActionPath = Join-Path $script:RunDir 'last-action.json'
$script:ActionMutexName = 'Local\RAGNotebooks.NotebookAction'

$script:ModelLabels = @{
    'gemma-4-26b-a4b'    = 'Chat model (Gemma)'
    'qwen3-embedding-4b' = 'Embedding model (Qwen3)'
}

function Get-NotebookControlPaths {
    [pscustomobject]@{
        WorkspaceRoot  = $script:WorkspaceRoot
        LlamaExe       = $script:LlamaExe
        ModelsIni      = $script:ModelsIni
        RecordPath     = $script:RecordPath
        ComposeDir     = $script:ComposeDir
        RunDir         = $script:RunDir
        LogDir         = $script:LogDir
        LastActionPath = $script:LastActionPath
        UiUrl          = $script:UiUrl
        LlamaPort      = $script:LlamaPort
    }
}

# ---------------------------------------------------------------------------
# Logging (to notebook-control\logs\control-YYYYMMDD.log and the console)
# ---------------------------------------------------------------------------

function Write-ControlLog {
    param([Parameter(Mandatory)][string]$Message, [switch]$Quiet)
    try {
        if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null }
        $file = Join-Path $script:LogDir ('control-{0}.log' -f (Get-Date -Format 'yyyyMMdd'))
        Add-Content -Path $file -Value ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) -Encoding UTF8
    } catch { }
    if (-not $Quiet) { Write-Host $Message }
}

# ---------------------------------------------------------------------------
# Pure functions: identifying the notebook's own server
# ---------------------------------------------------------------------------

function ConvertTo-ComparablePath {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ($null -eq $Text) { return '' }
    return ($Text -replace '/', '\').ToLowerInvariant()
}

function ConvertFrom-RoundtripTime {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) { return $Value }
    try { return [datetime]::Parse("$Value", [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) } catch { return $null }
}

function New-ServerRecord {
    # What Start writes down about the router it launched. Pid + start time is
    # the identity; the command line is kept for humans reading the file.
    param([Parameter(Mandatory)]$Process, [int]$Port = 8080)
    [pscustomobject]@{
        Pid         = [int]$Process.ProcessId
        StartTime   = ([datetime]$Process.CreationDate).ToString('o')
        Port        = $Port
        CommandLine = "$($Process.CommandLine)"
        RecordedAt  = (Get-Date).ToString('o')
    }
}

function Write-ServerRecord {
    param([Parameter(Mandatory)]$Record, [Parameter(Mandatory)][string]$Path)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $Record | ConvertTo-Json | Set-Content -Path $Path -Encoding UTF8
}

function Read-ServerRecord {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try {
        $obj = Get-Content -Path $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch { return $null }
    if ($null -eq $obj) { return $null }
    if (-not $obj.PSObject.Properties['Pid'] -or -not $obj.PSObject.Properties['StartTime']) { return $null }
    return $obj
}

function Test-RecordMatchesProcess {
    # True only if the process has the recorded id AND is llama-server.exe AND
    # started at the recorded time (within tolerance). Windows reuses process
    # ids; the start time is what proves this is still the process we launched.
    param([Parameter(Mandatory)]$Record, [Parameter(Mandatory)]$Process, [double]$ToleranceSeconds = 2)
    if ($null -eq $Record -or $null -eq $Process) { return $false }
    try {
        if ([int]$Record.Pid -ne [int]$Process.ProcessId) { return $false }
    } catch { return $false }
    if ("$($Process.Name)" -ne 'llama-server.exe') { return $false }
    $recorded = ConvertFrom-RoundtripTime $Record.StartTime
    if ($null -eq $recorded) { return $false }
    try { $actual = [datetime]$Process.CreationDate } catch { return $false }
    return ([math]::Abs(($actual - $recorded).TotalSeconds) -le $ToleranceSeconds)
}

function Test-SameProcess {
    # True if a live process is still the one in an earlier snapshot: same id,
    # same name (llama-server.exe) and same start time within tolerance.
    param([Parameter(Mandatory)]$Snapshot, [AllowNull()]$Current, [double]$ToleranceSeconds = 2)
    if ($null -eq $Snapshot -or $null -eq $Current) { return $false }
    if ([int]$Snapshot.ProcessId -ne [int]$Current.ProcessId) { return $false }
    if ("$($Current.Name)" -ne 'llama-server.exe' -or "$($Snapshot.Name)" -ne 'llama-server.exe') { return $false }
    try {
        $delta = ([datetime]$Current.CreationDate - [datetime]$Snapshot.CreationDate).TotalSeconds
    } catch { return $false }
    return ([math]::Abs($delta) -le $ToleranceSeconds)
}

function Find-NotebookRouter {
    # The notebook's router(s) among a snapshot of llama-server.exe processes.
    # First by the record written at start (identity); if that does not match
    # any live process, by command line: only a process started with
    # "--models-preset <our models.ini>" as a whole token, on port 8080 (given
    # or default), counts. Never by program name alone.
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][AllowNull()][object[]]$Processes,
        [Parameter(Mandatory)][string]$ModelsIniPath,
        $Record = $null,
        [int]$Port = 8080
    )
    $procs = @($Processes | Where-Object { $null -ne $_ })
    if ($null -ne $Record) {
        $byRecord = @($procs | Where-Object { Test-RecordMatchesProcess -Record $Record -Process $_ })
        if ($byRecord.Count -gt 0) { return $byRecord }
    }
    $ini = ConvertTo-ComparablePath $ModelsIniPath
    $iniPattern = '--models-preset\s+"?' + [regex]::Escape($ini) + '"?(\s|$)'
    return @($procs | Where-Object {
        if ("$($_.Name)" -ne 'llama-server.exe') { return $false }
        $cmd = ConvertTo-ComparablePath "$($_.CommandLine)"
        if ($cmd -notmatch $iniPattern) { return $false }
        if ($cmd -match '--port\s+"?(\d+)"?') { return ([int]$Matches[1] -eq $Port) }
        return $true
    })
}

function Get-RouterHelpers {
    # The helper processes (one per loaded model) are direct children of the
    # router, started after it. Found by parentage plus age, never by name alone:
    # Windows reuses ids and never rewrites a process's parent id when the
    # parent exits, so an older process whose long-gone parent had our router's
    # id would otherwise look like a child.
    param([AllowEmptyCollection()][AllowNull()][object[]]$Processes, [Parameter(Mandatory)]$Router)
    $routerId = [int]$Router.ProcessId
    $notBefore = ([datetime]$Router.CreationDate).AddSeconds(-1)
    return @($Processes | Where-Object {
        $null -ne $_ -and
        "$($_.Name)" -eq 'llama-server.exe' -and
        [int]$_.ParentProcessId -eq $routerId -and
        [int]$_.ProcessId -ne $routerId -and
        ([datetime]$_.CreationDate) -ge $notBefore
    })
}

function Get-PortOwners {
    # Distinct process ids listening on a port, from Get-NetTCPConnection rows.
    param([AllowEmptyCollection()][AllowNull()][object[]]$Connections, [Parameter(Mandatory)][int]$Port)
    return @($Connections |
        Where-Object { $null -ne $_ -and [int]$_.LocalPort -eq $Port -and "$($_.State)" -eq 'Listen' } |
        ForEach-Object { [int]$_.OwningProcess } |
        Sort-Object -Unique)
}

function Test-PortOwnedBy {
    param([AllowEmptyCollection()][AllowNull()][object[]]$Connections, [Parameter(Mandatory)][int]$Port, [Parameter(Mandatory)][int]$ExpectedPid)
    $owners = @(Get-PortOwners -Connections $Connections -Port $Port)
    if ($owners.Count -eq 0) { return $false }
    return (@($owners | Where-Object { $_ -ne $ExpectedPid }).Count -eq 0)
}

function Select-ModelsToUnload {
    # From llama-server's GET /models answer: the ids that hold (or are about
    # to hold) GPU memory. "unloaded" and "downloading" hold none.
    param([Parameter(Mandatory)][AllowNull()]$Payload)
    $ids = @()
    if ($null -eq $Payload) { return $ids }
    $data = $null
    if ($Payload.PSObject.Properties['data']) { $data = $Payload.data }
    foreach ($item in @($data)) {
        if ($null -eq $item) { continue }
        $value = ''
        if ($item.PSObject.Properties['status'] -and $null -ne $item.status) { $value = "$($item.status.value)" }
        if ($value -in @('loaded', 'loading', 'sleeping')) { $ids += "$($item.id)" }
    }
    return $ids
}

function Resolve-StopPlan {
    # Decides, from snapshots only, exactly what Stop may touch:
    #   Routers          our router(s), by identity (see Find-NotebookRouter)
    #   Helpers          their direct children, younger than them
    #   ProcessIdsToEnd  the union; never contains anything else
    #   PortOk           true only if every listener on the port is one of our routers,
    #                    i.e. it is safe to send unload requests there
    #   Orphans          helpers of a recorded router that has died: reported, never ended
    param(
        [AllowEmptyCollection()][AllowNull()][object[]]$Processes,
        [Parameter(Mandatory)][string]$ModelsIniPath,
        $Record = $null,
        [AllowEmptyCollection()][AllowNull()][object[]]$Connections,
        [int]$Port = 8080
    )
    $procs = @($Processes | Where-Object { $null -ne $_ })
    $routers = @(Find-NotebookRouter -Processes $procs -ModelsIniPath $ModelsIniPath -Record $Record -Port $Port)
    $helpers = @()
    foreach ($r in $routers) { $helpers += @(Get-RouterHelpers -Processes $procs -Router $r) }
    $owners = @(Get-PortOwners -Connections $Connections -Port $Port)
    $routerIds = @($routers | ForEach-Object { [int]$_.ProcessId })

    $orphans = @()
    if ($routers.Count -eq 0 -and $null -ne $Record) {
        $recordedStart = ConvertFrom-RoundtripTime $Record.StartTime
        $recordedPid = 0
        try { $recordedPid = [int]$Record.Pid } catch { }
        if ($null -ne $recordedStart -and $recordedPid -gt 0) {
            $notBefore = $recordedStart.AddSeconds(-1)
            $orphans = @($procs | Where-Object {
                "$($_.Name)" -eq 'llama-server.exe' -and
                [int]$_.ParentProcessId -eq $recordedPid -and
                [int]$_.ProcessId -ne $recordedPid -and
                ([datetime]$_.CreationDate) -ge $notBefore
            })
        }
    }

    $portOk = $false
    $reason = ''
    if ($routers.Count -eq 0) {
        $reason = "The notebook's model server is not running (no llama-server started with our models.ini on port $Port)."
        if ($orphans.Count -gt 0) {
            $ids = @($orphans | ForEach-Object { $_.ProcessId }) -join ', '
            $reason += " $($orphans.Count) llama-server process(es) (pid $ids) whose parent was the notebook's recorded server (pid $($Record.Pid), no longer running) may still hold GPU memory. A dead parent cannot be re-verified, so they are not touched; end them from Task Manager if needed."
        }
    } elseif ($owners.Count -eq 0) {
        $reason = "Nothing is listening on port $Port; the notebook's server (pid $($routerIds -join ', ')) may still be starting or may have died. No unload request can be sent; the processes are ended by identity."
    } elseif (@($owners | Where-Object { $_ -notin $routerIds }).Count -gt 0) {
        $foreign = @($owners | Where-Object { $_ -notin $routerIds })
        $reason = "Port $Port is owned by process $($foreign -join ', '), which is not the notebook's server (pid $($routerIds -join ', ')). No unload request is sent to it."
    } else {
        $portOk = $true
    }
    [pscustomobject]@{
        Routers         = $routers
        Helpers         = $helpers
        Orphans         = $orphans
        ProcessIdsToEnd = @(($routers + $helpers) | ForEach-Object { [int]$_.ProcessId })
        PortOk          = $portOk
        PortOwners      = $owners
        Reason          = $reason
    }
}

# ---------------------------------------------------------------------------
# Pure functions: command lines, parsing tool output, action results
# ---------------------------------------------------------------------------

function ConvertTo-CommandLineArgument {
    # Quotes one argument the way CreateProcess / CommandLineToArgvW expect:
    # wrap in quotes if it has whitespace or quotes, escape embedded quotes,
    # and double any backslashes that precede a quote or end the value.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $i = 0
    $n = $Value.Length
    while ($i -lt $n) {
        $backslashes = 0
        while ($i -lt $n -and $Value[$i] -eq '\') { $backslashes++; $i++ }
        if ($i -eq $n) {
            [void]$sb.Append('\', $backslashes * 2)
        } elseif ($Value[$i] -eq '"') {
            [void]$sb.Append('\', $backslashes * 2 + 1)
            [void]$sb.Append('"')
            $i++
        } else {
            [void]$sb.Append('\', $backslashes)
            [void]$sb.Append($Value[$i])
            $i++
        }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function New-ActionArgumentList {
    # The powershell.exe arguments that run action.ps1, already quoted, because
    # Start-Process joins -ArgumentList with spaces and adds no quotes of its own.
    param(
        [Parameter(Mandatory)][string]$ScriptPath,
        [Parameter(Mandatory)][string]$Action,
        [string]$Model = '',
        [switch]$OpenBrowser
    )
    $list = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
              '-File', (ConvertTo-CommandLineArgument $ScriptPath), '-Action', (ConvertTo-CommandLineArgument $Action))
    if ($Model) { $list += @('-Model', (ConvertTo-CommandLineArgument $Model)) }
    if ($OpenBrowser) { $list += '-OpenBrowser' }
    return $list
}

function Resolve-ActionOutcome {
    # What to tell the user when an action process has exited. The result
    # file counts only if this action wrote it (StartedAt not before the launch);
    # otherwise the process died before writing and we say so, with its exit code.
    param([Parameter(Mandatory)][string]$LastActionPath, [Parameter(Mandatory)][datetime]$LaunchedAt, [int]$ExitCode = -1)
    $notRun = [pscustomobject]@{
        Ran = $false; Ok = $false
        Message = "The action did not run to completion (exit code $ExitCode). Use 'Open log folder' to see why."
    }
    if (-not (Test-Path $LastActionPath)) { return $notRun }
    try {
        $la = Get-Content -Path $LastActionPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch { return $notRun }
    if ($null -eq $la -or -not $la.PSObject.Properties['StartedAt']) { return $notRun }
    $started = ConvertFrom-RoundtripTime $la.StartedAt
    if ($null -eq $started -or $started -lt $LaunchedAt.AddSeconds(-2)) { return $notRun }
    $ok = $false
    if ($la.PSObject.Properties['Ok']) { $ok = [bool]$la.Ok }
    $message = ''
    if ($la.PSObject.Properties['Message']) { $message = "$($la.Message)" }
    [pscustomobject]@{ Ran = $true; Ok = $ok; Message = $message }
}

function ConvertFrom-NvidiaSmiMemory {
    # Parses: nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if (-not $Text) { return $null }
    $line = @($Text -split "`r?`n" | Where-Object { $_.Trim() }) | Select-Object -First 1
    if (-not $line) { return $null }
    $parts = $line -split ','
    if ($parts.Count -lt 2) { return $null }
    $used = 0; $total = 0
    if (-not [int]::TryParse($parts[0].Trim(), [ref]$used)) { return $null }
    if (-not [int]::TryParse($parts[1].Trim(), [ref]$total)) { return $null }
    [pscustomobject]@{ UsedMiB = $used; TotalMiB = $total }
}

function Format-GpuGb {
    param([Parameter(Mandatory)][double]$Mib)
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.0} GB', ($Mib / 1024))
}

function Format-Bytes {
    param([AllowNull()]$Bytes)
    if ($null -eq $Bytes) { return '' }
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0:0.0} GB', ([double]$Bytes / 1GB))
}

function ConvertFrom-DockerPsLines {
    # Parses: docker ps --format "{{.Names}}|{{.Status}}"
    param([AllowEmptyCollection()][AllowNull()][string[]]$Lines)
    $out = @()
    foreach ($line in @($Lines)) {
        if (-not $line) { continue }
        $parts = $line -split '\|', 2
        $status = ''
        if ($parts.Count -gt 1) { $status = $parts[1].Trim() }
        $out += [pscustomobject]@{ Name = $parts[0].Trim(); Status = $status }
    }
    return $out
}

function Get-ModelLabel {
    param([string]$Id)
    if ($script:ModelLabels.ContainsKey($Id)) { return $script:ModelLabels[$Id] }
    return $Id
}

# ---------------------------------------------------------------------------
# Machine access (thin, side effects only)
# ---------------------------------------------------------------------------

function Invoke-NativeMerged {
    # Runs a program and captures its output and error streams as plain text,
    # returning them with the exit code. It never goes through PowerShell's
    # error stream: Windows PowerShell 5.1 turns a native program's stderr
    # lines into error records when they are redirected, and under
    # ErrorAction Stop the first such line aborts the caller. docker compose
    # prints its progress on stderr, which is exactly what broke the first
    # live Stop. Everything that runs docker or nvidia-smi goes through here.
    # A program that runs past -TimeoutSec is killed and TimedOut is set.
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = '',
        [int]$TimeoutSec = 120,
        [switch]$NoQuoting
    )
    if ($NoQuoting) { $quoted = @($ArgumentList) }
    else { $quoted = @($ArgumentList | ForEach-Object { ConvertTo-CommandLineArgument -Value "$_" }) }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = ($quoted -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    $timedOut = $false
    if (-not $p.WaitForExit([int]($TimeoutSec * 1000))) {
        $timedOut = $true
        try { $p.Kill() } catch { }
        $p.WaitForExit()
    }
    $out = $outTask.Result
    $err = $errTask.Result
    $lines = @()
    foreach ($chunk in @($out, $err)) {
        if ($chunk) { $lines += @($chunk -split "`r?`n" | Where-Object { $_ -ne '' }) }
    }
    if ($timedOut) { $lines += "(killed after $TimeoutSec s without finishing)" }
    [pscustomobject]@{ ExitCode = $p.ExitCode; Lines = $lines; StdOut = "$out"; StdErr = "$err"; TimedOut = $timedOut }
}

function Get-LlamaProcessSnapshot {
    # Every llama-server.exe on the PC, with the fields the pure functions need.
    @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject]@{
            ProcessId       = [int]$_.ProcessId
            ParentProcessId = [int]$_.ParentProcessId
            Name            = $_.Name
            CommandLine     = "$($_.CommandLine)"
            CreationDate    = $_.CreationDate
        }
    })
}

function Get-ProcessSnapshotById {
    param([Parameter(Mandatory)][int]$ProcessId)
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $p) { return $null }
    [pscustomobject]@{
        ProcessId       = [int]$p.ProcessId
        ParentProcessId = [int]$p.ParentProcessId
        Name            = $p.Name
        CommandLine     = "$($p.CommandLine)"
        CreationDate    = $p.CreationDate
    }
}

function Get-ListenConnections {
    param([int]$Port = $script:LlamaPort)
    @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-GpuMemory {
    $cmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    try {
        $r = Invoke-NativeMerged -FilePath $cmd.Source -ArgumentList @('--query-gpu=memory.used,memory.total', '--format=csv,noheader,nounits') -TimeoutSec 10
        if ($r.ExitCode -ne 0 -or $r.TimedOut) { return $null }
        return ConvertFrom-NvidiaSmiMemory -Text $r.StdOut
    } catch { return $null }
}

function Invoke-LlamaApi {
    param([string]$Method = 'Get', [Parameter(Mandatory)][string]$Path, $Body = $null, [int]$TimeoutSec = 30)
    $uri = "$script:LlamaBaseUrl$Path"
    if ($null -ne $Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -ContentType 'application/json' -Body ($Body | ConvertTo-Json -Compress) -TimeoutSec $TimeoutSec
    }
    return Invoke-RestMethod -Method $Method -Uri $uri -TimeoutSec $TimeoutSec
}

function Test-LlamaHealth {
    try { $h = Invoke-LlamaApi -Path '/health' -TimeoutSec 3; return ("$($h.status)" -eq 'ok') } catch { return $false }
}

function Get-LlamaModels {
    # GET /models (the router's management list, with load status), or $null if unreachable.
    param([int]$TimeoutSec = 5)
    try { return Invoke-LlamaApi -Path '/models' -TimeoutSec $TimeoutSec } catch { return $null }
}

function ConvertTo-ModelStates {
    param([AllowNull()]$Payload)
    $states = @()
    if ($null -eq $Payload -or -not $Payload.PSObject.Properties['data']) { return $states }
    foreach ($item in @($Payload.data)) {
        if ($null -eq $item) { continue }
        $status = 'unknown'; $itemArgs = @(); $size = $null; $failed = $false
        if ($item.PSObject.Properties['status'] -and $null -ne $item.status) {
            $status = "$($item.status.value)"
            if ($item.status.PSObject.Properties['args']) { $itemArgs = @($item.status.args) }
            if ($item.status.PSObject.Properties['failed']) { $failed = [bool]$item.status.failed }
        }
        if ($item.PSObject.Properties['meta'] -and $null -ne $item.meta -and $item.meta.PSObject.Properties['size']) { $size = $item.meta.size }
        $states += [pscustomobject]@{
            Id          = "$($item.id)"
            Label       = Get-ModelLabel -Id "$($item.id)"
            Status      = $status
            Failed      = $failed
            IsEmbedding = ($itemArgs -contains '--embeddings')
            SizeBytes   = $size
        }
    }
    return $states
}

# ---- the shared "one action at a time" lock ---------------------------------

function Enter-NotebookActionLock {
    # Returns the held mutex, or $null if another start/stop/load/unload is
    # running anywhere on this desktop (tray, terminal scripts, action.ps1).
    param([int]$TimeoutMs = 0)
    $m = New-Object System.Threading.Mutex($false, $script:ActionMutexName)
    $got = $false
    try { $got = $m.WaitOne($TimeoutMs) } catch [System.Threading.AbandonedMutexException] { $got = $true }
    if (-not $got) { $m.Dispose(); return $null }
    return $m
}

function Exit-NotebookActionLock {
    param($Mutex)
    if ($null -eq $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch { }
    try { $Mutex.Dispose() } catch { }
}

# ---- Docker -----------------------------------------------------------------

function Add-DockerToPath {
    $bin = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin'
    if ((Test-Path $bin) -and ($env:Path -notlike "*$bin*")) { $env:Path = "$bin;$env:Path" }
}

function Get-DockerExe {
    Add-DockerToPath
    $c = Get-Command docker -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

function Test-DockerDesktopProcess {
    return [bool](Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)
}

function Test-DockerEngine {
    $docker = Get-DockerExe
    if (-not $docker) { return $false }
    try {
        $r = Invoke-NativeMerged -FilePath $docker -ArgumentList @('version', '--format', '{{.Server.Version}}') -TimeoutSec 20
        return (($r.ExitCode -eq 0) -and -not $r.TimedOut -and [bool]$r.StdOut.Trim())
    } catch { return $false }
}

function Start-DockerDesktop {
    param([int]$TimeoutSec = 180)
    $dd = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
    if (-not (Test-Path $dd)) { throw "Docker Desktop not found at $dd" }
    Write-ControlLog 'Starting Docker Desktop...'
    Start-Process -FilePath $dd | Out-Null
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while (-not (Test-DockerEngine)) {
        if ((Get-Date) -gt $deadline) { throw "Docker engine did not come up within $TimeoutSec seconds" }
        Start-Sleep -Seconds 5
    }
}

function Get-NotebookContainers {
    if (-not (Test-DockerDesktopProcess)) { return @() }
    $docker = Get-DockerExe
    if (-not $docker) { return @() }
    try {
        $r = Invoke-NativeMerged -FilePath $docker -ArgumentList @('ps', '--filter', "label=com.docker.compose.project=$script:ComposeProject", '--format', '{{.Names}}|{{.Status}}') -TimeoutSec 20
        if ($r.ExitCode -ne 0 -or $r.TimedOut) { return @() }
        return @(ConvertFrom-DockerPsLines -Lines @($r.StdOut -split "`r?`n"))
    } catch { return @() }
}

function Invoke-Compose {
    # Runs "docker compose <arguments>" in the compose folder; every output
    # line goes to the control log; returns the exit code (-1 if killed for
    # running past the timeout).
    param([Parameter(Mandatory)][string[]]$Arguments, [int]$TimeoutSec = 300)
    $docker = Get-DockerExe
    if (-not $docker) { throw 'docker.exe not found. Is Docker Desktop installed?' }
    $r = Invoke-NativeMerged -FilePath $docker -ArgumentList (@('compose') + $Arguments) -WorkingDirectory $script:ComposeDir -TimeoutSec $TimeoutSec
    foreach ($line in $r.Lines) { Write-ControlLog -Message "  compose: $line" -Quiet }
    if ($r.TimedOut) { return -1 }
    return $r.ExitCode
}

function Test-NotebookApiHealthy {
    try { $h = Invoke-RestMethod -Uri $script:ApiHealthUrl -TimeoutSec 5; return ("$($h.status)" -eq 'healthy') } catch { return $false }
}

# ---------------------------------------------------------------------------
# The notebook's llama-server: start and stop by identity
# ---------------------------------------------------------------------------

function Get-StopPlan {
    # The read-only dry run: what Stop would touch right now.
    $record = Read-ServerRecord -Path $script:RecordPath
    $snapshot = Get-LlamaProcessSnapshot
    $conns = Get-ListenConnections -Port $script:LlamaPort
    return Resolve-StopPlan -Processes $snapshot -ModelsIniPath $script:ModelsIni -Record $record -Connections $conns -Port $script:LlamaPort
}

function Stop-ProcessByIdentity {
    # Ends one process from an earlier snapshot, but only if the live process
    # with that id is still the same llama-server (same name, same start time).
    param([Parameter(Mandatory)]$Process)
    $cur = Get-ProcessSnapshotById -ProcessId ([int]$Process.ProcessId)
    if (-not $cur) { return 'already gone' }
    if (-not (Test-SameProcess -Snapshot $Process -Current $cur)) {
        return "skipped: pid $($Process.ProcessId) now belongs to another process ($($cur.Name), started $($cur.CreationDate))"
    }
    try {
        Stop-Process -Id ([int]$Process.ProcessId) -Force -ErrorAction Stop
    } catch {
        return "failed: $($_.Exception.Message)"
    }
    return 'ended'
}

function Wait-ProcessesGone {
    param([Parameter(Mandatory)][AllowEmptyCollection()][int[]]$ProcessIds, [int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $alive = @($ProcessIds | Where-Object { $p = Get-ProcessSnapshotById -ProcessId $_; $p -and "$($p.Name)" -eq 'llama-server.exe' })
        if ($alive.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-NotebookLlamaServer {
    # Stops the notebook's own model server and nothing else:
    #  1. identify our router (record, else command line) and its helpers (parentage + age)
    #  2. if port 8080 is ours: ask it to unload every model it holds, wait until it reports none
    #  3. end the router by identity, then any helper still alive (re-listed right before), by identity
    #  4. wait for them to be gone (GPU memory is released only then), remove the record
    param([int]$UnloadTimeoutSec = 60, [int]$ExitTimeoutSec = 30)

    $plan = Get-StopPlan
    $result = [pscustomobject]@{
        WasRunning     = (@($plan.Routers).Count -gt 0)
        RouterIds      = @($plan.Routers | ForEach-Object { [int]$_.ProcessId })
        HelperIds      = @($plan.Helpers | ForEach-Object { [int]$_.ProcessId })
        PortOk         = $plan.PortOk
        Reason         = $plan.Reason
        ModelsUnloaded = @()
        Ended          = @()
        AllGone        = $true
        Notes          = @()
    }

    if (-not $result.WasRunning) {
        Write-ControlLog "Model server: not running. $($plan.Reason)"
        if (@($plan.Orphans).Count -gt 0) { $result.Notes += $plan.Reason }
        if (Test-Path $script:RecordPath) { Remove-Item $script:RecordPath -Force -ErrorAction SilentlyContinue }
        return $result
    }

    Write-ControlLog ("Model server: router pid {0}, helpers {1}" -f ($result.RouterIds -join ', '), $(if ($result.HelperIds.Count) { $result.HelperIds -join ', ' } else { 'none' }))

    if ($plan.PortOk) {
        $payload = Get-LlamaModels -TimeoutSec 10
        $toUnload = @(Select-ModelsToUnload -Payload $payload)
        foreach ($id in $toUnload) {
            try {
                Write-ControlLog "  unloading $id ..."
                Invoke-LlamaApi -Method Post -Path '/models/unload' -Body @{ model = $id } -TimeoutSec 60 | Out-Null
                $result.ModelsUnloaded += $id
            } catch {
                $result.Notes += "unload of $id failed: $($_.Exception.Message)"
                Write-ControlLog "  unload of $id failed: $($_.Exception.Message)"
            }
        }
        $deadline = (Get-Date).AddSeconds($UnloadTimeoutSec)
        $stillLoaded = @()
        while ((Get-Date) -lt $deadline) {
            $p = Get-LlamaModels -TimeoutSec 10
            if ($null -eq $p) { $stillLoaded = @(); break }
            $stillLoaded = @(Select-ModelsToUnload -Payload $p)
            if ($stillLoaded.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
        if ($stillLoaded.Count -gt 0) {
            $result.Notes += "the server still reported $($stillLoaded -join ', ') loaded after $UnloadTimeoutSec s; its processes are ended anyway"
            Write-ControlLog "  still loaded after $UnloadTimeoutSec s: $($stillLoaded -join ', ')"
        } elseif ($toUnload.Count -gt 0) {
            Write-ControlLog ("  server reports {0} unloaded" -f ($result.ModelsUnloaded -join ', '))
        }
    } else {
        Write-ControlLog "  $($plan.Reason)"
        $result.Notes += $plan.Reason
    }

    # Helpers may have appeared since the plan was made (a model that began
    # loading in between): list them again right before ending the router.
    $fresh = Get-LlamaProcessSnapshot
    $helpers = @($plan.Helpers)
    foreach ($r in @($plan.Routers)) {
        foreach ($h in @(Get-RouterHelpers -Processes $fresh -Router $r)) {
            if (-not (@($helpers | ForEach-Object { [int]$_.ProcessId }) -contains [int]$h.ProcessId)) { $helpers += $h }
        }
    }

    foreach ($r in @($plan.Routers)) {
        $outcome = Stop-ProcessByIdentity -Process $r
        Write-ControlLog "  router pid $($r.ProcessId): $outcome"
        if ($outcome -eq 'ended') { $result.Ended += [int]$r.ProcessId } elseif ($outcome -like 'failed*') { $result.Notes += "router pid $($r.ProcessId) $outcome" }
    }
    foreach ($h in $helpers) {
        $outcome = Stop-ProcessByIdentity -Process $h
        Write-ControlLog "  helper pid $($h.ProcessId): $outcome"
        if ($outcome -eq 'ended') { $result.Ended += [int]$h.ProcessId } elseif ($outcome -like 'failed*') { $result.Notes += "helper pid $($h.ProcessId) $outcome" }
    }

    $toWait = @(@($plan.Routers) + $helpers | ForEach-Object { [int]$_.ProcessId })
    $result.AllGone = Wait-ProcessesGone -ProcessIds $toWait -TimeoutSec $ExitTimeoutSec
    if (-not $result.AllGone) {
        $result.Notes += "some of the notebook's server processes were still running after $ExitTimeoutSec s"
        Write-ControlLog "  warning: still running after $ExitTimeoutSec s"
    }
    if (Test-Path $script:RecordPath) { Remove-Item $script:RecordPath -Force -ErrorAction SilentlyContinue }
    return $result
}

function New-LlamaServerArgumentList {
    # The command line handed to llama-server (router mode).
    # --no-models-autoload: the server loads a model only when asked to through
    # its /models/load call (the tray, the Model memory panel) or through the
    # load-on-startup lines in models.ini. A chat or embedding request naming a
    # model that is not loaded fails instead of loading it. By design, nothing
    # loads a model unless the user asks.
    return @(
        '--models-preset', (ConvertTo-CommandLineArgument $script:ModelsIni),
        '--host', '0.0.0.0',
        '--port', "$script:LlamaPort",
        '--models-max', '2',
        '--no-models-autoload'
    )
}

function Start-NotebookLlamaServer {
    # Starts the notebook's model server (router mode, our models.ini, port 8080)
    # unless it is already running and healthy. A server of ours that does not
    # answer is given time first (it may still be starting); only after that is
    # it ended and replaced. Refuses to start if another program owns port
    # 8080. Records the new process's identity for Stop.
    param([int]$HealthTimeoutSec = 30)

    if (-not (Test-Path $script:LlamaExe)) { throw "llama-server.exe not found at $script:LlamaExe" }
    if (-not (Test-Path $script:ModelsIni)) { throw "models.ini not found at $script:ModelsIni" }
    New-Item -ItemType Directory -Force -Path $script:LlamaLogDir | Out-Null

    $plan = Get-StopPlan
    if (@($plan.Routers).Count -gt 0) {
        $router = $plan.Routers[0]
        $ageSec = ((Get-Date) - [datetime]$router.CreationDate).TotalSeconds
        $grace = 15
        if ($ageSec -lt 60) { $grace = 45 }
        $deadline = (Get-Date).AddSeconds($grace)
        $healthy = $false
        do {
            if ($plan.PortOk -and (Test-LlamaHealth)) { $healthy = $true; break }
            Start-Sleep -Seconds 1
            $plan = Get-StopPlan
            if (@($plan.Routers).Count -eq 0) { break }
        } while ((Get-Date) -lt $deadline)

        if ($healthy) {
            $record = Read-ServerRecord -Path $script:RecordPath
            if (-not $record -or -not (Test-RecordMatchesProcess -Record $record -Process $router)) {
                Write-ServerRecord -Record (New-ServerRecord -Process $router -Port $script:LlamaPort) -Path $script:RecordPath
            }
            Write-ControlLog "Model server: already running and healthy (pid $($router.ProcessId))."
            return [pscustomobject]@{ Started = $false; AlreadyRunning = $true; Pid = [int]$router.ProcessId; Message = "Model server already running (pid $($router.ProcessId))" }
        }
        if (@($plan.Routers).Count -gt 0) {
            Write-ControlLog ("Model server: our server (pid {0}, started {1:0} s ago) did not answer on port {2} within {3} s (port owners: {4}); ending it and starting a fresh one." -f $router.ProcessId, $ageSec, $script:LlamaPort, $grace, $(if ($plan.PortOwners.Count) { $plan.PortOwners -join ', ' } else { 'none' }))
            Stop-NotebookLlamaServer | Out-Null
        }
    }

    $owners = @(Get-PortOwners -Connections (Get-ListenConnections -Port $script:LlamaPort) -Port $script:LlamaPort)
    if ($owners.Count -gt 0) {
        $names = @($owners | ForEach-Object { $p = Get-ProcessSnapshotById -ProcessId $_; if ($p) { "$($p.Name) (pid $_)" } else { "pid $_" } })
        throw "Port $script:LlamaPort is in use by $($names -join ', '), which is not the notebook's model server. Not starting a second server on the same port."
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $argumentList = @(New-LlamaServerArgumentList)
    Write-ControlLog 'Model server: starting llama-server (router mode, autoload off)...'
    $p = Start-Process -FilePath $script:LlamaExe -ArgumentList $argumentList -WorkingDirectory $script:LlamaDir `
        -RedirectStandardOutput (Join-Path $script:LlamaLogDir "llama-server-$stamp.out.log") `
        -RedirectStandardError  (Join-Path $script:LlamaLogDir "llama-server-$stamp.err.log") `
        -WindowStyle Hidden -PassThru

    $entry = $null
    for ($i = 0; $i -lt 20 -and -not $entry; $i++) { $entry = Get-ProcessSnapshotById -ProcessId $p.Id; if (-not $entry) { Start-Sleep -Milliseconds 250 } }
    if (-not $entry) { throw "llama-server (pid $($p.Id)) exited immediately. Check $script:LlamaLogDir\llama-server-$stamp.err.log" }
    if ("$($entry.Name)" -ne 'llama-server.exe') { throw "Process $($p.Id) is not llama-server.exe (found $($entry.Name)); not recording it." }
    Write-ServerRecord -Record (New-ServerRecord -Process $entry -Port $script:LlamaPort) -Path $script:RecordPath

    $deadline = (Get-Date).AddSeconds($HealthTimeoutSec)
    while (-not (Test-LlamaHealth) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1 }
    $healthy = Test-LlamaHealth
    $ownsPort = Test-PortOwnedBy -Connections (Get-ListenConnections -Port $script:LlamaPort) -Port $script:LlamaPort -ExpectedPid $p.Id
    if (-not $healthy -or -not $ownsPort) {
        throw "llama-server (pid $($p.Id)) started but is not answering on port $script:LlamaPort as expected. Check $script:LlamaLogDir\llama-server-$stamp.err.log"
    }
    Write-ControlLog "Model server: started (pid $($p.Id)); no model is loaded until you load one (tray menu or Model memory panel)."
    return [pscustomobject]@{ Started = $true; AlreadyRunning = $false; Pid = [int]$p.Id; Message = "Model server started (pid $($p.Id))" }
}

function Invoke-NotebookModelAction {
    # Load or unload one model on the notebook's own server (what the sidebar
    # switch does). Refuses if port 8080 is not ours or another action runs.
    param([Parameter(Mandatory)][ValidateSet('load', 'unload')][string]$Action, [Parameter(Mandatory)][string]$Model)
    $lock = Enter-NotebookActionLock
    if ($null -eq $lock) { throw 'Another start or stop is already running; nothing was done.' }
    try {
        $plan = Get-StopPlan
        if (@($plan.Routers).Count -eq 0) { throw "The notebook's model server is not running." }
        if (-not $plan.PortOk) { throw $plan.Reason }
        $payload = Get-LlamaModels -TimeoutSec 10
        $known = @(ConvertTo-ModelStates -Payload $payload | ForEach-Object { $_.Id })
        if ($Model -notin $known) { throw "Model '$Model' is not known to the notebook's server (known: $($known -join ', '))." }
        Write-ControlLog "Model server: $Action $Model"
        Invoke-LlamaApi -Method Post -Path "/models/$Action" -Body @{ model = $Model } -TimeoutSec 180 | Out-Null
        return "$Action requested for $(Get-ModelLabel -Id $Model)."
    } finally { Exit-NotebookActionLock $lock }
}

# ---------------------------------------------------------------------------
# The whole notebook
# ---------------------------------------------------------------------------

function Start-Notebook {
    param([switch]$OpenBrowser, [int]$ApiTimeoutSec = 180)
    $lock = Enter-NotebookActionLock
    if ($null -eq $lock) { throw 'Another start or stop is already running; nothing was done.' }
    try {
        $started = Get-Date
        Write-ControlLog '[1/3] Docker Desktop'
        if (-not (Test-DockerEngine)) { Start-DockerDesktop } else { Write-ControlLog '  engine ready' }

        Write-ControlLog '[2/3] Model server'
        $llama = Start-NotebookLlamaServer

        Write-ControlLog '[3/3] Notebook containers'
        $code = Invoke-Compose -Arguments @('up', '-d')
        if ($code -ne 0) { throw "docker compose up failed (exit $code). See notebook-control\logs." }

        Write-ControlLog '  waiting for the notebook to answer...'
        $deadline = (Get-Date).AddSeconds($ApiTimeoutSec)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            if (Test-NotebookApiHealthy) { $ready = $true; break }
            Start-Sleep -Seconds 5
        }
        if (-not $ready) { throw "The notebook did not answer within $ApiTimeoutSec s. Containers are up; check: docker compose -f `"$script:ComposeDir\docker-compose.yml`" logs open_notebook" }

        if ($OpenBrowser) { Start-Process $script:UiUrl | Out-Null }
        $seconds = [int]((Get-Date) - $started).TotalSeconds
        $msg = "Notebook is on ($seconds s). $script:UiUrl"
        Write-ControlLog $msg
        return [pscustomobject]@{ Ok = $true; Message = $msg; Seconds = $seconds; ModelServer = $llama }
    } finally { Exit-NotebookActionLock $lock }
}

function Stop-Notebook {
    param([int]$UnloadTimeoutSec = 60)
    $lock = Enter-NotebookActionLock
    if ($null -eq $lock) { throw 'Another start or stop is already running; nothing was done.' }
    try {
        $started = Get-Date
        $gpuBefore = Get-GpuMemory
        $ok = $true
        $notes = @()

        Write-ControlLog '[1/2] Notebook containers'
        $containersRemoved = $false
        if (Test-DockerEngine) {
            try {
                $code = Invoke-Compose -Arguments @('down')
                if ($code -ne 0) {
                    $ok = $false
                    $notes += "Removing the containers failed (docker compose down exit $code); see the log."
                    Write-ControlLog "  docker compose down failed (exit $code); continuing with the model server"
                } else {
                    $containersRemoved = $true
                    Write-ControlLog '  containers removed (data kept on disk)'
                }
            } catch {
                $ok = $false
                $notes += "Removing the containers failed: $($_.Exception.Message)"
                Write-ControlLog "  docker compose down failed: $($_.Exception.Message); continuing with the model server"
            }
        } else {
            Write-ControlLog '  Docker engine not running, so no containers to remove'
        }

        Write-ControlLog '[2/2] Model server'
        $server = Stop-NotebookLlamaServer -UnloadTimeoutSec $UnloadTimeoutSec
        if (-not $server.AllGone) { $ok = $false }
        if ($server.Notes.Count -gt 0) { $notes += $server.Notes }

        Start-Sleep -Seconds 2
        $gpuAfter = Get-GpuMemory
        $seconds = [int]((Get-Date) - $started).TotalSeconds

        $parts = @()
        if ($ok) { $parts += 'Notebook is off.' } else { $parts += 'Notebook stop finished with problems.' }
        if ($server.WasRunning) {
            if ($server.ModelsUnloaded.Count -gt 0) { $parts += "Unloaded $($server.ModelsUnloaded -join ' and ')." }
            else { $parts += 'The model server held no model.' }
        } else {
            $parts += 'The model server was not running.'
        }
        if ($gpuBefore -and $gpuAfter) { $parts += "GPU memory $(Format-GpuGb $gpuBefore.UsedMiB), now $(Format-GpuGb $gpuAfter.UsedMiB)." }
        if ($notes.Count -gt 0) { $parts += ($notes -join ' ') }
        $msg = $parts -join ' '
        Write-ControlLog $msg
        return [pscustomobject]@{
            Ok                = $ok
            Message           = $msg
            Seconds           = $seconds
            ContainersRemoved = $containersRemoved
            ModelServer       = $server
            GpuBeforeMiB      = $(if ($gpuBefore) { $gpuBefore.UsedMiB } else { $null })
            GpuAfterMiB       = $(if ($gpuAfter) { $gpuAfter.UsedMiB } else { $null })
        }
    } finally { Exit-NotebookActionLock $lock }
}

function Get-NotebookStatus {
    # Read-only. Nothing here loads, wakes or stops anything.
    $dockerDesktop = Test-DockerDesktopProcess
    $containers = @()
    if ($dockerDesktop) { $containers = @(Get-NotebookContainers) }
    $appUp = (@($containers | Where-Object { $_.Name -like '*open_notebook*' -and $_.Status -like 'Up*' }).Count -gt 0)
    $apiHealthy = $false
    if ($appUp) { $apiHealthy = Test-NotebookApiHealthy }

    $plan = Get-StopPlan
    $serverState = 'off'
    $serverPid = $null
    $models = @()
    if (@($plan.Routers).Count -gt 0) {
        $serverPid = [int]$plan.Routers[0].ProcessId
        if ($plan.PortOk) {
            $payload = Get-LlamaModels -TimeoutSec 5
            if ($null -ne $payload) { $serverState = 'on'; $models = @(ConvertTo-ModelStates -Payload $payload) }
            else { $serverState = 'starting' }
        } elseif (@($plan.PortOwners).Count -eq 0) {
            $serverState = 'starting'
        } else {
            $serverState = 'port conflict'
        }
    }
    $gpu = Get-GpuMemory
    $orphanCount = @($plan.Orphans).Count

    $overall = 'off'
    if ($apiHealthy -and $serverState -eq 'on') { $overall = 'on' }
    elseif ($appUp -or @($containers).Count -gt 0 -or $serverState -ne 'off') { $overall = 'partial' }

    $summaryParts = @()
    switch ($overall) {
        'on'      { $summaryParts += 'Notebook on' }
        'partial' { $summaryParts += 'Notebook partly up' }
        default   { $summaryParts += 'Notebook off' }
    }
    $chat = $models | Where-Object { -not $_.IsEmbedding } | Select-Object -First 1
    if ($chat) { $summaryParts += "chat model $($chat.Status)" }
    if ($orphanCount -gt 0) { $summaryParts += "$orphanCount leftover model process(es)" }
    if ($gpu) { $summaryParts += "GPU $(Format-GpuGb $gpu.UsedMiB) of $(Format-GpuGb $gpu.TotalMiB)" }
    $summary = $summaryParts -join ', '
    if ($summary.Length -gt 63) { $summary = $summary.Substring(0, 63) }

    [pscustomobject]@{
        Overall       = $overall
        DockerDesktop = $dockerDesktop
        Containers    = $containers
        ApiHealthy    = $apiHealthy
        ServerState   = $serverState
        ServerPid     = $serverPid
        PortReason    = $plan.Reason
        Orphans       = $orphanCount
        Models        = $models
        Gpu           = $gpu
        Summary       = $summary
        CheckedAt     = (Get-Date).ToString('o')
    }
}

Export-ModuleMember -Function *
