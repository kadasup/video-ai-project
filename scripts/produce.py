"""
社會新聞短影音產製腳本（監視器 / 社會案件用）
輸出格式：1080×1920 直式，68 秒（片頭 3s + 主畫面 60s + 片尾 5s）

用法：
  # 無影片先測流程（黑底）
  python produce.py --article D:/VideoAI/input/news.txt

  # 有監視器影片（可指定多支，依序銜接補滿主畫面秒數）
  python produce.py --article D:/VideoAI/input/news.txt --video D:/VideoAI/input/cctv1.mp4 D:/VideoAI/input/cctv2.mp4 --start 145 0

安裝依賴：
  pip install openai edge-tts
  winget install Gyan.FFmpeg  （ffmpeg 要在 PATH）

環境變數：
  OPENAI_API_KEY=sk-...
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from openai import AzureOpenAI
import edge_tts

# ── 路徑設定 ─────────────────────────────────────────────────────────────────
BASE      = Path(r"D:\VideoAI")
TEMPLATES = BASE / "templates"
OUTPUT    = BASE / "output"
TMP       = BASE / "tmp"

INTRO_PNG = TEMPLATES / "intro.png"    # 片頭圖卡（設計師提供）
OUTRO_PNG = TEMPLATES / "outro.png"    # 片尾圖卡（設計師提供，靜態圖，次要備援）
OUTRO_MP4 = TEMPLATES / "shorts片尾.mp4"  # 設計師提供的 shorts 專用動態片尾（已是 1080x1920，優先使用）
LOGO_PNG  = TEMPLATES / "logo.png"     # 角標（標準命名，找不到時退回下方實際素材）
BGM_MP3   = TEMPLATES / "bgm.mp3"     # 背景音樂
FONT_TTF  = TEMPLATES / "font.ttf"    # 字幕字型
# 沒有設計師素材時，自動用黑底 / 系統字型暫代，流程照跑

# 設計師實際提供的角標素材（檔名跟標準命名對不上，依序找）
LOGO_CANDIDATES = [
    LOGO_PNG,
    TEMPLATES / "自由時報1080Logo.png",
    TEMPLATES / "自由時報720Logo.png",
    TEMPLATES / "自由電子報LOGO.png",
]

# ── 影片規格 ──────────────────────────────────────────────────────────────────
W, H       = 1080, 1920
INTRO_SEC  = 3
OUTRO_SEC  = 5
HOOK_SEC   = 2    # hook 文字疊加在主畫面最前幾秒，純畫面文字，不唸出來
MAIN_SEC   = 60   # 主畫面目標秒數；來源影片不足時後段補黑幕並提醒
TTS_VOICE  = "zh-TW-HsiaoChenNeural"
TTS_RATE   = "+8%"    # 新聞播報語速略快於預設，貼近主播腔
TTS_PITCH  = "+0Hz"

# 可選台灣腔聲音（edge-tts 目前只有這三個 zh-TW）
TTS_VOICES = {
    "hsiaochen": "zh-TW-HsiaoChenNeural",  # 女聲（預設）
    "hsiaoyu":   "zh-TW-HsiaoYuNeural",    # 女聲
    "yunjhe":    "zh-TW-YunJheNeural",     # 男聲，較穩重
}

# ── Windows fallback 字型（沒有 font.ttf 時用）────────────────────────────────
SYSTEM_FONT = r"C:/Windows/Fonts/msjh.ttc"  # 微軟正黑體

# ── 發音替換表 ────────────────────────────────────────────────────────────────
# edge-tts 不支援音標標記，專有名詞唸錯時用「換個寫法」讓它唸對：
# 送 TTS 的文字會先替換，但字幕上仍顯示原詞。
# 編輯 D:\VideoAI\pronounce_map.json 即可，格式：{"Oka": "歐卡", "AI": "A I"}
# （底線開頭的 key 當註解用，會被略過；改完存檔即生效，不用重啟）
PRONOUNCE_MAP_FILE = BASE / "pronounce_map.json"

def _load_pronounce_map() -> dict:
    try:
        m = json.loads(PRONOUNCE_MAP_FILE.read_text(encoding="utf-8-sig"))
        return {k: v for k, v in m.items() if not k.startswith("_")}
    except Exception:
        return {}

def apply_pronounce(text: str) -> str:
    """把容易唸錯的詞換成會唸對的寫法（只影響 TTS 發音，不影響字幕顯示）"""
    for k, v in _load_pronounce_map().items():
        text = text.replace(k, v)
    return text

def _client() -> AzureOpenAI:
    """延遲建立 AzureOpenAI client，讀 D:\\VideoAI\\.env 的設定"""
    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 字幕：不讓 GPT 寫字幕文字（實測 GPT 會把字幕寫成「摘要版」而不是逐字節錄，
# 導致跟旁白對不上、逐字對齊演算法失效），改成直接從旁白原文切段，保證一字不差。
# ─────────────────────────────────────────────────────────────────────────────
def _chunk_narration(text: str, lo: int = 8, hi: int = 12) -> list[str]:
    """把旁白原文切成 8~12 字一句，優先在標點處斷句；純切字串，不改寫內容"""
    punct = set("，。！？、；：,.!?;:")
    chunks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        remaining = n - i
        if remaining <= hi:
            chunks.append(text[i:])
            break
        cut = -1
        for j in range(min(hi, remaining) - 1, lo - 2, -1):
            if text[i + j] in punct:
                cut = j + 1
                break
        if cut == -1:
            cut = hi
        chunks.append(text[i:i + cut])
        i += cut
    # 尾段太短（<4字）併回前一句，避免出現孤零零一兩個字的字幕
    if len(chunks) >= 2 and len(chunks[-1]) < 4:
        chunks[-2] += chunks[-1]
        chunks.pop()
    return chunks


def _build_subtitles(narration: str) -> list[dict]:
    """時間戳先給佔位值，實際起訖交給 align_subtitles_to_boundaries 用逐字時間算"""
    return [
        {"index": i, "start": "00:00:00,000", "end": "00:00:00,000", "text": c}
        for i, c in enumerate(_chunk_narration(narration), 1)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Step 1：生成腳本
# ─────────────────────────────────────────────────────────────────────────────
def generate_script(article: str, footage_notes: str | None = None) -> dict:
    """
    新聞稿 → JSON 腳本（標題、地點、旁白、字幕、語意分段）。
    footage_notes: 素材畫面清單（catalog_video 的結果彙整）。有給的話，
    旁白會「貼著畫面寫」（write to picture）——只講畫面裡看得到的事優先，
    這是旁白跟畫面搭得起來的關鍵。
    """
    footage_block = ""
    if footage_notes:
        footage_block = f"""
