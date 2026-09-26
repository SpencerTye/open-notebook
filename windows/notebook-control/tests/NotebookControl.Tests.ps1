# Pester 3.4 tests for the pure decision logic in NotebookControl.psm1.
# Run:  Invoke-Pester -Path windows\notebook-control\tests
#
# Everything here works on plain objects (process snapshots, port tables,
# JSON payloads) so no process is started, stopped or queried by these tests.

$modulePath = Join-Path $PSScriptRoot '..\NotebookControl.psm1'
Import-Module $modulePath -Force

# ---- fixtures -------------------------------------------------------------

$iniPath = 'C:\Open Notebook\llama-server\models.ini'
$exe     = 'C:\Open Notebook\llama-server\bin\llama-server.exe'
$t0      = Get-Date '2026-09-14 19:27:52'

function New-Proc([int]$ProcessId, [int]$ParentProcessId, [string]$Name, [string]$CommandLine, [datetime]$CreationDate) {
    [pscustomobject]@{
        ProcessId       = $ProcessId
        ParentProcessId = $ParentProcessId
        Name            = $Name
        CommandLine     = $CommandLine
        CreationDate    = $CreationDate
    }
}

$router = New-Proc 10480 13920 'llama-server.exe' "`"$exe`" --models-preset `"$iniPath`" --host 0.0.0.0 --port 8080 --models-max 2 --no-models-autoload" $t0
$helperEmbed = New-Proc 33336 10480 'llama-server.exe' "`"$exe`" --embeddings --host 127.0.0.1 --pooling last --port 52248 --alias qwen3-embedding-4b --model `"C:/Open Notebook/llama-server/models/Qwen3-Embedding-4B-Q8_0.gguf`" --n-gpu-layers 999" $t0.AddSeconds(102)
$helperChat  = New-Proc 30976 10480 'llama-server.exe' "`"$exe`" --host 127.0.0.1 --port 58892 --alias gemma-4-26b-a4b --model `"C:/Open Notebook/llama-server/models/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf`" --n-gpu-layers 999" $t0.AddHours(4)
# Another llama.cpp router running for something else: same program name, different preset file.
$foreignRouter = New-Proc 4444 1 'llama-server.exe' "`"F:\LLM\llama.cpp\llama-server.exe`" --models-preset `"F:\LLM\other.ini`" --port 8090" $t0.AddMinutes(5)
$foreignHelper = New-Proc 4445 4444 'llama-server.exe' "`"F:\LLM\llama.cpp\llama-server.exe`" --alias other-model --model `"F:\LLM\models\other.gguf`" --port 50000" $t0.AddMinutes(6)
# A single-model llama-server (no router), also not ours.
$foreignSingle = New-Proc 5555 1 'llama-server.exe' "`"F:\LLM\llama.cpp\llama-server.exe`" -m `"F:\LLM\models\x.gguf`" --port 8081" $t0.AddMinutes(7)

$allProcs = @($router, $helperEmbed, $helperChat, $foreignRouter, $foreignHelper, $foreignSingle)

function New-Conn([string]$LocalAddress, [int]$LocalPort, [string]$State, [int]$OwningProcess) {
    [pscustomobject]@{ LocalAddress = $LocalAddress; LocalPort = $LocalPort; State = $State; OwningProcess = $OwningProcess }
}

# ---- Find-NotebookRouter ---------------------------------------------------

