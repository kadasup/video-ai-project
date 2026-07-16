# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案名稱
Video AI Project

## 對話開始時請先讀
進度與最近更動都在 Obsidian：`創作庫/Video AI Project/Video AI Project-工作紀錄.md`

## 工作模式
- **結束工作**：說「收工」→ 自動 commit + push + 更新 Obsidian 工作筆記
- **接續工作**：說「開工」→ 讀工作筆記、報告 git 狀態、建議下一步

## 三個家
- 📋 本機：`~/Desktop/Claude/Video AI Project/`
- 🐙 GitHub：https://github.com/kadasup/video-ai-project
- 📘 Obsidian：`創作庫/Video AI Project/Video AI Project-工作紀錄.md`

## 資料夾分工（2026-07-16 定案）
- **這裡（`Video AI Project/`）＝唯一程式碼工作目錄**：所有 `scripts/*.py` 修改都在這裡做、git 追蹤、push 上 GitHub。
- **`D:\VideoAI\`＝純資料/暫存/影音處理**：來源影片（`input/`）、成片（`output/`）、產製紀錄（`logs/`）、語意搜尋索引（`search/`）、正音對照表（`pronounce_map.json`）、暫存（`tmp/`）。這個資料夾**不進 git**（5GB+ 媒體檔）。
- **`D:\VideoAI\scripts` 是指向這裡 `scripts/` 的目錄 junction**——實體檔案只有一份（在這裡），伺服器從 `D:\VideoAI\scripts` 執行時其實跑的就是這份 git 版本，兩邊改一次全同步，不會分岔。
- 程式碼裡的路徑是寫死 `BASE = Path(r"D:\VideoAI")`（見 `produce.py`/`select_clip.py`/`indexer.py`），所以程式碼實體放哪裡都不影響資料讀寫。