可用畫面素材清單（這批就是成片會用的畫面）：
{footage_notes}

⚠️ 貼著畫面寫稿（write to picture）——最重要的規則：
- 旁白優先講「畫面裡真的看得到的事」：素材有消防救援，就把救援過程講細一點；
  素材沒拍到的情節（案發瞬間、嫌犯特徵），一句帶過就好，不要展開
- 段落順序盡量跟著素材能提供的畫面走，讓每段旁白都有對應畫面可配
- 新聞稿裡畫面拍不到的背景資訊（警方說法、後續處理）集中放最後一段
- segments 的 desc 直接註明建議配哪支影片的哪個時段（如「配V1的0~5秒消防救援」）
"""

    prompt = f"""你是台灣電視新聞影音編輯。根據以下新聞稿，產出一支 {MAIN_SEC} 秒短影音的播報腳本。
{footage_block}

輸出 JSON（嚴格依此格式，不要加任何說明）：
{{
  "title": "片頭標題（20 字內，吸睛但不煽情，放在片頭卡）",
  "hook": "開頭鉤子（10~14 字內，比 title 更聳動抓眼球，疊在主畫面最前 2 秒的畫面上，不會被旁白唸出來，純文字看的）",
  "hashtags": ["#關鍵字1", "#關鍵字2", "#關鍵字3", "#關鍵字4"],
  "location": "地點（縣市＋路段，10 字內）",
  "narration": "旁白全文（繁體中文，約 {int(MAIN_SEC * 4.5)} 字）",
  "segments": [
    {{"start": 0, "end": 18, "desc": "這段旁白在講什麼（例：案發經過，嫌犯持刀進入超商）"}},
    {{"start": 18, "end": 40, "desc": "嫌犯特徵與逃逸方向"}},
    {{"start": 40, "end": {MAIN_SEC}, "desc": "警方調閱監視器追查"}}
  ]
}}

hook／hashtag 規則：
- hook 是給演算法用的「前 2 秒留人」鉤子，跟 title（片頭卡的正式標題）分開寫、語氣可以比 title 更直白聳動，
  但仍要符合事實、不能誇大到失真（例：新聞是「監視器拍到行搶」，hook 可以寫「這幕全被拍下來了」，
  不要寫「全台瘋傳」這種無中生有的浮誇語）
- hashtags 4~6 個，繁體中文、含 # 號，跟地點/案件類型/關鍵字相關（例：#社會新聞 #台南 #監視器 #超商搶案），
  不要塞空泛跟內容無關的熱門標籤

旁白播報文體規則（台灣電視新聞播報腔）：
- ⚠️ 字數硬規定：{int(MAIN_SEC * 4.2)}~{int(MAIN_SEC * 4.5)} 字（含標點），寫完務必數一次。
  低於 {int(MAIN_SEC * 4.2)} 字結尾會有長段無聲空景、高於 {int(MAIN_SEC * 4.5)} 字會被截斷，
  兩種都算不合格；寫完若不在區間內，自行增刪到符合再輸出