Describe 'Find-NotebookRouter' {

    Context 'without a record (fallback by command line)' {
        It 'returns only the router started with our models.ini, not other llama-servers' {
            $found = @(Find-NotebookRouter -Processes $allProcs -ModelsIniPath $iniPath)
            $found.Count | Should Be 1
            $found[0].ProcessId | Should Be 10480
        }

        It 'does not return the helpers even though their command line names our model files' {
            $found = @(Find-NotebookRouter -Processes @($helperEmbed, $helperChat) -ModelsIniPath $iniPath)
            $found.Count | Should Be 0
        }

        It 'matches the ini path regardless of slash direction and letter case' {
            $found = @(Find-NotebookRouter -Processes $allProcs -ModelsIniPath 'c:/open notebook/llama-server/MODELS.INI')
            $found.Count | Should Be 1
            $found[0].ProcessId | Should Be 10480
        }

        It 'returns nothing when only foreign servers are running' {
            $found = @(Find-NotebookRouter -Processes @($foreignRouter, $foreignHelper, $foreignSingle) -ModelsIniPath $iniPath)
            $found.Count | Should Be 0
        }

        It 'returns nothing for an empty process list' {
            $found = @(Find-NotebookRouter -Processes @() -ModelsIniPath $iniPath)
            $found.Count | Should Be 0
        }

        It 'does not match a preset path that merely starts with ours (models.ini.experimental)' {
            $variant = New-Proc 6100 1 'llama-server.exe' "`"$exe`" --models-preset `"$iniPath.experimental`" --host 0.0.0.0 --port 8090" $t0
            $found = @(Find-NotebookRouter -Processes @($variant, $foreignRouter) -ModelsIniPath $iniPath)
            $found.Count | Should Be 0
        }

        It 'does not match our models.ini on a different port: the notebook''s server lives on 8080' {
            $otherPort = New-Proc 6200 1 'llama-server.exe' "`"$exe`" --models-preset `"$iniPath`" --host 0.0.0.0 --port 8090" $t0
            $found = @(Find-NotebookRouter -Processes @($otherPort) -ModelsIniPath $iniPath)
            $found.Count | Should Be 0
        }

        It 'matches our models.ini with no --port given (llama-server defaults to 8080)' {
            $noPort = New-Proc 6300 1 'llama-server.exe' "`"$exe`" --models-preset `"$iniPath`"" $t0
            $found = @(Find-NotebookRouter -Processes @($noPort) -ModelsIniPath $iniPath)
            $found.Count | Should Be 1
            $found[0].ProcessId | Should Be 6300
        }
    }

    Context 'with a record written at start' {
        It 'returns the recorded process when id and start time match' {
            $record = New-ServerRecord -Process $router -Port 8080
            $found = @(Find-NotebookRouter -Processes $allProcs -ModelsIniPath $iniPath -Record $record)
            $found.Count | Should Be 1
            $found[0].ProcessId | Should Be 10480
        }

        It 'ignores a record whose process id now belongs to a process started at a different time' {
            # Same id as the foreign router, but recorded start time is different: the id was recycled.
            $record = New-ServerRecord -Process $foreignRouter -Port 8080
            $record.StartTime = ($t0.AddDays(-1)).ToString('o')
            $found = @(Find-NotebookRouter -Processes $allProcs -ModelsIniPath $iniPath -Record $record)
            ($found | Where-Object { $_.ProcessId -eq 4444 }).Count | Should Be 0
            # falls back to the command-line match
            $found.Count | Should Be 1
            $found[0].ProcessId | Should Be 10480
        }

        It 'ignores a record whose process no longer exists and falls back to the command line' {
            $gone = New-Proc 99999 1 'llama-server.exe' 'whatever' $t0
            $record = New-ServerRecord -Process $gone -Port 8080
            $found = @(Find-NotebookRouter -Processes $allProcs -ModelsIniPath $iniPath -Record $record)
            $found.Count | Should Be 1
            $found[0].ProcessId | Should Be 10480
        }
    }
}

# ---- Test-RecordMatchesProcess ---------------------------------------------

