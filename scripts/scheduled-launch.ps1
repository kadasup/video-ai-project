# 由 Windows 工作排程器啟動：讓產製站(5000)/搜尋站(5001)/watchdog 獨立於 Claude App 執行。
# 關鍵設計：這支是排程任務的「主程序」——最後在前景常駐 watchdog、永不結束，
# 讓任務的行程 job 一直開著，底下啟動的兩台伺服器(子孫程序)就不會被連帶收掉。
# 因為整條程序樹掛在「工作排程器服務」底下、不是 Claude 底下，
# 所以 Claude App 更新/關閉/這個對話結束都動不到它。
$ErrorActionPreference = "SilentlyContinue"
$proj = "D:\VideoAI"
$ps   = "$proj\scripts"

# 先確保兩台伺服器起來(-Force：清掉殘留、換成這個排程階段擁有的乾淨程序)
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps\restart-server.ps1" -Port 5000 -Script "app.py" -HealthPath "/api/status" -Force
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps\restart-server.ps1" -Port 5001 -Script "search_app.py" -PyArgs "-X utf8" -HealthPath "/" -Force

# 前景常駐 watchdog(每 2 分鐘健檢、掛了自動重拉)：這支活著＝任務 job 開著＝伺服器不被收
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$ps\watchdog.ps1"
