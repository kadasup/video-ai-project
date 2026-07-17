param(
    [int]$Port = 5000,
    [string]$Script = "app.py",
    [string]$PyArgs = "",            # 額外 python 參數，搜尋站要 "-X utf8"
    [string]$HealthPath = "/",       # 健康檢查路徑（app 可用 /api/status，search 用 /）
    [switch]$Force
)

$ErrorActionPreference = "SilentlyContinue"
$proj    = "D:\VideoAI"
$pyExe   = "C:\Users\kevin\AppData\Local\Python\bin\python.exe"
$logDir  = Join-Path $proj "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# log/lock 都帶 port，兩台伺服器各自獨立、互不干擾（原本寫死 5000 會撞在一起）
$tag      = if ($Port -eq 5000) { "" } else { "-$Port" }
$logFile  = Join-Path $logDir ("server{0}-{1:yyyyMMdd}.log" -f $tag, (Get-Date))
$eventLog = Join-Path $logDir ("restart-events-{0:yyyyMMdd}.log" -f (Get-Date))
$lockFile = Join-Path $logDir ("restart-$Port.lock")

function Write-Event($msg) {
    Add-Content -Path $eventLog -Value ("{0} [:{1}] {2}" -f (Get-Date -Format "yyyy/M/d HH:mm:ss"), $Port, $msg)
}

function Test-Up {
    try { return (Invoke-WebRequest "http://localhost:$Port$HealthPath" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 }
    catch { return $false }
}

if ((-not $Force) -and (Test-Up)) {
    Write-Event "already up, skip"
    exit 0
}

# 防兩個 restart 同 port 併發（手動重啟撞上 watchdog）；lock 依 port 區分
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
    Write-Event "restart-server start ($Script)"

    # 殺掉還佔著這個 port 的殘留程序
    $heldPids = netstat -ano | Select-String ":$Port " | Select-String "LISTENING" |
        ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -Unique
    foreach ($p in $heldPids) { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2

    Start-Process -FilePath "cmd" `
        -ArgumentList "/c cd /d $proj\scripts && `"$pyExe`" $PyArgs $Script >> `"$logFile`" 2>&1" `
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