Describe 'Test-RecordMatchesProcess' {
    It 'is true for the same id and a start time within two seconds' {
        $record = New-ServerRecord -Process $router -Port 8080
        $later = New-Proc 10480 13920 'llama-server.exe' $router.CommandLine $t0.AddMilliseconds(1500)
        Test-RecordMatchesProcess -Record $record -Process $later | Should Be $true
    }

    It 'is false when the start time differs by more than two seconds' {
        $record = New-ServerRecord -Process $router -Port 8080
        $other = New-Proc 10480 13920 'llama-server.exe' $router.CommandLine $t0.AddSeconds(10)
        Test-RecordMatchesProcess -Record $record -Process $other | Should Be $false
    }

    It 'is false when the id differs' {
        $record = New-ServerRecord -Process $router -Port 8080
        Test-RecordMatchesProcess -Record $record -Process $helperChat | Should Be $false
    }

    It 'is false when the process is not llama-server.exe' {
        $record = New-ServerRecord -Process $router -Port 8080
        $impostor = New-Proc 10480 1 'notepad.exe' 'notepad' $t0
        Test-RecordMatchesProcess -Record $record -Process $impostor | Should Be $false
    }
}

# ---- Get-RouterHelpers -----------------------------------------------------

Describe 'Get-RouterHelpers' {
    It 'returns the direct children of our router and nothing else' {
        $helpers = @(Get-RouterHelpers -Processes $allProcs -Router $router)
        $helpers.Count | Should Be 2
        ($helpers.ProcessId | Sort-Object) -join ',' | Should Be '30976,33336'
    }

    It 'does not return the foreign router''s helper' {
        $helpers = @(Get-RouterHelpers -Processes $allProcs -Router $router)
        ($helpers | Where-Object { $_.ProcessId -eq 4445 }).Count | Should Be 0
    }

    It 'returns nothing when the router has no children' {
        $helpers = @(Get-RouterHelpers -Processes @($router, $foreignRouter) -Router $router)
        $helpers.Count | Should Be 0
    }

    It 'does not return a process older than the router even if its recorded parent id equals the router''s id' {
        # Windows never rewrites a process's parent id when the parent exits, and
        # reuses ids. A foreign server whose long-gone launcher had the id our
        # router now has looks like a child; its start time proves it is not.
        $olderStranger = New-Proc 6000 10480 'llama-server.exe' "`"F:\LLM\llama.cpp\llama-server.exe`" -m `"F:\LLM\models\y.gguf`" --port 8082" $t0.AddHours(-3)
        $helpers = @(Get-RouterHelpers -Processes @($router, $helperEmbed, $olderStranger) -Router $router)
        ($helpers.ProcessId | Sort-Object) -join ',' | Should Be '33336'
    }
}

Describe 'Test-SameProcess' {
    It 'is true for the same id, name and start time' {
        Test-SameProcess -Snapshot $router -Current $router | Should Be $true
    }

    It 'is false when the start time differs by more than two seconds (the id was reused)' {
        $reused = New-Proc 10480 1 'llama-server.exe' 'other' $t0.AddMinutes(1)
        Test-SameProcess -Snapshot $router -Current $reused | Should Be $false
    }

    It 'is false when the name differs' {
        $other = New-Proc 10480 1 'notepad.exe' 'notepad' $t0
        Test-SameProcess -Snapshot $router -Current $other | Should Be $false
    }

    It 'is false when the current process is missing' {
        Test-SameProcess -Snapshot $router -Current $null | Should Be $false
    }
}

# ---- port ownership --------------------------------------------------------

Describe 'Get-PortOwners' {
    It 'returns the distinct pids listening on the port' {
        $conns = @(
            (New-Conn '0.0.0.0' 8080 'Listen' 10480),
            (New-Conn '::' 8080 'Listen' 10480),
            (New-Conn '127.0.0.1' 8000 'Listen' 777),
            (New-Conn '0.0.0.0' 8080 'Established' 999)
        )
        $owners = @(Get-PortOwners -Connections $conns -Port 8080)
        $owners.Count | Should Be 1
        $owners[0] | Should Be 10480
    }

    It 'returns nothing when nobody listens on the port' {
        $conns = @((New-Conn '127.0.0.1' 8000 'Listen' 777))
        @(Get-PortOwners -Connections $conns -Port 8080).Count | Should Be 0
    }
}

