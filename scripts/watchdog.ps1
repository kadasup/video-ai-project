$proj   = "D:\VideoAI"
$logDir = Join-Path $proj "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

# 兩台伺服器都顧：5000 產製、5001 語意搜尋（原本只看 5000，搜尋站掛了沒人重啟）
$servers = @(
    @{ Port = 5000; Health = "/api/status"; Script = "app.py";        PyArgs = "" }
    @{ Port = 5001; Health = "/";           Script = "search_app.py"; PyArgs = "-X utf8" }
)

# 連續失敗門檻：非單次失敗就重啟——避免產製尖峰 CPU 吃滿、健檢一時逾時就誤殺「忙碌但存活」的
# 伺服器（原本 5 秒逾時＋單次失敗即 -Force 重啟，會中斷正在跑的產製）。
$FAIL_THRESHOLD = 2
$fails = @{ 5000 = 0; 5001 = 0 }

while ($true) {
    $logFile = Join-Path $logDir ("heartbeat-{0:yyyyMMdd}.log" -f (Get-Date))
    $mem = (Get-Process python -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum).Sum / 1MB

    foreach ($s in $servers) {
        $port = $s.Port
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $status = "DOWN"
        try {
            # 逾時放寬到 8 秒（原 5 秒），給重載/尖峰一點餘裕
            $resp = Invoke-WebRequest "http://localhost:$port$($s.Health)" -UseBasicParsing -TimeoutSec 8
            $status = $resp.StatusCode
        } catch {}
        $sw.Stop()

        Add-Content -Path $logFile -Value ("{0} [:{1}] status={2} ms={3} mem={4}MB fails={5}" -f `
            (Get-Date -Format "yyyy/M/d HH:mm:ss"), $port, $status, $sw.ElapsedMilliseconds, [math]::Round($mem, 1), $fails[$port])

        if ($status -eq 200) {
            $fails[$port] = 0
        } else {
            $fails[$port]++
            if ($fails[$port] -ge $FAIL_THRESHOLD) {
                Add-Content -Path $logFile -Value ("{0} [:{1}] down x{2}, restarting" -f (Get-Date -Format "yyyy/M/d HH:mm:ss"), $port, $fails[$port])
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$proj\scripts\restart-server.ps1" `
                    -Port $port -Script $s.Script -PyArgs $s.PyArgs -HealthPath $s.Health -Force
                $fails[$port] = 0
            }
        }
    }

    Start-Sleep -Seconds 120
}