- 第一句先破題：時間＋地點＋發生什麼事，一句話講完
- 結構：破題 → 經過細節 → 傷亡損失 → 警方處置 → 收尾（後續發展）
- ⚠️ 句號要省著用：實測 TTS 在句號／驚嘆號／問號後會停頓約 0.85 秒，逗號只停頓約 0.3 秒，
  句號斷太密旁白會一直卡頓、很不自然。每個句號之間要有 25~40 字，
  中間用逗號串 2~3 個 8~16 字的短分句帶節奏，意思完整講完才用句號收尾，
  主詞能省就省（範例：「一名男子走進店裡，突然亮出水果刀，逼店員打開收銀機。」是一句，不是三句）
- 數字用口語唸法：「三萬元」不寫「30,000元」、「七十歲」不寫「70歲」
- 平鋪直敘、不帶評論、不煽情
- 匿名原則：嫌疑人用「王姓男子」「張姓嫌犯」，不寫全名

語感範例（模仿這種台灣電視新聞口白的節奏，不要照抄內容；注意句號很省，都是用逗號把短分句串成一句）：
「台南安平一間超商，昨天下午驚傳搶案，一名王姓男子戴著口罩走進店裡，突然亮出水果刀，
逼店員打開收銀機。得手三萬元後往港濱公園方向逃逸，店員嚇得直發抖，趕緊按下警鈴。
警方調閱監視器，鎖定嫌犯特徵，正全力追緝。」

❌ 禁用的 AI 腔（出現任何一個就重寫）：
「值得注意的是」「引發外界關注」「造成社會震驚」「相關單位」「進行了…」「展開了…」
「…作業」（如「救援作業」直接寫「救援」）「此外」「同時」「隨後」開頭、
「目前」一篇超過一次、連續兩句以上用相同句式開頭

（字幕不用你寫，程式會直接從旁白原文切段，不用在這裡管字幕格式）

segments 規則（給後續自動選片配畫面用）：
- 把旁白依內容切成 3~5 個語意段落，start/end 為秒數，必須從 0 連續涵蓋到 {MAIN_SEC}
- desc 描述該段旁白的畫面需求，具體寫出人事物與動作（如「嫌犯亮刀威脅店員」而非「案發」）

新聞稿：
{article}"""

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=4096,
    )
    usage = {}
    if resp.usage:
        usage = {
            "prompt_tokens":     resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens":      resp.usage.total_tokens,
        }
    data = json.loads(resp.choices[0].message.content)
    data["subtitles"] = _build_subtitles(data["narration"])
    return data, usage


def shorten_script(script: dict, max_chars: int) -> tuple[dict, dict]:
    """旁白超過字數上限時的保險：請 GPT 縮寫旁白並同步重排 segments"""
    prompt = f"""以下短影音腳本的旁白太長（{len(script['narration'])} 字），會超過播出秒數。
請縮短旁白到 {max_chars} 字以內（保留關鍵資訊，刪次要細節），並同步重寫 segments。

規則不變：
- segments 3~5 段連續涵蓋 0~{MAIN_SEC} 秒
- 句號要省著用（每句號間隔 25~40 字，中間用逗號串短分句），句號斷太密 TTS 停頓會很卡

原腳本：
{json.dumps(script, ensure_ascii=False)}