Describe 'Test-PortOwnedBy' {
    It 'is true when every listener on the port is the expected process' {
        $conns = @((New-Conn '0.0.0.0' 8080 'Listen' 10480), (New-Conn '::' 8080 'Listen' 10480))
        Test-PortOwnedBy -Connections $conns -Port 8080 -ExpectedPid 10480 | Should Be $true
    }

    It 'is false when another process holds the port' {
        $conns = @((New-Conn '0.0.0.0' 8080 'Listen' 4444))
        Test-PortOwnedBy -Connections $conns -Port 8080 -ExpectedPid 10480 | Should Be $false
    }

    It 'is false when nobody listens on the port' {
        Test-PortOwnedBy -Connections @() -Port 8080 -ExpectedPid 10480 | Should Be $false
    }
}

# ---- Select-ModelsToUnload -------------------------------------------------

Describe 'Select-ModelsToUnload' {
    It 'picks loaded, loading and sleeping models and skips unloaded and downloading ones' {
        $payload = @'
{ "data": [
  { "id": "gemma-4-26b-a4b",     "status": { "value": "loaded" } },
  { "id": "qwen3-embedding-4b",  "status": { "value": "unloaded" } },
  { "id": "m-loading",           "status": { "value": "loading" } },
  { "id": "m-sleeping",          "status": { "value": "sleeping" } },
  { "id": "m-downloading",       "status": { "value": "downloading" } }
], "object": "list" }
'@ | ConvertFrom-Json
        $ids = @(Select-ModelsToUnload -Payload $payload)
        ($ids | Sort-Object) -join ',' | Should Be 'gemma-4-26b-a4b,m-loading,m-sleeping'
    }

    It 'returns nothing when every model is unloaded' {
        $payload = '{ "data": [ { "id": "a", "status": { "value": "unloaded" } } ] }' | ConvertFrom-Json
        @(Select-ModelsToUnload -Payload $payload).Count | Should Be 0
    }

    It 'returns nothing for an empty list' {
        $payload = '{ "data": [] }' | ConvertFrom-Json
        @(Select-ModelsToUnload -Payload $payload).Count | Should Be 0
    }
}

# ---- Resolve-StopPlan ------------------------------------------------------

