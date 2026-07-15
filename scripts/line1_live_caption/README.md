# 線1 直播即時字幕 —— PoC 測試套件

對應 [`Video AI Project-工作紀錄.md`](../../Video%20AI%20Project-工作紀錄.md) 「🗓️ 2026-07-08 線1」那一段。範圍：**只做中文字幕，不做英文翻譯**。

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `caption_server.py` | 本地字幕轉播 server（不含任何 STT 邏輯，純轉播） |
| `caption.html` | OBS 瀏覽器來源要開的頁面，連 server 顯示最新字幕 |
| `manual_test_producer.py` | 手動輸入文字測試工具，**不需要任何金鑰** |
| `mic_stt_client.py` | 麥克風收音 → OpenAI `gpt-realtime-whisper` 轉錄 → 推字幕，**需要 OpenAI API Key** |
| `hotwords.txt` | 新聞熱詞表，編輯團隊自行維護 |

## 安裝

```bash
cd scripts/line1_live_caption
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Windows 上 `pyaudio` 若 `pip install` 失敗（缺編譯環境），改抓對應 wheel 安裝。

## 測試步驟（照順序做，卡在哪一步先解決那一步）

### 階段②：OBS 顯示測試（零依賴，先做這個）

1. 開一個終端機：`python caption_server.py`
2. 在 OBS 加一個「瀏覽器來源」，網址填 `file:///<這個資料夾的完整路徑>/caption.html`
3. 開另一個終端機：`python manual_test_producer.py`，輸入文字按 Enter
4. 確認 OBS 預覽視窗有即時顯示、樣式/位置正常

### 階段①：STT 準確度/延遲測試（需要金鑰）

1. 複製 `.env.example` 為 `.env`，填入 `OPENAI_API_KEY`（目前打的是 OpenAI 官方端點，不是 Azure——Azure 資源類型確認後這支要另外改）
2. `caption_server.py` 維持開著
3. 開新終端機：`python mic_stt_client.py`，對麥克風念稿測試，看終端機印出的辨識結果準不準、延遲多少

### 階段③：本地錄影測試

1. 承上，`caption_server.py`＋`mic_stt_client.py` 都跑著
2. OBS 開「開始錄影」，念一段新聞稿
3. 錄完回放，檢查字幕跟語音對不對得上、斷句自不自然

### 階段④：接直播音訊測試（正式接直播前最後一步）

1. 安裝 VB-Audio Virtual Cable，把直播音訊軌路由到虛擬音源
2. `mic_stt_client.py` 目前寫死用系統預設麥克風輸入，這步需要改成讀取虛擬音源裝置（`pyaudio` 用 `PyAudio().get_device_info_by_index()` 列出裝置後指定 `input_device_index`）——待階段①②③都測過、確定要往這步走再回來改

## 待確認事項

- Azure 資源類型確認後（Azure AI Speech 或 Azure OpenAI Realtime API），`mic_stt_client.py` 的連線/認證部分需要重寫，`caption_server.py`／`caption.html`／`manual_test_producer.py` 不受影響可以直接沿用
- 熱詞表 `hotwords.txt` 目前是空模板，需要編輯團隊提供實際名單