輸出相同格式的完整 JSON（title、hook、hashtags、location、narration、segments，不用輸出 subtitles），
hook 跟 hashtags 原樣保留不用重寫。"""

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=4096,
    )
    usage = {}
    if resp.usage:
        usage = {
            "prompt_tokens":     resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens":      resp.usage.total_tokens,
        }
    data = json.loads(resp.choices[0].message.content)
    data["subtitles"] = _build_subtitles(data["narration"])
    return data, usage


# ─────────────────────────────────────────────────────────────────────────────
# Step 2：TTS
# ─────────────────────────────────────────────────────────────────────────────
# 曾試過逐句合成＋句間插靜音，做出來旁白會因為每句各自獨立合成而語氣不連貫，
# 且插入的靜音沒有反映在字幕時間戳上，導致字幕跟語音對不上。改回整段一次合成，
# 讓 edge-tts 自己處理語氣連貫與自然停頓；旁白/字幕的長度對齊改在 rescale_* 處理。
async def _tts_async(text: str, path: Path, voice: str, rate: str, pitch: str) -> list[dict]:
    """
    用 stream 模式合成：一邊寫音訊、一邊收集 WordBoundary（每個字/詞的精確發音時間），
    回傳 boundaries 供字幕逐字對齊。offset/duration 單位是 100ns ticks。
    edge-tts 免費服務偶爾會暫時性失敗（如 'No audio was received'），重試 3 次再放棄。
    """
    tts_text = apply_pronounce(text)   # 專有名詞發音修正（字幕不受影響）
    last_err = None
    for attempt in range(3):
        boundaries: list[dict] = []
        try:
            comm = edge_tts.Communicate(tts_text, voice, rate=rate, pitch=pitch,
                                         boundary="WordBoundary")
            with open(path, "wb") as f:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        f.write(chunk["data"])
                    elif chunk["type"] == "WordBoundary":
                        boundaries.append({
                            "start": chunk["offset"] / 1e7,
                            "end":   (chunk["offset"] + chunk["duration"]) / 1e7,
                            "text":  chunk["text"],
                        })
            if path.exists() and path.stat().st_size > 0:
                return boundaries
            last_err = RuntimeError("TTS 產出空檔案")
        except Exception as e:
            last_err = e
        await asyncio.sleep(1.5 * (attempt + 1))
    raise last_err


def generate_tts(text: str, path: Path, voice: str = TTS_VOICE,
                  rate: str = TTS_RATE, pitch: str = TTS_PITCH) -> list[dict]:
    """合成旁白音訊，回傳逐字時間資料（給字幕對齊用；拿不到時回空 list）"""
    return asyncio.run(_tts_async(text, path, voice, rate, pitch))


# ─────────────────────────────────────────────────────────────────────────────
# 字幕時間軸縮放（旁白實際長度 ≠ 腳本假設的 MAIN_SEC 時用）
# ─────────────────────────────────────────────────────────────────────────────
def _ts_to_sec(t: str) -> float:
    """'00:00:03,500' → 3.5"""
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _sec_to_ts(sec: float) -> str:
    """3.5 → '00:00:03,500'"""
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _flatten_boundaries_to_chars(boundaries: list) -> list[tuple[str, float, float]]:
    """把每個 WordBoundary（可能含多字）拆成逐字時間戳，一個 word 內用等分近似"""
    chars: list[tuple[str, float, float]] = []
    for b in boundaries:
        text = re.sub(r"[^0-9A-Za-z一-鿿]", "", b["text"])
        if not text:
            continue
        n = len(text)
        step = (b["end"] - b["start"]) / n
        for k, ch in enumerate(text):
            chars.append((ch, b["start"] + k * step, b["start"] + (k + 1) * step))
    return chars


def align_subtitles_to_boundaries(subtitles: list, boundaries: list) -> list:
    """
    用 TTS 回傳的逐字發音時間（WordBoundary）精確對齊字幕。
    原理：字幕文字與旁白一字不差，把 boundary 攤平成逐字時間戳後，
    依序切出每句字幕該佔的字元段，就能拿到該句真實的起訖時間。
    比「GPT 猜時間戳再等比縮放」準確得多（那個只有總長對、中間每句會漂）。

    切字用「累計消耗量」而非「每句各自 round」：每句需要的字數若剛好卡在
    boundary word 中間，各自 round 一定會偏向多吃（無條件進位），21 句下來
    累積誤差會讓最後一兩句連 boundary 都吃不到；改成對累計消耗量取整數，
    捨入誤差會前後抵銷，不會愈積愈多。
    """
    def _clean(s: str) -> str:
        return re.sub(r"[^0-9A-Za-z一-鿿]", "", s)

    chars = _flatten_boundaries_to_chars(boundaries)
    total_bnd = len(chars)
    total_sub = sum(len(_clean(apply_pronounce(s["text"]))) for s in subtitles)
    if total_sub == 0 or total_bnd == 0:
        return []
    ratio = total_bnd / total_sub

    aligned: list = []
    ci = 0.0        # 累計「應該」消耗到第幾個字元（浮點，不提前 round）
    consumed = 0
    for sub in subtitles:
        need_len = len(_clean(apply_pronounce(sub["text"])))
        if need_len == 0:
            continue
        ci += need_len * ratio
        target = min(total_bnd, round(ci))
        take = max(1, target - consumed)
        seg = chars[consumed: consumed + take]
        if not seg:
            break  # 字元用完了，剩下的句子交給呼叫端 fallback
        aligned.append({**sub, "_start_sec": seg[0][1], "_end_sec": seg[-1][2]})
        consumed += len(seg)

    if len(aligned) < len([s for s in subtitles if _clean(s["text"])]):
        return []  # 對齊不完整（字數對不上），回空讓呼叫端 fallback 回縮放法

    # 每句尾端留 0.2s 緩衝，但不能蓋到下一句的開頭
    out = []
    for i, sub in enumerate(aligned):
        end = sub["_end_sec"] + 0.2
        if i + 1 < len(aligned):
            end = min(end, aligned[i + 1]["_start_sec"] - 0.01)
        out.append({k: v for k, v in sub.items() if not k.startswith("_")} |
                   {"start": _sec_to_ts(sub["_start_sec"]),
                    "end":   _sec_to_ts(max(end, sub["_end_sec"]))})
    return out


def rescale_subtitles(subtitles: list, from_total: float, to_total: float) -> list:
    """把 0~from_total 的字幕時間軸等比縮放到 0~to_total"""
    if from_total <= 0 or abs(from_total - to_total) < 0.3:
        return subtitles
    k = to_total / from_total
    out = []
    for s in subtitles:
        out.append({**s,
                    "start": _sec_to_ts(_ts_to_sec(s["start"]) * k),
                    "end":   _sec_to_ts(min(_ts_to_sec(s["end"]) * k, to_total))})
    return out


def rescale_segments(segments: list, from_total: float, to_total: float) -> list:
    """把語意分段（數字秒）等比縮放"""
    if from_total <= 0 or abs(from_total - to_total) < 0.3:
        return segments
    k = to_total / from_total
    return [{**seg, "start": round(seg["start"] * k, 1),
             "end": round(min(seg["end"] * k, to_total), 1)} for seg in segments]


# ─────────────────────────────────────────────────────────────────────────────
# Step 3：字幕檔（寫 .ass，不是 .srt）
# ─────────────────────────────────────────────────────────────────────────────
# 用 .ass 而非 .srt 是刻意的：ffmpeg 的 subtitles 濾鏡把純 .srt 自動轉檔時，
# 內部假設的座標系統（PlayResX/Y）跟實際輸出畫面尺寸對不上，
# 會導致 force_style 的 MarginV 被錯誤放大，字幕整個被推出畫面外（實測驗證過）。
# .ass 檔頭明確宣告 PlayResX/PlayResY = 輸出尺寸，就不會有這個問題。
def _srt_time_to_ass(t: str) -> str:
    """'00:00:05,000' → '0:00:05.00'（ASS 時間戳只到百分秒，且不補零到兩位小時）"""
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    centis = int(ms) // 10
    return f"{int(h)}:{m}:{s}.{centis:02d}"


def write_ass(subtitles: list, path: Path) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft JhengHei,84,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,1,2,20,20,280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""

    MAX_CHARS = 12  # 84 級字在 1080 寬的安全上限，超過自動斷行

    def _wrap(text: str) -> str:
        text = text.replace("\n", "")
        if len(text) <= MAX_CHARS:
            return text
        mid = (len(text) + 1) // 2  # 對半斷，兩行都不超過上限（字幕最長 ~24 字）
        return text[:mid] + "\\N" + text[mid:]

    lines = [header]
    for s in subtitles:
        start = _srt_time_to_ass(s["start"])
        end = _srt_time_to_ass(s["end"])
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{_wrap(s['text'])}")

    path.write_text("\n".join(lines), encoding="utf-8-sig")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg 工具
# ─────────────────────────────────────────────────────────────────────────────
def _ffmpeg_exe() -> str:
    """找 ffmpeg，優先 PATH，退回 winget 安裝路徑"""
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget_path = (
        r"C:\Users\kevin\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe"
    )
    if Path(winget_path).exists():
        return winget_path
    raise FileNotFoundError("找不到 ffmpeg，請確認已安裝並在 PATH 中")


def _ffprobe_exe() -> str:
    """找 ffprobe，優先 PATH，退回跟 ffmpeg 同一個資料夾"""
    import shutil
    found = shutil.which("ffprobe")
    if found:
        return found
    return _ffmpeg_exe().replace("ffmpeg.exe", "ffprobe.exe")


def _probe_duration(path: Path) -> float:
    """讀取一支影片的總長度（秒）"""
    r = subprocess.run(
        [_ffprobe_exe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, encoding="utf-8"
    )
    try:
        return float(r.stdout.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"無法讀取影片長度，請確認是有效的影片檔案：{path}")


def resolve_logo_path() -> Path | None:
    """找角標素材：優先標準命名 logo.png，找不到就退回設計師實際提供的檔名"""
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def ff(*args, show=False):
    """執行 ffmpeg，出錯直接停"""
    cmd = [_ffmpeg_exe(), "-y"] + [str(a) for a in args]
    if not show:
        cmd += ["-loglevel", "error"]
    subprocess.run(cmd, check=True)


def esc(p: Path) -> str:
    """ffmpeg 濾鏡用路徑：反斜線→正斜線，冒號加跳脫"""
    return str(p).replace("\\", "/").replace(":", "\\:")


def font_path() -> str:
    """取得可用字型路徑（優先 templates/font.ttf，退回系統字型）"""
    if FONT_TTF.exists():
        return esc(FONT_TTF)
    system_font_path = Path(SYSTEM_FONT.replace("/", "\\"))
    if system_font_path.exists():
        return esc(system_font_path)
    return ""

def sub_style() -> str:
    """
    字幕的字級/顏色/位置已經寫進 .ass 檔頭（write_ass 的 Style 行），這裡只在
    有設計師自訂字型檔（templates/font.ttf）時才需要覆寫 FontFile；
    系統字型走 .ass 內建的 Fontname=Microsoft JhengHei 即可，不必覆寫。
    """
    if FONT_TTF.exists():
        return f"FontFile={esc(FONT_TTF)}"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Step 4a：片頭
# ─────────────────────────────────────────────────────────────────────────────
def make_intro(script: dict) -> Path:
    out = TMP / "intro.mp4"
    title    = script["title"].replace("'", "\\'").replace(":", "\\:")
    location = script["location"].replace("'", "\\'")
    fp = font_path()
    font_arg = f":fontfile='{fp}'" if fp else ""

    text_filter = (
        f"drawtext=text='{title}'{font_arg}:fontsize=68:fontcolor=white"
        f":x=(w-text_w)/2:y=(h-text_h)/2-60:shadowcolor=black:shadowx=2:shadowy=2,"
        f"drawtext=text='{location}'{font_arg}:fontsize=42:fontcolor=white@0.85"
        f":x=(w-text_w)/2:y=(h-text_h)/2+40"
    )

    if INTRO_PNG.exists():
        ff(
            "-loop", "1", "-i", INTRO_PNG,
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]scale={W}:{H},{text_filter}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", INTRO_SEC, "-r", "30", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    else:
        # 黑底暫代
        ff(
            "-f", "lavfi", "-i", f"color=c=black:size={W}x{H}:rate=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]{text_filter}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", INTRO_SEC, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    return out


def _hook_filter(hook: str | None) -> str:
    """hook 純文字疊加在主畫面最前 HOOK_SEC 秒，不唸出來；沒有 hook 就回空字串"""
    if not hook:
        return ""
    text = hook.replace("'", "\\'").replace(":", "\\:")
    fp = font_path()
    font_arg = f":fontfile='{fp}'" if fp else ""
    return (
        f"drawtext=text='{text}'{font_arg}:fontsize=56:fontcolor=white"
        f":x=(w-text_w)/2:y=280:shadowcolor=black:shadowx=2:shadowy=2"
        f":box=1:boxcolor=black@0.45:boxborderw=20"
        f":enable='between(t,0,{HOOK_SEC})'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4b：主畫面
# ─────────────────────────────────────────────────────────────────────────────
def _sub_filter(srt: Path) -> str:
    """字幕濾鏡字串。位置/字級/顏色寫在 .ass 檔頭（write_ass），
    這裡只在有自訂字型檔時才 force_style 覆寫 FontFile。"""
    srt_esc = esc(srt)
    style = sub_style()
    return f"subtitles='{srt_esc}':force_style='{style}'" if style else f"subtitles='{srt_esc}'"


def _mix_audio(tts: Path, audio_out: Path, main_sec: float):
    """TTS + BGM 混音（無 BGM 時只轉檔），長度切齊 main_sec"""
    if BGM_MP3.exists():
        ff(
            "-i", tts,
            "-stream_loop", "-1", "-i", BGM_MP3,
            "-filter_complex",
            "[0:a]volume=1.0[tts];[1:a]volume=0.08[bgm];[tts][bgm]amix=inputs=2:duration=first[a]",
            "-map", "[a]", "-t", main_sec,
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            audio_out
        )
    else:
        ff(
            "-i", tts, "-t", main_sec,
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            audio_out
        )


def make_main(tts: Path, srt: Path, sources: list[dict] | None = None,
              main_sec: float = MAIN_SEC, hook: str | None = None) -> tuple[Path, dict]:
    """
    sources: [{"path": "...", "start": 12.0}, ...] 依序使用，不足 main_sec 秒的部分自動補黑幕。
             傳 None 或空 list = 全黑幕（測試流程）。
    main_sec: 主畫面實際長度（依旁白 TTS 實際長度動態決定，預設 MAIN_SEC）。
    hook: 疊在最前 HOOK_SEC 秒畫面上的純文字鉤子，不影響旁白/字幕。

    回傳 (輸出路徑, 長度資訊 dict)：
      length_info = {
        "target_sec":    目標秒數,
        "used_sources":  實際用上的來源數,
        "covered_sec":   來源真正覆蓋到的秒數,
        "shortfall_sec": 不足、補黑幕的秒數,
        "insufficient":  bool，是否有補黑幕,
      }
    """
    out       = TMP / "main.mp4"
    audio_out = TMP / "audio.aac"
    sub_f = _sub_filter(srt)
    _mix_audio(tts, audio_out, main_sec)

    # ── 依序分配每支來源可用秒數，湊不滿就補黑幕（結尾）─────────────────────────
    sources = sources or []
    plan: list[dict] = []
    remaining = float(main_sec)

    for src in sources:
        if remaining <= 0.05:
            break
        path = Path(src["path"])
        start = float(src.get("start", 0) or 0)
        try:
            dur = _probe_duration(path)
        except ValueError:
            continue  # 探測失敗就跳過這支，不中斷整體流程
        avail = max(0.0, dur - start)
        if avail <= 0.05:
            continue
        alloc = min(avail, remaining)
        plan.append({"path": path, "start": start, "dur": alloc})
        remaining -= alloc

    if remaining > 0.05:
        plan.append({"path": None, "start": 0, "dur": remaining})

    length_info = _plan_length_info(plan, main_sec)
    _assemble_main(plan, audio_out, sub_f, out, main_sec, hook=hook)
    return out, length_info


def make_main_plan(tts: Path, srt: Path, plan: list[dict],
                   main_sec: float = MAIN_SEC, hook: str | None = None) -> tuple[Path, dict]:
    """
    依智慧配對產生的時間軸組裝主畫面。
    plan: [{"path": str|None, "start": float, "dur": float}, ...]
          path=None 代表該段配不到畫面、用黑幕；黑幕可以出現在任何位置。
    hook: 疊在最前 HOOK_SEC 秒畫面上的純文字鉤子，不影響旁白/字幕。
    """
    out       = TMP / "main.mp4"
    audio_out = TMP / "audio.aac"
    sub_f = _sub_filter(srt)
    _mix_audio(tts, audio_out, main_sec)

    norm_plan = [
        {"path": (Path(e["path"]) if e.get("path") else None),
         "start": float(e.get("start", 0) or 0),
         "dur": float(e["dur"])}
        for e in plan if float(e.get("dur", 0)) > 0.05
    ]
    length_info = _plan_length_info(norm_plan, main_sec)
    _assemble_main(norm_plan, audio_out, sub_f, out, main_sec, hook=hook)
    return out, length_info


def _plan_length_info(plan: list[dict], target_sec: float = MAIN_SEC) -> dict:
    covered = sum(e["dur"] for e in plan if e.get("path"))
    black   = sum(e["dur"] for e in plan if not e.get("path"))
    return {
        "target_sec":    round(target_sec, 1),
        "used_sources":  len({str(e["path"]) for e in plan if e.get("path")}),
        "covered_sec":   round(covered, 1),
        "shortfall_sec": round(black, 1),
        "insufficient":  black > 0.05,
    }


def _assemble_main(plan: list[dict], audio_out: Path, sub_f: str, out: Path,
                   main_sec: float = MAIN_SEC, hook: str | None = None):
    """把 plan 的片段（影片/黑幕交錯皆可）串接 → 疊 hook → 上字幕 → 疊角標 → 混音輸出"""
    logo_path = resolve_logo_path()
    has_logo  = logo_path is not None
    crop_scale = f"crop=ih*9/16:ih,scale={W}:{H}"

    args: list = []
    filter_parts: list[str] = []
    seg_labels: list[str] = []
    idx = 0

    for e in plan:
        if e.get("path"):
            args += ["-ss", f"{e['start']}", "-i", str(e["path"])]
            filter_parts.append(
                f"[{idx}:v]trim=duration={e['dur']:.2f},setpts=PTS-STARTPTS,{crop_scale},setsar=1[seg{idx}]"
            )
        else:
            args += ["-f", "lavfi", "-i",
                     f"color=c=black:size={W}x{H}:rate=30:duration={e['dur']:.2f}"]
            filter_parts.append(f"[{idx}:v]setsar=1[seg{idx}]")
        seg_labels.append(f"[seg{idx}]")
        idx += 1

    audio_idx = idx
    args += ["-i", str(audio_out)]
    idx += 1

    filter_parts.append("".join(seg_labels) + f"concat=n={len(seg_labels)}:v=1:a=0[vraw]")

    base_label = "vraw"
    hook_f = _hook_filter(hook)
    if hook_f:
        filter_parts.append(f"[vraw]{hook_f}[vhk]")
        base_label = "vhk"

    if has_logo:
        logo_idx = idx
        args += ["-i", str(logo_path)]
        idx += 1
        filter_parts.append(f"[{base_label}]{sub_f}[vs]")
        filter_parts.append(f"[vs][{logo_idx}:v]overlay=main_w-overlay_w-30:60[vout]")
    else:
        filter_parts.append(f"[{base_label}]{sub_f}[vout]")

    ff(
        *args,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-map", f"{audio_idx}:a",
        "-t", main_sec, "-r", "30", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-c:a", "copy",
        out
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4c：片尾
# ─────────────────────────────────────────────────────────────────────────────
def make_outro() -> Path:
    """
    片尾優先序：shorts片尾.mp4（設計師提供的動態片尾，已是 1080x1920）
    → outro.png（靜態圖備援）→ 黑底暫代。
    動態片尾一律重新編碼成跟片頭/主畫面一致的規格（30fps/yuv420p/44100Hz），
    確保最後 concat（stream copy）不會因規格不一致而出錯或音畫不同步。
    """
    out = TMP / "outro.mp4"
    if OUTRO_MP4.exists():
        dur = _probe_duration(OUTRO_MP4)
        ff(
            "-i", OUTRO_MP4,
            "-vf", f"scale={W}:{H},setsar=1",
            "-t", dur, "-r", "30", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    elif OUTRO_PNG.exists():
        ff(
            "-loop", "1", "-i", OUTRO_PNG,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]scale={W}:{H}[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", OUTRO_SEC, "-r", "30", "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    else:
        ff(
            "-f", "lavfi", "-i", f"color=c=black:size={W}x{H}:rate=30",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map", "0:v", "-map", "1:a",
            "-t", OUTRO_SEC, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Step 5：串接
# ─────────────────────────────────────────────────────────────────────────────
def concat(clips: list[Path], out: Path):
    lst = TMP / "concat.txt"
    lst.write_text(
        "\n".join(f"file '{str(c).replace(chr(92), '/')}'" for c in clips),
        encoding="utf-8"
    )
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="社會新聞短影音產製")
    parser.add_argument("--article", required=True, help="新聞稿 .txt 路徑")
    parser.add_argument("--video",   nargs="*", default=[], help="監視器影片路徑，可指定多個依序銜接")
    parser.add_argument("--start",   nargs="*", type=float, default=[], help="對應每支影片的起始秒數，缺省補 0")
    parser.add_argument("--name",    default=None,  help="輸出檔名（預設用標題）")
    parser.add_argument("--voice",   default="hsiaochen", choices=list(TTS_VOICES),
                        help="旁白聲音（預設 hsiaochen）")
    args = parser.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    article_path = Path(args.article)
    if not article_path.exists():
        sys.exit(f"❌ 找不到新聞稿：{article_path}")

    article = article_path.read_text(encoding="utf-8")
    sources = [
        {"path": v, "start": (args.start[i] if i < len(args.start) else 0)}
        for i, v in enumerate(args.video)
    ]

    print("① 生成播報腳本 (GPT-4o mini)...")
    script, _ = generate_script(article)
    print(f"   標題：{script['title']}")
    print(f"   Hook：{script.get('hook', '')}")
    print(f"   Hashtags：{' '.join(script.get('hashtags', []))}")
    print(f"   地點：{script['location']}")
    print(f"   旁白字數：{len(script['narration'])} 字")

    tts_path = TMP / "narration.mp3"
    srt_path = TMP / "subtitles.ass"

    print("② 生成旁白音訊 (edge-tts)...")
    boundaries = generate_tts(script["narration"], tts_path, voice=TTS_VOICES[args.voice])

    print("③ 寫字幕檔（逐字對齊旁白）...")
    subs = align_subtitles_to_boundaries(script["subtitles"], boundaries) if boundaries else []
    if not subs:
        subs = script["subtitles"]  # 對齊失敗退回 GPT 原時間戳
    write_ass(subs, srt_path)

    print("④ 組裝片頭 (3秒)...")
    intro = make_intro(script)

    print(f"⑤ 組裝主畫面 ({MAIN_SEC}秒)...")
    main_clip, length_info = make_main(tts_path, srt_path, sources, hook=script.get("hook"))
    if length_info["insufficient"]:
        print(f"   ⚠️ 來源影片總長度只有 {length_info['covered_sec']} 秒，"
              f"不足 {MAIN_SEC} 秒，其餘 {length_info['shortfall_sec']} 秒為黑幕空景")

    print("⑥ 組裝片尾 (5秒)...")
    outro = make_outro()

    safe_name = re.sub(r'[\\/:*?"<>|]', "-", script["title"])[:20]
    out_path = OUTPUT / (args.name or f"{safe_name}.mp4")

    print("⑦ 串接輸出...")
    concat([intro, main_clip, outro], out_path)

    # 清暫存
    for f in TMP.iterdir():
        try:
            f.unlink()
        except Exception:
            pass

    print(f"\n✅ 完成：{out_path}")
    print(f"   總長：{INTRO_SEC + MAIN_SEC + OUTRO_SEC} 秒")


if __name__ == "__main__":
    main()