Describe 'Resolve-StopPlan' {
    It 'lists our router and its two helpers, and approves the port, in the normal case' {
        $conns = @((New-Conn '0.0.0.0' 8080 'Listen' 10480))
        $plan = Resolve-StopPlan -Processes $allProcs -ModelsIniPath $iniPath -Connections $conns -Port 8080
        @($plan.Routers).Count | Should Be 1
        $plan.Routers[0].ProcessId | Should Be 10480
        (@($plan.Helpers).ProcessId | Sort-Object) -join ',' | Should Be '30976,33336'
        $plan.PortOk | Should Be $true
        (@($plan.ProcessIdsToEnd) | Sort-Object) -join ',' | Should Be '10480,30976,33336'
    }

    It 'never lists a foreign process, whatever is running' {
        $conns = @((New-Conn '0.0.0.0' 8080 'Listen' 10480))
        $plan = Resolve-StopPlan -Processes $allProcs -ModelsIniPath $iniPath -Connections $conns -Port 8080
        foreach ($foreignPid in 4444, 4445, 5555) {
            (@($plan.ProcessIdsToEnd) -contains $foreignPid) | Should Be $false
        }
    }

    It 'refuses the port when another process listens on 8080, so no unload request is sent there' {
        $conns = @((New-Conn '0.0.0.0' 8080 'Listen' 4444))
        $plan = Resolve-StopPlan -Processes $allProcs -ModelsIniPath $iniPath -Connections $conns -Port 8080
        $plan.PortOk | Should Be $false
        $plan.Reason | Should Match '8080'
        # our own processes are still identified and can still be ended by identity
        (@($plan.ProcessIdsToEnd) | Sort-Object) -join ',' | Should Be '10480,30976,33336'
    }

    It 'reports the notebook server as not running when nothing of ours is in the list' {
        $plan = Resolve-StopPlan -Processes @($foreignRouter, $foreignHelper, $foreignSingle) -ModelsIniPath $iniPath -Connections @() -Port 8080
        @($plan.Routers).Count | Should Be 0
        @($plan.ProcessIdsToEnd).Count | Should Be 0
        $plan.PortOk | Should Be $false
        $plan.Reason | Should Match 'not running'
    }

    It 'refuses the port when a foreign process listens on 8080 next to ours' {
        $conns = @((New-Conn '0.0.0.0' 8080 'Listen' 10480), (New-Conn '::' 8080 'Listen' 4444))
        $plan = Resolve-StopPlan -Processes $allProcs -ModelsIniPath $iniPath -Connections $conns -Port 8080
        $plan.PortOk | Should Be $false
    }

    It 'reports, but never ends, helpers left over from a recorded router that has died' {
        # The record names router 10480, which is gone; its helper 30976 is still
        # there holding GPU memory. It is reported so the user knows, but the
        # dead parent's id cannot be re-verified, so nothing is ended.
        $record = New-ServerRecord -Process $router -Port 8080
        $plan = Resolve-StopPlan -Processes @($helperChat, $foreignRouter, $foreignHelper) -ModelsIniPath $iniPath -Record $record -Connections @() -Port 8080
        @($plan.Routers).Count | Should Be 0
        @($plan.ProcessIdsToEnd).Count | Should Be 0
        (@($plan.Orphans).ProcessId -join ',') | Should Be '30976'
        $plan.Reason | Should Match 'not touched'
    }

    It 'does not report a process older than the recorded router as its orphan' {
        $record = New-ServerRecord -Process $router -Port 8080
        $older = New-Proc 6000 10480 'llama-server.exe' 'foreign' $t0.AddHours(-3)
        $plan = Resolve-StopPlan -Processes @($older) -ModelsIniPath $iniPath -Record $record -Connections @() -Port 8080
        @($plan.Orphans).Count | Should Be 0
    }
}

# ---- GPU memory parsing ----------------------------------------------------

Describe 'ConvertFrom-NvidiaSmiMemory' {
    It 'parses the used and total MiB from nvidia-smi csv output' {
        $m = ConvertFrom-NvidiaSmiMemory -Text "21472, 24576`r`n"
        $m.UsedMiB | Should Be 21472
        $m.TotalMiB | Should Be 24576
    }

    It 'returns null for empty or malformed output' {
        (ConvertFrom-NvidiaSmiMemory -Text '') | Should BeNullOrEmpty
        (ConvertFrom-NvidiaSmiMemory -Text 'No devices were found') | Should BeNullOrEmpty
    }
}

Describe 'Format-GpuGb' {
    It 'formats MiB as GB with one decimal' {
        Format-GpuGb -Mib 21472 | Should Be '21.0 GB'
        Format-GpuGb -Mib 24576 | Should Be '24.0 GB'
        Format-GpuGb -Mib 2150 | Should Be '2.1 GB'
    }
}

# ---- server record ---------------------------------------------------------

Describe 'server record' {
    It 'round-trips through a file' {
        $path = Join-Path $TestDrive 'notebook-server.json'
        $record = New-ServerRecord -Process $router -Port 8080
        Write-ServerRecord -Record $record -Path $path
        $back = Read-ServerRecord -Path $path
        $back.Pid | Should Be 10480
        $back.Port | Should Be 8080
        ([datetime]::Parse($back.StartTime)) | Should Be $t0
        Test-RecordMatchesProcess -Record $back -Process $router | Should Be $true
    }

    It 'reads null when the file is missing' {
        Read-ServerRecord -Path (Join-Path $TestDrive 'missing.json') | Should BeNullOrEmpty
    }

    It 'reads null when the file is not valid JSON' {
        $path = Join-Path $TestDrive 'bad.json'
        Set-Content -Path $path -Value 'not json at all'
        Read-ServerRecord -Path $path | Should BeNullOrEmpty
    }
}

# ---- running native programs -----------------------------------------------

