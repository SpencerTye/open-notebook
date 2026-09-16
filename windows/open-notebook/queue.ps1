# queue.ps1 - live view of the Open Notebook job queue. Read-only.
#
# The app has no screen for its job queue (its /api/commands/jobs endpoint is a
# stub). The queue lives in the database's `command` table; this script reads
# it and prints one line every few seconds:
#
#   07:41:03  embed_insight: 1 running, 183 waiting  ||  insights with a vector: 21 of 204
#
# "queue empty" on that line means every job has finished (or failed; failed
# jobs show in the container log). Ctrl+C stops the script.
#
# Run:  powershell -ExecutionPolicy Bypass -File "S:\RAG Notebooks\open-notebook\custom\queue.ps1"
#       add -Once for a single line, -Every 2 to poll every 2 seconds.

param([int]$Every = 5, [switch]$Once)

$ErrorActionPreference = "Stop"
$docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe"
$creds = & $docker exec open-notebook-open_notebook-1 sh -c 'echo $SURREAL_USER:$SURREAL_PASSWORD' 2>$null
if (-not $creds) { Write-Output "The notebook container is not running."; exit 1 }

$headers = @{
    Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($creds))
    'surreal-ns'  = 'open_notebook'
    'surreal-db'  = 'open_notebook'
    Accept        = 'application/json'
}

function Sql($query) {
    (Invoke-RestMethod -Uri 'http://127.0.0.1:8000/sql' -Method Post -Headers $headers -ContentType 'text/plain' -Body $query).result
}

function Line {
    $jobs = @(Sql "SELECT name, status FROM command WHERE status IN ['new', 'running'];")
    $done = @(Sql "SELECT count() AS n FROM source_insight WHERE embedding != none AND array::len(embedding) > 0 GROUP ALL;")
    $all = @(Sql "SELECT count() AS n FROM source_insight GROUP ALL;")

    $parts = @()
    foreach ($group in ($jobs | Group-Object name | Sort-Object Name)) {
        $running = @($group.Group | Where-Object status -eq 'running').Count
        $waiting = @($group.Group | Where-Object status -eq 'new').Count
        $parts += ("{0}: {1} running, {2} waiting" -f $group.Name, $running, $waiting)
    }
    if ($parts.Count -eq 0) { $queue = "queue empty" } else { $queue = $parts -join " | " }

    $embedded = 0; if ($done.Count -gt 0) { $embedded = $done[0].n }
    $total = 0; if ($all.Count -gt 0) { $total = $all[0].n }
    "{0:HH:mm:ss}  {1}  ||  insights with a vector: {2} of {3}" -f (Get-Date), $queue, $embedded, $total
}

if ($Once) { Line; exit 0 }
while ($true) { Line; Start-Sleep -Seconds $Every }
