$proj = "D:\VideoAI"

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$proj\scripts\restart-server.ps1"

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        if ((Invoke-WebRequest "http://localhost:5000/" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) {
            $ok = $true
            break
        }
    } catch {}
}
if ($ok) {
    Start-Process "http://localhost:5000/"
}

# start background watchdog: checks every 2 min, auto-restarts on crash
Start-Process -FilePath "powershell.exe" `
    -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$proj\scripts\watchdog.ps1`"" `
    -WindowStyle Hidden

# 語意搜尋伺服器（port 5001），沒起來才啟動
$searchUp = $false
try {
    $searchUp = (Invoke-WebRequest "http://localhost:5001/" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200
} catch {}
if (-not $searchUp) {
    Start-Process -FilePath "cmd" -ArgumentList "/c `"$proj\scripts\start_search.bat`"" -WindowStyle Hidden
}