Describe 'Invoke-NativeMerged' {
    It 'captures both output streams as text and returns the exit code, even under ErrorAction Stop' {
        # Windows PowerShell 5.1 turns a native program's stderr lines into
        # errors when they are redirected; with ErrorAction Stop the first line
        # aborts the caller. docker compose prints its progress on stderr, which
        # is exactly what broke the first live Stop. This must not throw.
        $ErrorActionPreference = 'Stop'
        $r = Invoke-NativeMerged -FilePath 'cmd.exe' -ArgumentList @('/c', 'echo hello & echo problem 1>&2 & exit 3')
        $r.ExitCode | Should Be 3
        ($r.Lines -join '|') | Should Match 'hello'
        ($r.Lines -join '|') | Should Match 'problem'
    }

    It 'returns exit code 0 and the output for a quiet success' {
        $r = Invoke-NativeMerged -FilePath 'cmd.exe' -ArgumentList @('/c', 'echo fine')
        $r.ExitCode | Should Be 0
        ($r.Lines -join '|') | Should Match 'fine'
    }
}

# ---- launching action.ps1 the way the tray does ----------------------------

Describe 'ConvertTo-CommandLineArgument' {
    It 'leaves a plain token alone' {
        ConvertTo-CommandLineArgument -Value 'stop' | Should Be 'stop'
    }
    It 'quotes a value with spaces' {
        ConvertTo-CommandLineArgument -Value 'C:\Open Notebook\x.ps1' | Should Be '"C:\Open Notebook\x.ps1"'
    }
    It 'doubles a trailing backslash inside quotes so the closing quote survives' {
        ConvertTo-CommandLineArgument -Value 'C:\Open Notebook\' | Should Be '"C:\Open Notebook\\"'
    }
    It 'escapes an embedded quote' {
        ConvertTo-CommandLineArgument -Value 'say "hi"' | Should Be '"say \"hi\""'
    }
}

Describe 'New-ActionArgumentList' {
    $actionScript = Join-Path $PSScriptRoot '..\action.ps1'

    It 'quotes the script path, which contains a space, so powershell.exe receives it whole' {
        $list = @(New-ActionArgumentList -ScriptPath 'C:\Open Notebook\notebook-control\action.ps1' -Action 'stop')
        $i = [array]::IndexOf($list, '-File')
        $i | Should Not Be -1
        $list[$i + 1] | Should Be '"C:\Open Notebook\notebook-control\action.ps1"'
        ($list -contains '-Action') | Should Be $true
        $list[[array]::IndexOf($list, '-Action') + 1] | Should Be 'stop'
        ($list -contains '-OpenBrowser') | Should Be $false
    }

    It 'adds the model and the open-browser switch when asked' {
        $list = @(New-ActionArgumentList -ScriptPath 'C:\x\action.ps1' -Action 'unload' -Model 'gemma-4-26b-a4b' -OpenBrowser)
        $list[[array]::IndexOf($list, '-Model') + 1] | Should Be 'gemma-4-26b-a4b'
        ($list -contains '-OpenBrowser') | Should Be $true
    }

    It 'produces a command line that reaches action.ps1 (probe: an invalid action fails validation, not file lookup)' {
        # Runs the real action.ps1 with an action outside its ValidateSet, so
        # parameter binding fails before any code runs: no side effects, but it
        # proves the quoted path was received whole. The bug this guards against
        # made powershell.exe complain that "S:\RAG" is not a .ps1 file.
        $list = @(New-ActionArgumentList -ScriptPath (Resolve-Path $actionScript).Path -Action 'nothing')
        $r = Invoke-NativeMerged -FilePath 'powershell.exe' -ArgumentList $list -TimeoutSec 60 -NoQuoting
        $r.ExitCode | Should Not Be 0
        $r.StdErr | Should Not Match "does not have a '.ps1' extension"
        $r.StdErr | Should Match 'nothing'
    }
}

