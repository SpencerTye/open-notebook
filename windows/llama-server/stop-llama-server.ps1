# Stops the llama-server router and every model child process it spawned,
# and waits until they have actually exited (the GPU memory is released only
# then, and the start script must not mistake a dying process for a live one).
$procs = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) { Write-Output "llama-server is not running."; exit 0 }
$procs | Stop-Process -Force -Confirm:$false -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Process llama-server -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
$left = @(Get-Process llama-server -ErrorAction SilentlyContinue)
if ($left.Count -gt 0) {
    Write-Warning "$($left.Count) llama-server process(es) still exiting after 30 s."
    exit 1
}
Write-Output "Stopped $($procs.Count) llama-server process(es)."
