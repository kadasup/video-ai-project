param([switch]$Force)

$ErrorActionPreference = "SilentlyContinue"
$proj    = "D:\VideoAI"
$pyExe   = "C:\Users\kevin\AppData\Local\Python\bin\python.exe"
$logDir  = Join-Path $proj "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$logFile  = Join-Path $logDir ("server-{0:yyyyMMdd}.log" -f (Get-Date))
$eventLog = Join-Path $logDir ("restart-events-{0:yyyyMMdd}.log" -f (Get-Date))
$lockFile = Join-Path $logDir "restart.lock"
$port     = 5000

function Write-Event($msg) {
    Add-Content -Path $eventLog -Value ("{0} {1}" -f (Get-Date -Format "yyyy/M/d HH:mm:ss"), $msg)
}

function Test-Up {
    try { return (Invoke-WebRequest "http://localhost:$port/" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { return $false }
}

if ((-not $Force) -and (Test-Up)) {
    Write-Event "already up, skip"
    exit 0
}

# prevent concurrent restart-server.ps1 runs (manual restart / watchdog collision)
if (Test-Path $lockFile) {
    $lockPid    = Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $lockAge    = (Get-Date) - (Get-Item $lockFile).LastWriteTime
    $ownerAlive = $lockPid -and (Get-Process -Id $lockPid -ErrorAction SilentlyContinue)
    if ($ownerAlive -and $lockAge.TotalMinutes -lt 3) {
        Write-Event "another restart-server.ps1 (PID $lockPid) running, skip"
        exit 0
    }
}
Set-Content -Path $lockFile -Value $PID

try {
    Write-Event "restart-server start"

    # kill any stale process still holding the port
    $heldPids = netstat -ano | Select-String ":$port " | Select-String "LISTENING" |
        ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -Unique
    foreach ($p in $heldPids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2

    Start-Process -FilePath "cmd" `
        -ArgumentList "/c cd /d $proj\scripts && `"$pyExe`" app.py >> `"$logFile`" 2>&1" `
        -WorkingDirectory "$proj\scripts" -WindowStyle Hidden

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Up) {
            Write-Event "restart-server OK"
            exit 0
        }
    }
    Write-Event "restart-server TIMEOUT"
    exit 1
}
finally {
    Remove-Item -Path $lockFile -Force -ErrorAction SilentlyContinue
}