Describe 'New-LlamaServerArgumentList' {
    # The exact command line Start-NotebookLlamaServer hands to llama-server.
    $paths = Get-NotebookControlPaths
    $list = @(New-LlamaServerArgumentList)

    It 'names our models.ini as the preset, quoted when the path contains a space' {
        $i = [array]::IndexOf($list, '--models-preset')
        $i | Should Not Be -1
        $list[$i + 1] | Should Be (ConvertTo-CommandLineArgument $paths.ModelsIni)
    }

    It 'listens on all interfaces on port 8080 with at most two models resident' {
        $list[[array]::IndexOf($list, '--host') + 1] | Should Be '0.0.0.0'
        $list[[array]::IndexOf($list, '--port') + 1] | Should Be '8080'
        $list[[array]::IndexOf($list, '--models-max') + 1] | Should Be '2'
    }

    It 'turns autoload off: no model loads unless the user asks for it' {
        ($list -contains '--no-models-autoload') | Should Be $true
        ($list -contains '--models-autoload') | Should Be $false
    }
}

Describe 'Resolve-ActionOutcome' {
    $launchedAt = Get-Date '2026-09-15 01:00:00'

    function Write-Result([string]$Path, [bool]$Ok, [string]$Message, [datetime]$StartedAt) {
        [pscustomobject]@{ Action = 'stop'; Ok = $Ok; Message = $Message; StartedAt = $StartedAt.ToString('o') } | ConvertTo-Json | Set-Content -Path $Path -Encoding UTF8
    }

    It 'reports that the action did not run when there is no result file' {
        $o = Resolve-ActionOutcome -LastActionPath (Join-Path $TestDrive 'none.json') -LaunchedAt $launchedAt -ExitCode 1
        $o.Ran | Should Be $false
        $o.Ok | Should Be $false
        $o.Message | Should Match 'did not run'
        $o.Message | Should Match '1'
    }

    It 'ignores a result file left by an earlier action' {
        $path = Join-Path $TestDrive 'stale.json'
        Write-Result $path $true 'Notebook is on (94 s).' $launchedAt.AddMinutes(-10)
        $o = Resolve-ActionOutcome -LastActionPath $path -LaunchedAt $launchedAt -ExitCode 0
        $o.Ran | Should Be $false
        $o.Ok | Should Be $false
        $o.Message | Should Not Match 'Notebook is on'
    }

    It 'returns the message and flag of a result written by this action' {
        $path = Join-Path $TestDrive 'fresh.json'
        Write-Result $path $false 'stop failed: something' $launchedAt.AddSeconds(2)
        $o = Resolve-ActionOutcome -LastActionPath $path -LaunchedAt $launchedAt -ExitCode 1
        $o.Ran | Should Be $true
        $o.Ok | Should Be $false
        $o.Message | Should Be 'stop failed: something'
    }

    It 'accepts a result written in the same second as the launch' {
        $path = Join-Path $TestDrive 'same.json'
        Write-Result $path $true 'Notebook is off.' $launchedAt
        $o = Resolve-ActionOutcome -LastActionPath $path -LaunchedAt $launchedAt.AddMilliseconds(400) -ExitCode 0
        $o.Ran | Should Be $true
        $o.Ok | Should Be $true
    }
}

# ---- docker ps parsing -----------------------------------------------------

Describe 'ConvertFrom-DockerPsLines' {
    It 'parses name and status per line' {
        $lines = @('open-notebook-open_notebook-1|Up 17 minutes', 'open-notebook-surrealdb-1|Up 5 hours')
        $c = @(ConvertFrom-DockerPsLines -Lines $lines)
        $c.Count | Should Be 2
        $c[0].Name | Should Be 'open-notebook-open_notebook-1'
        $c[0].Status | Should Be 'Up 17 minutes'
    }

    It 'returns nothing for no output' {
        @(ConvertFrom-DockerPsLines -Lines @()).Count | Should Be 0
        @(ConvertFrom-DockerPsLines -Lines $null).Count | Should Be 0
    }
}
