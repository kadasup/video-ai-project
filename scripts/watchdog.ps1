$proj   = "D:\VideoAI"
$logDir = Join-Path $proj "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

while ($true) {
    $logFile = Join-Path $logDir ("heartbeat-{0:yyyyMMdd}.log" -f (Get-Date))
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $status = "DOWN"
    try {
        $resp = Invoke-WebRequest "http://localhost:5000/api/status" -UseBasicParsing -TimeoutSec 5
        $status = $resp.StatusCode
    } catch {}
    $sw.Stop()

    $mem = (Get-Process python -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum).Sum / 1MB
    Add-Content -Path $logFile -Value ("{0} status={1} ms={2} mem={3}MB" -f (Get-Date -Format "yyyy/M/d HH:mm:ss"), $status, $sw.ElapsedMilliseconds, [math]::Round($mem, 1))

    if ($status -ne 200) {
        Add-Content -Path $logFile -Value ("{0} server down, calling restart-server.ps1 -Force" -f (Get-Date -Format "yyyy/M/d HH:mm:ss"))
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$proj\scripts\restart-server.ps1" -Force
    }


    Start-Sleep -Seconds 120
}
