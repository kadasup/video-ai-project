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
import random
import re
import subprocess
import sys
from pathlib import Path

# ── Windows 主控台預設 cp950，含 ⚠ 等 emoji 的 print 會 UnicodeEncodeError；
#    單獨執行本檔時也把標準輸出轉 UTF-8，避免產製中途崩潰。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

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
BGM_MP3   = TEMPLATES / "bgm.mp3"     # 背景音樂（人工指定單曲，優先於嚴選庫）
BGM_DIR   = BASE / "assets" / "bgm"   # BGM 嚴選庫（每支隨機選曲，sidechain 閃避旁白）
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
TITLE_SEC  = 3.8  # 標題條疊在主畫面最前幾秒（冷開場取代片頭卡，2026-07 格式改版）
MAIN_SEC   = 45   # 主畫面目標秒數（研究：敘事型甜蜜點 30~45s，45s 後留存下滑；
                  # 60s 留給有 SOT+數據卡的大題當上限，不再是預設目標）
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


def _tts_normalize_punct(text: str) -> str:
    """
    TTS 專用標點正規化（只影響發音，不影響字幕顯示）：把會讓 edge-tts 唸得
    卡頓或怪異的裝飾性標點清掉或換成自然停頓。實測踩過：標題外框的引號、
    書名號、波浪號夾在句中會讓語音突然斷掉或怪腔。
    去標點後的中文/英數字數不變 → 時間軸對齊（用去標點字數）完全不受影響。
    """
    text = re.sub(r"[「」『』﹁﹂﹃﹄“”‘’]", "", text)      # 引號外框 → 靜音移除，句子連貫唸
    text = re.sub(r"[《〈]", "", text)                      # 書名號/篇名號開 → 移除
    text = re.sub(r"[》〉]", "、", text)                    # 書名號閉 → 頓號（並列曲目/作品分隔）
    text = re.sub(r"[～〜]", "、", text)                    # 波浪號（裝飾性連接）→ 頓號停頓
    text = re.sub(r"、+", "、", text)                       # 收斂重複頓號
    text = re.sub(r"、(?=[，。！？、：])", "", text)         # 頓號後緊接其他句讀 → 去掉頓號
    text = re.sub(r"(?<=[，。！？：])、", "", text)          # 句讀後緊接頓號 → 去掉頓號
    text = re.sub(r"^[、，]+", "", text)                    # 行首多餘停頓
    return text


# ── 數字轉中文讀法（只給 TTS 用，字幕照樣顯示阿拉伯數字）────────────────────
# edge-tts 對阿拉伯數字常逐字唸（760 唸成「七六〇」），轉成中文數字才會唸「七百六十」。
# 字幕/TTS 字數差由對齊端的比例縮放吸收（跟 apply_pronounce 同一套機制）。

_ZH_DIGITS = "零一二三四五六七八九"
_HOTLINES = {"110", "119", "113", "165", "1999", "1968", "1995"}


def _int_to_zh(s: str) -> str:
    """整數 → 中文讀法（到億；零插入、十四不唸一十四、兩百/兩千慣用）"""
    n = int(s)
    if n == 0:
        return "零"
    units4 = ["", "萬", "億", "兆"]
    parts = []
    gi = 0
    while n > 0:
        parts.append((n % 10000, units4[gi]))
        n //= 10000
        gi += 1
    out = ""
    need_zero = False
    for val, unit in reversed(parts):
        if val == 0:
            need_zero = bool(out)
            continue
        seg = ""
        u = ["", "十", "百", "千"]
        digs = [int(c) for c in str(val)]
        ln = len(digs)
        for i, dg in enumerate(digs):
            pos = ln - 1 - i
            if dg == 0:
                if seg and not seg.endswith("零") and pos > 0 and any(digs[i + 1:]):
                    seg += "零"
                continue
            seg += _ZH_DIGITS[dg] + u[pos]
        if out and (need_zero or ln < 4):
            out += "零"
        out += seg + unit
        need_zero = False
    out = re.sub(r"^一十", "十", out)                      # 14 → 十四
    out = re.sub(r"(^|零)二(百|千|萬|億)", r"\g<1>兩\g<2>", out)   # 慣用：兩百/兩千/兩萬
    return out


def _digits_to_zh(s: str) -> str:
    """逐字唸（年份/專線/長號碼）：2026 → 二零二六"""
    return "".join(_ZH_DIGITS[int(c)] for c in s)


def num2zh(text: str) -> str:
    """把文中的阿拉伯數字換成中文讀法。規則：
    百分比 → 百分之N；4 位數+年 → 逐字唸；報案/服務專線與 7 位以上長號碼 → 逐字唸；
    小數 → 整數部分讀量值＋點＋逐字；其餘一律讀量值（760 → 七百六十）。"""
    def _num(m: re.Match) -> str:
        s = m.group(0)
        tail = text[m.end(): m.end() + 1]
        if len(s) == 4 and tail == "年":
            return _digits_to_zh(s)
        if s in _HOTLINES or len(s) >= 7:
            return _digits_to_zh(s)
        if "." in s:
            a, b = s.split(".", 1)
            return _int_to_zh(a) + "點" + _digits_to_zh(b)
        return _int_to_zh(s)

    text = re.sub(r"(?<=\d),(?=\d)", "", text)   # 千分位逗號先拿掉（1,200 → 1200），否則會唸成「一,兩百」
    text = re.sub(r"(\d+(?:\.\d+)?)\s*[%％]", lambda m: "百分之" + num2zh(m.group(1)), text)
    return re.sub(r"\d+(?:\.\d+)?", _num, text)

def _client() -> AzureOpenAI:
    """延遲建立 AzureOpenAI client，讀 D:\\VideoAI\\.env 的設定。

    timeout=90／max_retries=2：實測正常一次呼叫僅約 12 秒，但偶發會遇到 API 端延遲
    尖峰（曾出現單次 800 秒才回、拖垮整個產製）。設 90 秒上限＋自動重試 2 次，讓卡住的
    呼叫快速中止改重試，把最壞情況從十幾分鐘壓到 2~3 分鐘（A/B 實測：調 reasoning_effort
    無效甚至更糟，timeout 才是對症解）。"""
    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        timeout=90.0,
        max_retries=2,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 字幕：不讓 GPT 寫字幕文字（實測 GPT 會把字幕寫成「摘要版」而不是逐字節錄，
# 導致跟旁白對不上、逐字對齊演算法失效），改成直接從旁白原文切段，保證一字不差。
# ─────────────────────────────────────────────────────────────────────────────
def _chunk_narration(text: str, max_len: int = 16) -> list[str]:
    """
    把旁白原文切成字幕片段：優先照旁白本來就有的標點（逗號/句號等）分句，
    因為每個標點本來就是 TTS 停頓的自然單位（見句號/逗號停頓規則），
    照標點切出來的字幕變化時機會剛好貼合語音實際停頓，比固定字數切更自然，
    也不會像「固定 8~12 字硬切」那樣常常把一個詞從中間切開
    （例如「報案」被切成上一句尾「報」、下一句頭「案」）。
    只有極少數單一分句本身就超長（GPT 沒照規則斷句）才退回字數硬切。
    """
    punct = set("，。！？、；：,.!?;:")
    clauses: list[str] = []
    start = 0
    for idx, ch in enumerate(text):
        if ch in punct:
            # 夾在數字中間的分隔符不是標點，切下去會把數字劈成兩張字幕：
            # 小數點/千分位（25.9、1,000）、頓號列舉（第2、3代）
            if (ch in ".,、．，" and 0 < idx < len(text) - 1
                    and text[idx - 1].isdigit() and text[idx + 1].isdigit()):
                continue
            clauses.append(text[start:idx + 1])
            start = idx + 1
    if start < len(text):
        clauses.append(text[start:])

    chunks: list[str] = []
    for clause in clauses:
        if not clause:
            continue
        if len(clause) <= max_len:
            chunks.append(clause)
        else:
            chunks.extend(_split_long_clause(clause, max_len))

    # 太短的片段（<4字）併回前一句，避免出現孤零零一兩個字的字幕
    merged: list[str] = []
    for c in chunks:
        if merged and len(c) < 4:
            merged[-1] += c
        else:
            merged.append(c)
    return merged


_OPENCC_T2S = None
_USERDICT_LOADED = False
USERDICT_FILE = BASE / "assets" / "jieba_userdict.txt"


def _load_userdict():
    """自訂詞典：報社/新聞常用詞遇到斷錯，往 assets/jieba_userdict.txt 加一行即修
    （寫繁體即可，載入時自動轉簡體餵給 jieba）"""
    global _USERDICT_LOADED
    if _USERDICT_LOADED:
        return
    _USERDICT_LOADED = True
    try:
        import jieba
        for line in USERDICT_FILE.read_text(encoding="utf-8").splitlines():
            w = line.strip()
            if w and not w.startswith("#"):
                jieba.add_word(_OPENCC_T2S.convert(w) if _OPENCC_T2S else w)
    except Exception:
        pass


def _seg_words(text: str) -> list[str] | None:
    """
    結巴斷詞（回傳原文的逐詞切分）。jieba 預設詞典是簡體，直接斷繁體會把
    「董事長」切成「董事/長」（實測翻車）——先繁→簡（OpenCC，字數 1:1）再斷，
    詞長映射回原文。不可用時回 None，呼叫端自行退回硬切。
    """
    global _OPENCC_T2S
    try:
        import jieba
        if _OPENCC_T2S is None:
            from opencc import OpenCC
            _OPENCC_T2S = OpenCC("t2s")
        _load_userdict()
        simp = _OPENCC_T2S.convert(text)
        if len(simp) != len(text):   # 極少數一對多轉換，位置對不上就直接斷原文
            simp = text
        out, pos = [], 0
        for w in jieba.cut(simp):
            out.append(text[pos:pos + len(w)])
            pos += len(w)
        # 數字/序數詞跟後面的詞黏著成一個單位（第二+航廈、760+架次）——
        # 這種組合被拆開跨卡/跨行會很難讀（「…前往第二 ‖ 航廈車道…」）。
        # 也要吃得下小數/千分位（25.9公里、1,000）：jieba 會把「25.9」切成
        # 25/./9 三塊，黏著時「25」「25.」「25.9」都得算數字才能接著黏
        glued: list[str] = []
        numish = re.compile(
            r"第?[0-9０-９零一二兩三四五六七八九十百千]+"
            r"(?:[\.．,][0-9０-９]+)*[\.．,]?$")
        for w in out:
            if glued and numish.fullmatch(glued[-1]):
                glued[-1] += w
            else:
                glued.append(w)
        return glued
    except Exception:
        return None


def _split_long_clause(clause: str, max_len: int) -> list[str]:
    """
    超長分句（GPT 沒下標點）照結巴詞界打包切卡，寧可卡短也不切開詞
    （實測踩過：「車道入口」被切成上一卡尾「…車」、下一卡頭「道入口與行李區」）。
    jieba 不可用時退回字數硬切。
    """
    words = _seg_words(clause)
    if words is None:
        return [clause[i:i + max_len] for i in range(0, len(clause), max_len)]
    out, cur = [], ""
    for w in words:
        if len(w) > max_len:            # 單詞比上限還長（幾乎不可能），退回硬切
            if cur:
                out.append(cur)
                cur = ""
            out.extend(w[i:i + max_len] for i in range(0, len(w), max_len))
            continue
        if cur and len(cur) + len(w) > max_len:
            out.append(cur)
            cur = ""
        cur += w
    if cur:
        out.append(cur)
    return out


def _build_subtitles(narration: str) -> list[dict]:
    """時間戳先給佔位值，實際起訖交給 align_subtitles_to_boundaries 用逐字時間算"""
    return [
        {"index": i, "start": "00:00:00,000", "end": "00:00:00,000", "text": c}
        for i, c in enumerate(_chunk_narration(narration), 1)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Step 1：生成腳本
# ─────────────────────────────────────────────────────────────────────────────
def generate_script(article: str, footage_notes: str | None = None,
                    interview_notes: str | None = None) -> dict:
    """
    新聞稿 → JSON 腳本（標題、地點、旁白、字幕、語意分段、SOT 原音安排）。
    footage_notes: 素材畫面清單（catalog_video 的結果彙整）。有給的話，
    旁白會在「新聞稿內容為主」的前提下貼著畫面寫（write to picture）——
    新聞稿寫了什麼、篇幅怎麼分配就照樣寫，畫面只用來決定「怎麼描述細節」，
    不能反過來讓畫面有無去裁剪或打亂新聞稿本身的內容。
    interview_notes: 素材裡的受訪原音逐字稿（Whisper 轉譯，可能有同音錯字）。
    有給的話，GPT 可以安排一段 SOT（受訪原音）插進成片——這是電視新聞的
    標準做法（旁白＋原音交替），比整支都是旁白蓋過閉麥講話臉專業得多。
    """
    footage_block = ""
    if footage_notes:
        footage_block = f"""
可用畫面素材清單（這批就是成片會用的畫面）：
{footage_notes}

⚠️ 貼著畫面寫稿（write to picture）——但新聞稿的事實跟敘事比重才是主體，畫面只是輔助：
- ⚠️ 最優先：新聞稿寫了什麼、用多少篇幅講，旁白就照樣講、照樣分配篇幅。
  不能因為某段情節沒有對應畫面就刪減、簡化或一句帶過——沒畫面就配黑幕，
  新聞內容本身不能因此縮水；也不能為了配合畫面把新聞稿原本的敘事順序打亂重排
- 在「新聞稿原本就會提到」的情節裡，如果素材剛好拍到對應畫面，用字可以貼近畫面
  的具體細節（例如新聞稿寫「消防救援」，素材有救援畫面，就可以多描述救援動作）；
  但這只是「錦上添花」，不是新增內容的理由，新聞稿沒寫的事不要因為有畫面就加進去
- segments 的 desc 直接註明建議配哪支影片的哪個時段（如「配V1的0~5秒消防救援」），
  這欄純粹給後續配對用，不影響旁白正文的取捨
- ⚠️ 這是給你內部規劃用的畫面清單，旁白正文絕對不能提到「畫面中」「這段影像」
  「另一段影像可以看到」「可以看到」這種講述影片本身的解說詞——旁白要像主播播報
  事件本身，不是在講解你看到的素材
"""

    interview_block = ""
    if interview_notes:
        interview_block = f"""
素材裡的受訪原音逐字稿（本機語音辨識，可能有同音錯字，請對照新聞稿判讀真意）：
{interview_notes}

⚠️ SOT（受訪原音）安排規則——電視新聞的標準做法是「旁白與受訪原音交替」：
- 若逐字稿裡有適合直接讓當事人/警方「自己講」的話（完整、切題、5~15 秒），
  **一定要安排成 SOT**（在輸出 JSON 加 "sots" 欄位）——受訪/講話畫面的正確用法是
  讓當事人用「原音」講這一段，而不是把講話畫面當無聲底圖鋪在旁白下面。
  逐字稿內容破碎、離題或品質差才不安排（"sots" 給空陣列）
- ⚠️ **受訪畫面只安排 1 段就好**（"sots" 最多 1 段）：同一位受訪者講一次即可，
  短影音塞多段「談話頭」會很悶。挑最切題、最完整的那一段，其餘捨棄；
  SOT 長度 5~15 秒，after_sentence 放在旁白講完「誰要說話」的引導句之後
- "quote" 必須從上面逐字稿**一字不差**複製（含錯字也照抄，程式要靠它對回時間軸）；
  "display" 是給觀眾看的字幕版本：把 quote 的同音錯字對照新聞稿改正、轉繁體、
  但不增刪語句結構
- "after_sentence"：原音要插在旁白第幾句之後（句子以句號／驚嘆號／問號計算，1-based）。
  插入點要順：通常放在旁白講完「誰說了什麼」的引導句之後（例：旁白「警方表示」→ SOT 原音）
- ⚠️ 旁白不要複述 SOT 裡講的內容（會重複）；旁白負責敘事，細節讓原音自己講
- ⚠️ 安排 SOT 時，旁白字數上限要扣掉原音時間：SOT 每秒約佔 4.3 字的額度
  （例：安排 10 秒 SOT，旁白就寫 {int(MAIN_SEC * 4.2)} 減 43 字左右）
- "speaker_name"/"speaker_title"（受訪者名條）：**只在新聞稿能明確判斷這段原音是誰講的**
  才填（例：稿裡寫「桃機公司代理董事長楊○○表示…」且原音內容與其發言吻合）。
  名字職稱照新聞稿用字。**不確定是誰講的就兩個都給空字串——名條掛錯人是重大事故，寧缺勿濫**
"""

    prompt = f"""你是台灣電視新聞影音編輯。根據以下新聞稿，產出一支 {MAIN_SEC} 秒短影音的播報腳本。
{footage_block}{interview_block}

輸出 JSON（嚴格依此格式，不要加任何說明）：
{{
  "title": "片頭標題（20 字內，吸睛但不煽情，放在片頭卡）",
  "hook": "開頭鉤子（10~14 字內，比 title 更聳動抓眼球，疊在主畫面最前 2 秒的畫面上，不會被旁白唸出來，純文字看的）",
  "hashtags": ["#關鍵字1", "#關鍵字2", "#關鍵字3", "#關鍵字4"],
  "location": "地點（縣市＋路段，10 字內）",
  "narration": "旁白全文（繁體中文，約 {int(MAIN_SEC * 4.5)} 字）",
  "segments": [
    {{"sentences": 2, "desc": "這段涵蓋的頭 2 句在講什麼（例：案發經過，嫌犯持刀進入超商）"}},
    {{"sentences": 1, "desc": "嫌犯特徵與逃逸方向"}},
    {{"sentences": 2, "desc": "警方調閱監視器追查"}}
  ],
  "sots": [
    {{"video": "V2", "quote": "從逐字稿一字不差複製的原句", "display": "校正後的繁體字幕版",
      "after_sentence": 3, "speaker_name": "楊○○", "speaker_title": "桃機公司代理董事長"}}
  ],
  "stat": {{"value": "760", "unit": "架次", "label": "航班取消", "note": "颱風影響桃園機場"}},
  "highlights": ["760架次", "全數取消", "陳彥伯"]
}}
（沒有受訪逐字稿、或沒有適合的原音時，"sots" 給空陣列 []）

"stat"（數據動態字卡）規則：
- 只在新聞裡有「一個」具衝擊力的關鍵數字時才給（金額、人數、次數、百分比等），
  成片會在旁白唸到它時插入 2.8 秒的動態數字卡強化記憶點
- value 必須與**旁白中該數字的字面寫法完全一致**（程式靠字面比對找插卡時間點）：
  旁白寫「760」就給 "760"、寫「1萬1400」就給 "1萬1400"（可含萬/億）；label 6 字內；note 12 字內可省略
- 沒有適合的數字、或數字不是新聞重點時，"stat" 給 null，寧缺勿濫

"highlights"（字幕關鍵詞標色）規則：
- 從旁白挑 3~6 個「觀眾掃一眼就該抓到」的詞：關鍵數字含單位、人名、地名、罪名、
  關鍵動作（例：肇事逃逸、全數取消）。字幕會把這些詞標成金黃色
- 每個詞 2~6 字、**必須與旁白字面完全一致**（程式靠字面比對上色）；沒有就給空陣列

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
- ⚠️ 忠於記者原稿：旁白是把新聞稿「刪減」到字數區間，不是讀完後自己重寫一篇。
  能沿用原稿的字句就照抄沿用，壓字數靠刪掉次要細節與重複資訊，不靠換句話說；
  絕不自創原稿沒有的形容詞、動詞或情境描寫。只有四種情況才改動原稿用字：
  ①書面語唸不順（括號補充、倒裝書面句、「（記者○○○報導）」這類）
  ②違反本清單其他規則（數字唸法、匿名原則、句號節奏、AI 腔）
  ③破題句需要重組出「時間＋地點＋事件」
  ④相鄰兩段被刪掉中間內容後需要最小限度的銜接詞
- 第一句先破題：時間＋地點＋發生什麼事，一句話講完
- ⚠️ 地點要完整：事發地點第一次出現時保留原稿的完整行政區（縣市＋區/鄉鎮＋路段），
  不要簡化掉縣市（例：原稿「桃園市龜山區湖山街180巷77弄口」就照寫完整，不要縮成
  「龜山區湖山街…」）。地方新聞觀眾常不知道「龜山區」在哪個縣市，掉了縣市會看不懂
- 結構：破題 → 經過細節 → 傷亡損失 → 警方處置 → 收尾（後續發展）
- ⚠️ 句號要省著用：實測 TTS 在句號／驚嘆號／問號後會停頓約 0.85 秒，逗號只停頓約 0.3 秒，
  句號斷太密旁白會一直卡頓、很不自然。每個句號之間要有 25~40 字，
  中間用逗號串 2~3 個 8~16 字的短分句帶節奏，意思完整講完才用句號收尾，
  主詞能省就省（範例：「一名男子走進店裡，突然亮出水果刀，逼店員打開收銀機。」是一句，不是三句）
- 數字用口語唸法：「三萬元」不寫「30,000元」、「七十歲」不寫「70歲」
- 平鋪直敘、不帶評論、不煽情
- 匿名原則：嫌疑人用「王姓男子」「張姓嫌犯」，不寫全名
- ⚠️ 動詞只負責準確，不要用花俏動詞硬撐力道：專業新聞旁白的力道來自「句子結構」與
  「資訊本身」，不是靠聳動的動詞。不要為了讓句子看起來有力，就把好幾個獨立事件／動作
  硬濃縮成一個花俏動詞（例：把「駕車衝撞、逆向、闖越三個路口」濃縮成「一路狂飆碾壓」），
  這樣會失去新聞語境、變成 AI 腔。寧可用平實準確的動詞，把事件一件一件交代清楚，
  也不要用一個帥氣動詞含糊帶過多個事實。
- ⚠️ 不要壓成「新聞標題式的電報體殘句」：每個逗號分句都要是主播唸得出口的完整口語，
  該有的主詞/動詞/受詞不要為了省字硬砍成兩三個字的碎片。壓字數要靠**刪掉整個次要子句**，
  不是把句子縮成報紙標題（❌「台水關水，明天改善」——沒講關什麼水、改善什麼，不是人話；
  ✅「台水已關閉水流，預計明天恢復供水」）。收尾的處置/後續句尤其容易被壓爛，要特別留意

語感範例（模仿這種台灣電視新聞口白的節奏，不要照抄內容；注意句號很省，都是用逗號把短分句串成一句）：
「台南安平一間超商，昨天下午驚傳搶案，一名王姓男子戴著口罩走進店裡，突然亮出水果刀，
逼店員打開收銀機。得手三萬元後往港濱公園方向逃逸，店員嚇得直發抖，趕緊按下警鈴。
警方調閱監視器，鎖定嫌犯特徵，正全力追緝。」

❌ 禁用的 AI 腔（出現任何一個就重寫）：
「值得注意的是」「引發外界關注」「造成社會震驚」「相關單位」「進行了…」「展開了…」
「…作業」（如「救援作業」直接寫「救援」）「此外」「同時」「隨後」開頭、
「目前」一篇超過一次、連續兩句以上用相同句式開頭

❌ 禁止解說詞（旁白正文絕對不能出現，這是在講事件不是在講影片）：
「畫面中」「畫面顯示」「這段影像」「另一段影像」「可以看到」「可見」開頭句

（字幕不用你寫，程式會直接從旁白原文切段，不用在這裡管字幕格式）

segments 規則（給後續自動選片配畫面用）：
- 把旁白依「句號／驚嘆號／問號」切出的句子分組成 3~5 段，"sentences" 是這段涵蓋幾句
  （正整數，依序消耗，不用給 start/end 秒數，程式會自動用 TTS 逐字時間精算）
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
- ⚠️ **縮短靠「刪掉整個次要子句」，不是把句子壓成殘句**：每個逗號分句仍要是主播唸得出口
  的完整口語，該有的主詞/動詞/受詞不能為了省字砍成兩三字碎片（❌「台水關水，明天改善」；
  ✅「台水已關閉水流，預計明天恢復」）。寧可整句刪掉，也不要留一句電報體標題殘句
- segments 3~5 段，"sentences" 是該段涵蓋幾句（依句號/驚嘆號/問號切句），不用給秒數
- 句號要省著用（每句號間隔 25~40 字，中間用逗號串短分句），句號斷太密 TTS 停頓會很卡
- 旁白正文不能出現「畫面中」「這段影像」「另一段影像可以看到」這種解說詞
- 若原腳本有 "sots"（受訪原音安排），原樣保留不要改；但 after_sentence 要對應
  縮寫後新旁白的句子位置，必要時調整這個數字（quote/display 不動）
- 若原腳本有 "stat"（數據卡），原樣保留；縮寫後的旁白仍要用同樣的阿拉伯數字寫法唸到該數字
- 若原腳本有 "highlights"，保留仍出現在縮寫後旁白裡的詞（字面一致），被縮掉的詞移除

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


def refine_sot(quote: str, article: str) -> tuple[dict, dict]:
    """
    自動補排 SOT 專用：把原始逐字稿引句校正成端正字幕（繁體、修同音錯字、不增刪語意），
    並從新聞稿判斷發言者姓名/職稱（判不出留空，寧缺勿掛錯人）。回 (data, usage)。
    data = {"display", "speaker_name", "speaker_title"}。
    """
    prompt = f"""以下是新聞素材裡一段受訪原音的逐字稿（本機語音辨識，可能有同音錯字、簡體）：
「{quote}」

新聞稿：
{article}

輸出 JSON：
- "display"：把上面逐字稿校正成端正的繁體中文字幕（修同音錯字、轉繁體、補合適標點），
  但不增刪原意、不改變語句結構——就是這個人實際講的話
- "speaker_name"：發言者姓名。**用「動作對應」判斷並果斷填寫**：看這段話在描述誰做的事、
  站什麼立場講，對應新聞稿裡的具名者。例如引句「我到現場發現…我緊急圍設三角錐、緊急通報」
  → 對應稿裡「獲報火速到場」的人，而不是稿裡「表示過去已如何處置」的官員。
  若有多位都到場（例：議員＋里長都火速到場），選**最主要/職銜較高的主要受訪者**（一般
  議員優先於里長）。稿裡人物與動作能對上就填、不要動不動留空；只有**完全對不上任何具名者
  才留空**（後續分鏡站會讓編輯再確認一次，掛錯可當場改）
- "speaker_title"：對應到的那個人的職稱/身分（如「桃園市議員」「里長」）；
  speaker_name 留空時這欄也留空
只回 JSON：{{"display":"...","speaker_name":"...","speaker_title":"..."}}"""
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=800,
    )
    usage = {}
    if resp.usage:
        usage = {"prompt_tokens": resp.usage.prompt_tokens,
                 "completion_tokens": resp.usage.completion_tokens,
                 "total_tokens": resp.usage.total_tokens}
    return json.loads(resp.choices[0].message.content), usage


def dedup_narration_vs_sots(narration: str,
                            sot_texts: list[str]) -> tuple[str, list[int], dict]:
    """
    旁白 vs 受訪原音（SOT）內容去重：實測 GPT 寫稿常犯「旁白先講一遍取消班次數字、
    原音又講一遍」。有重複時把該資訊留給原音講、重寫旁白（重複句改成引導句或刪除），
    並回傳每段原音改寫後的插入句位。回 (narration, after_sentences, tokens)。
    """
    sots_block = "\n".join(f"原音{i+1}：「{t}」" for i, t in enumerate(sot_texts))
    prompt = (
        "你是電視新聞編審。成片會在旁白中插入受訪原音（SOT），內容如下：\n"
        f"{sots_block}\n\n"
        f"旁白全文：\n{narration}\n\n"
        "請檢查旁白是否有句子「複述了原音要講的資訊」（同樣的數字、同一件事的細節）。\n"
        "- 有重複：把該資訊留給原音講——旁白的重複句改成不含該細節的引導句"
        "（例：「至於取消的班次數量，聽聽桃機公司怎麼說」的語感，但要自然）或直接刪除；"
        "其餘句子一字不動\n"
        "- 沒重複：narration 原樣回傳、changed 給 false\n"
        "- 同時給每段原音應插在（改寫後）旁白第幾句之後 after_sentence"
        "（句以句號/驚嘆號/問號計、1-based；引導句之後就是原音的最佳位置）\n\n"
        '回傳 JSON：{"changed": true/false, "narration": "改寫後全文", '
        '"after_sentences": [3]}'
    )
    resp = _client().chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2"),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=1500,
    )
    data = json.loads(resp.choices[0].message.content)
    usage = {}
    if resp.usage:
        usage = {"prompt_tokens": resp.usage.prompt_tokens,
                 "completion_tokens": resp.usage.completion_tokens,
                 "total_tokens": resp.usage.total_tokens}
    if not data.get("changed"):
        return narration, [], usage
    new_nar = (data.get("narration") or "").strip()
    afters = [int(x) for x in (data.get("after_sentences") or []) if str(x).isdigit()]
    if len(new_nar) < 30:   # 改寫結果異常短，視同失敗、用原稿
        return narration, [], usage
    return new_nar, afters, usage


def extract_card(narration: str) -> tuple[dict | None, dict]:
    """
    資訊卡補判（四型選一）：generate_script 沒給 stat 時，再判斷這支旁白適合哪種
    動畫資訊卡——stat 數據卡 / timeline 時序卡 / alert 警示卡 / chart 圖表卡。
    一支影片最多一張，選資訊力最強的。回傳 (card|None, tokens)。
    """
    prompt = (
        "你是新聞影音編輯。判斷以下旁白適不適合插入「一張」動畫資訊卡（四選一或不插）：\n"
        "1. stat 數據卡＝有單一衝擊性關鍵數字 →\n"
        '   {"type":"stat","value":"760","unit":"架次","label":"航班取消","note":"週六停飛"}\n'
        "2. timeline 時序卡＝事件有清楚的 3~4 步經過（案發→逃逸→落網）→\n"
        '   {"type":"timeline","title":"事件經過","steps":["23:40 酒駕連撞4車","駕駛棄車逃逸","1小時後落網"]}\n'
        "3. alert 警示卡＝颱風/豪雨/地震等警報、停班停課類訊息 →\n"
        '   {"type":"alert","icon":"颱風","headline":"海陸警報齊發","sub":"暴風圈今晚觸陸"}\n'
        "   icon 限：颱風/豪雨/雷雨/地震/火災/警報/強風/大浪/停電/低溫\n"
        "4. chart 圖表卡＝旁白出現 3 筆以上同類數字（各時段雨量、各地災情數）→\n"
        '   {"type":"chart","title":"時雨量","unit":"毫米","points":[{"label":"9時","value":58},{"label":"10時","value":96}]}\n\n'
        "規則：\n"
        "- 每張卡都要給 \"anchor\"：從旁白**原文複製**的一小段（8~14 字），卡片會在旁白唸到"
        "這句時出現——必須字面一致，程式靠它定位\n"
        "- stat 的 value 必須與旁白字面寫法完全一致（旁白寫「1萬1400」就給「1萬1400」）\n"
        "- label/title/headline ≤8 字；steps 每步 ≤12 字；sub/note ≤14 字\n"
        "- 都不適合就回 {\"card\": null}，寧缺勿濫\n\n"
        f"旁白：{narration}\n\n"
        '回傳 JSON：{"card": {...含 type 與 anchor...}} 或 {"card": null}'
    )
    resp = _client().chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2"),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=500,
    )
    data = json.loads(resp.choices[0].message.content)
    usage = {}
    if resp.usage:
        usage = {"prompt_tokens": resp.usage.prompt_tokens,
                 "completion_tokens": resp.usage.completion_tokens,
                 "total_tokens": resp.usage.total_tokens}
    card = data.get("card")
    if isinstance(card, dict) and card.get("type") in ("stat", "timeline", "alert", "chart"):
        return card, usage
    return None, usage


def factcheck_narration(article: str, narration: str) -> tuple[list[dict], list[dict], dict]:
    """
    查核 Agent：把 AI 改寫的旁白逐項比對記者原稿，抓「改寫過程改錯的關鍵事實」。
    新聞的命脈，只查會出事的硬事實（數字/地名/人名/罪名/時間/因果），
    不管文句風格。同一次呼叫順便抽出旁白裡的「外國人名/地名/組織譯名」，
    供後續比對報社音譯總表（譯名核實），不用多花一次 GPT。

    回傳 (issues, translits, tokens)：
      issues    = [{"field","narration_says","article_says","severity"}]
      translits = [{"chinese": "旁白用的譯名", "english_guess": "推測英文原名"}]
      severity: high（數字/人名/地名/罪名錯）/ medium（時序、因果、程度用詞偏差）
    """
    prompt = f"""你是嚴謹的新聞事實查核員。以下「旁白」是 AI 根據「原稿」改寫的短影音口白。

任務一：逐項比對旁白與原稿，**只抓「旁白與原稿互相衝突」的硬事實錯誤**，這是要播出去的新聞，錯了會出事：
- **type=矛盾**：旁白說的和原稿**牴觸**——數字不同（原稿1200元、旁白講12000元）、
  事實相反（原稿1死、旁白2死）、人名地名罪名張冠李戴、因果時序搞反。severity＝high。
- **type=捏造**：旁白講了原稿**完全沒有**的事實（無中生有）。severity＝high。
⚠️⚠️ 最重要的判準：**只標旁白與原稿「互相衝突」的地方。旁白比原稿少講的細節（省略）一律不要標。**
- ✅ 要標：旁白說的跟原稿**不一樣/相反**（矛盾）、原稿沒有的事**憑空冒出來**（捏造）。
- ❌ 不要標：旁白**沒提到**原稿裡的某個細節（例：原稿有罰鍰金額區間，旁白只說「將開罰」——這是省略，
  旁白並沒講錯，**不算問題、不要列**）；也不要標文句風格/順暢度、匿名改寫（張三→張姓男子）、
  數字口語化（30000→三萬）、合理精簡。
判斷口訣：**「旁白講的內容有沒有跟原稿打架？」有打架才標，只是少講不標。**

任務二：列出旁白裡出現的**外國**人名/地名/組織的中文譯名（音譯詞），
每個附上你推測的英文/原文名稱（用來查報社音譯表）。台灣本地的人名地名不要列；
沒有外國譯名就給空陣列。

原稿：
{article}

旁白：
{narration}

只回傳 JSON（沒有矛盾/捏造就給空的 issues []，不要為了湊而列省略）：
{{"issues": [
  {{"type": "矛盾", "field": "罰款金額", "narration_says": "旁白寫的內容（逐字節錄，不要加你自己的註解）", "article_says": "原稿寫的內容", "severity": "high"}}
],
"translits": [
  {{"chinese": "澤倫斯基", "english_guess": "Zelensky"}}
]}}
⚠️ narration_says 一定要是旁白裡**一字不差存在**的原文片段（給前端定位用），不要夾帶「（未交代…）」這類你的註解。
type 只有 矛盾/捏造 兩種；severity 一律 high。省略類不要回。"""

    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.2")
    resp = _client().chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_completion_tokens=1500,
    )
    usage = {}
    if resp.usage:
        usage = {"prompt_tokens": resp.usage.prompt_tokens,
                 "completion_tokens": resp.usage.completion_tokens,
                 "total_tokens": resp.usage.total_tokens}
    try:
        data = json.loads(resp.choices[0].message.content)
        issues = data.get("issues", [])
        translits = data.get("translits", [])
    except Exception:
        issues, translits = [], []
    # A 案：只保留「矛盾/捏造」的硬事實錯誤，省略（low / type=省略）一律丟掉、不呈現
    issues = [i for i in issues
              if i.get("field") and i.get("severity") in ("high", "medium")
              and i.get("type") != "省略"]
    for i in issues:
        i.setdefault("type", "矛盾")
    translits = [t for t in translits if t.get("chinese")]
    return issues, translits, usage


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
    # 標點正規化＋發音修正＋數字轉中文讀法（都只影響 TTS 發音，字幕不受影響）
    tts_text = num2zh(apply_pronounce(_tts_normalize_punct(text)))
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
    total_sub = sum(len(_clean(num2zh(apply_pronounce(s["text"])))) for s in subtitles)
    if total_sub == 0 or total_bnd == 0:
        return []
    ratio = total_bnd / total_sub

    aligned: list = []
    ci = 0.0        # 累計「應該」消耗到第幾個字元（浮點，不提前 round）
    consumed = 0
    for sub in subtitles:
        need_len = len(_clean(num2zh(apply_pronounce(sub["text"]))))
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


def shift_subtitles(subtitles: list, offset_sec: float) -> list:
    """把一批字幕的時間軸整體往後平移 offset_sec 秒（SOT 分段組裝用）"""
    if offset_sec <= 0:
        return subtitles
    return [{**s,
             "start": _sec_to_ts(_ts_to_sec(s["start"]) + offset_sec),
             "end":   _sec_to_ts(_ts_to_sec(s["end"]) + offset_sec)}
            for s in subtitles]


def build_sot_subtitles(display_text: str, start_sec: float, dur_sec: float) -> list:
    """
    SOT（受訪原音）段的字幕：display_text 是 GPT 校正過的引句，
    照標點切段後按字數比例分配到 [start_sec, start_sec+dur_sec] 區間。
    （精度 ±0.5 秒等級，對受訪字幕夠用；旁白字幕仍走逐字精算不受影響）
    """
    chunks = _chunk_narration(display_text)
    total_chars = sum(len(c) for c in chunks) or 1
    subs, t = [], start_sec
    for i, c in enumerate(chunks):
        d = dur_sec * len(c) / total_chars
        subs.append({"index": i + 1,
                     "start": _sec_to_ts(t),
                     "end": _sec_to_ts(min(t + d, start_sec + dur_sec)),
                     "text": c})
        t += d
    return subs


def extract_audio_clip(src: Path, start: float, dur: float, out: Path) -> Path:
    """從來源影片抽出指定區間的原音（SOT 用），統一轉 aac 44100 立體聲"""
    ff("-ss", f"{start:.2f}", "-i", src, "-t", f"{dur:.2f}",
       "-vn", "-c:a", "aac", "-ar", "44100", "-ac", "2", out)
    return out


def concat_audio(files: list[Path], out: Path) -> Path:
    """把多段音訊（TTS mp3 / SOT m4a 混搭）串接成單一音軌，重取樣統一規格"""
    if len(files) == 1:
        ff("-i", files[0], "-c:a", "aac", "-ar", "44100", "-ac", "2", out)
        return out
    args: list = []
    for f in files:
        args += ["-i", str(f)]
    labels = "".join(f"[{i}:a]" for i in range(len(files)))
    ff(*args, "-filter_complex",
       f"{labels}concat=n={len(files)}:v=0:a=1[a]",
       "-map", "[a]", "-c:a", "aac", "-ar", "44100", "-ac", "2", out)
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


def _split_sentences(text: str) -> list[str]:
    """依句號/驚嘆號/問號切句，標點保留在句尾；沒有句尾標點的殘餘文字併成最後一句"""
    parts = re.findall(r"[^。！？]*[。！？]|[^。！？]+$", text)
    return [p for p in parts if p]


def align_segments_to_boundaries(narration: str, segments: list, boundaries: list) -> list:
    """
    比照字幕對齊的做法：segments 不再用 GPT 猜的秒數，改用「這段涵蓋幾句」
    （sentences 計數）+ TTS 逐字時間表精算實際起訖秒數，跟 align_subtitles_to_boundaries
    共用同一套「累計消耗量統一取整數」演算法，避免捨入誤差累積。
    """
    def _clean(s: str) -> str:
        return re.sub(r"[^0-9A-Za-z一-鿿]", "", s)

    sentences = _split_sentences(narration)
    if not sentences or not boundaries or not segments:
        return []

    chars = _flatten_boundaries_to_chars(boundaries)
    total_bnd = len(chars)
    total_text = sum(len(_clean(num2zh(apply_pronounce(s)))) for s in sentences)
    if total_bnd == 0 or total_text == 0:
        return []
    ratio = total_bnd / total_text

    # GPT 給的 sentences 加總常常跟實際句數對不上（多算/少算一句很常見），
    # 先按比例normalize 到剛好等於實際句數，誤差平均分攤，不要整段被吃掉或整段多出來
    declared = [max(1, int(seg.get("sentences", 1))) for seg in segments]
    declared_total = sum(declared)
    if declared_total != len(sentences):
        scaled = [max(1, round(d * len(sentences) / declared_total)) for d in declared]
        diff = len(sentences) - sum(scaled)
        scaled[-1] = max(1, scaled[-1] + diff)
        declared = scaled

    aligned: list = []
    si = 0
    consumed = 0
    running = 0.0
    for seg, n in zip(segments, declared):
        remaining = len(sentences) - si
        if remaining <= 0:
            break
        n = min(n, remaining)
        chunk = sentences[si: si + n]
        si += n
        seg_len = sum(len(_clean(num2zh(apply_pronounce(s)))) for s in chunk)
        running += seg_len * ratio
        target = min(total_bnd, round(running))
        take = max(1, target - consumed)
        seg_chars = chars[consumed: consumed + take]
        if not seg_chars:
            break
        aligned.append({"start": round(seg_chars[0][1], 1),
                         "end": round(seg_chars[-1][2], 1),
                         "desc": seg.get("desc", "")})
        consumed += len(seg_chars)

    if not aligned:
        return []

    # GPT 給的 segments 涵蓋句數加總若少於實際句數，剩下的句子併給最後一段
    if si < len(sentences):
        remaining_chars = chars[consumed:]
        if remaining_chars:
            aligned[-1]["end"] = round(remaining_chars[-1][2], 1)

    aligned[0]["start"] = 0.0
    for i in range(len(aligned) - 1):
        aligned[i]["end"] = aligned[i + 1]["start"]
    return aligned


def rescale_segments(narration: str, segments: list, total_sec: float) -> list:
    """
    沒有 TTS 逐字時間可用時的備援：依每段涵蓋句子的字數比例分配秒數
    （比 GPT 直接猜秒數可靠，好歹跟實際文字量成正比）。
    """
    sentences = _split_sentences(narration)
    if not sentences or not segments or total_sec <= 0:
        return []

    si = 0
    parts: list[tuple[str, int]] = []
    for seg in segments:
        remaining = len(sentences) - si
        if remaining <= 0:
            break
        n = max(1, min(int(seg.get("sentences", 1)), remaining))
        chunk = sentences[si: si + n]
        si += n
        parts.append((seg.get("desc", ""), sum(len(s) for s in chunk)))
    if si < len(sentences) and parts:
        parts[-1] = (parts[-1][0], parts[-1][1] + sum(len(s) for s in sentences[si:]))
    if not parts:
        return []

    total_chars = sum(c for _, c in parts) or 1
    out = []
    t = 0.0
    for desc, clen in parts:
        dur = total_sec * clen / total_chars
        out.append({"start": round(t, 1), "end": round(min(t + dur, total_sec), 1), "desc": desc})
        t += dur
    out[-1]["end"] = round(total_sec, 1)
    return out


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


def write_ass(subtitles: list, path: Path, highlights: list | None = None) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft JhengHei,84,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,1,2,20,20,280,1
Style: Strap,Microsoft JhengHei,50,&H00FFFFFF,&H000000FF,&H99000000,&H99000000,1,0,0,0,100,100,0,0,3,10,0,1,36,36,600,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""

    MAX_CHARS = 12  # 84 級字在 1080 寬的安全上限，超過自動斷行

    _PUNCT = "，。、；：！？,.;:!?"

    def _display(text: str) -> str:
        """
        電視新聞字幕慣例：不帶標點。頭尾標點直接去掉；句中標點換半形空格
        （切段是照標點切的，句中標點只會出現在短尾併回前句的情況）。
        只影響「顯示」——時間軸對齊用的是去標點字數，本來就不受影響。
        """
        text = text.replace("\n", "")
        text = text.strip(_PUNCT)
        # 標點一律換半形空格，但「數字—分隔符—數字」的分隔符（. , 、．，）留著顯示
        # ——小數點/千分位（25.9、1,200）、頓號列舉（第2、3代）不能被拆成「第2 3代」
        midnum = set(".,、．，")
        out = []
        for i, ch in enumerate(text):
            if ch in _PUNCT:
                if (ch in midnum and 0 < i < len(text) - 1
                        and text[i - 1].isdigit() and text[i + 1].isdigit()):
                    out.append(ch)
                else:
                    out.append(" ")
            else:
                out.append(ch)
        return re.sub(r"\s+", " ", "".join(out)).strip()

    # 斷行語感：只在「詞的邊界」斷（結巴斷詞），介詞後面優先、數字絕不拆；
    # 盲目對半剁會把人名/地名切成兩行（實測：「…董事長陳／彥伯到…」「桃／園機場」）
    _BREAK_AFTER = set("的了到在與和及或並把被讓向從對為於是等後前中內外")

    def _word_bounds(text: str) -> set:
        """結巴斷詞的詞界位置集合（可斷點）；斷詞不可用時回 None 退回純規則"""
        words = _seg_words(text)
        if words is None:
            return None
        bounds, pos = set(), 0
        for w in words:
            pos += len(w)
            bounds.add(pos)
        return bounds

    def _wrap(text: str) -> str:
        if len(text) <= MAX_CHARS:
            return text
        n = len(text)
        mid = (n + 1) // 2
        lo, hi = max(2, n - MAX_CHARS), min(n - 2, MAX_CHARS)
        bounds = _word_bounds(text)
        best_j, best_score = mid, -99.0
        for j in range(lo, hi + 1):
            score = -abs(j - mid) * 0.6          # 越接近對半越好（兩行平衡）
            prev, nxt = text[j - 1], text[j]
            if bounds is not None:
                score += 2.5 if j in bounds else -2.5   # 詞界可斷、詞中間重罰
            if prev == " ":
                score += 4                        # 原本的標點位置（_display 換成空格）最優先
            elif prev in _BREAK_AFTER:
                score += 2                        # 介詞/連接詞後面是自然停頓
            if nxt in _BREAK_AFTER:
                score -= 2                        # 下一行用「的/了…」開頭很難讀
            if ((prev.isdigit() or prev in ".,、．，") and
                    (nxt.isdigit() or nxt in ".,、．，")):
                score -= 5                        # 數字絕不能拆（760→7／60；25.9→25.／9；第2、3代）
            if score > best_score:
                best_score, best_j = score, j
        a, b = text[:best_j].rstrip(), text[best_j:].lstrip()
        return a + "\\N" + b

    # ── 關鍵字標色（B 檔）：GPT 標的重點詞＋所有數字上金黃色（台灣新聞字幕慣例），
    #    字卡本身維持靜態不跳動，專業感不變、重點一眼可抓
    _HL_TERMS = [h.strip() for h in (highlights or []) if h and len(h.strip()) >= 2]
    _NUM_RE = re.compile(
        r"\d+(?:\.\d+)?[%％]?"
        r"(?:架次|人次|毫米|公里|公尺|億|萬|元|人|架|次|件|戶|棟|歲|度|時|分|秒|年|月|日|天|級)?")
    _YEL = r"{\1c&H00D7FF&}"   # 金黃（ASS 是 BGR）
    _WHI = r"{\1c&HFFFFFF&}"

    def _colorize(line: str) -> str:
        spans = []
        for t in _HL_TERMS:
            i = 0
            while True:
                i = line.find(t, i)
                if i < 0:
                    break
                spans.append((i, i + len(t)))
                i += len(t)
        for m in _NUM_RE.finditer(line):
            spans.append((m.start(), m.end()))
        if not spans:
            return line
        spans.sort()
        merged = []
        for st_, en_ in spans:
            if merged and st_ <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(en_, merged[-1][1]))
            else:
                merged.append((st_, en_))
        out, pos = [], 0
        for st_, en_ in merged:
            out.append(line[pos:st_])
            out.append(_YEL + line[st_:en_] + _WHI)
            pos = en_
        out.append(line[pos:])
        return "".join(out)

    lines = [header]
    for s in subtitles:
        start = _srt_time_to_ass(s["start"])
        end = _srt_time_to_ass(s["end"])
        if s.get("style") == "strap":
            # 受訪者名條（SOT lower-third）：左下半透明底框、不去標點不斷行
            lines.append(f"Dialogue: 1,{start},{end},Strap,,0,0,0,,{s['text']}")
            continue
        disp = _display(s["text"])
        if not disp:
            continue   # 整句只剩標點（理論上不會發生），跳過
        # 先斷行再逐行上色（色碼標籤不能被斷行邏輯當成字數）
        wrapped = "\\N".join(_colorize(seg) for seg in _wrap(disp).split("\\N"))
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{wrapped}")

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
        f"drawtext=text='{text}'{font_arg}:fontsize=38:fontcolor=0x0A1B33"
        f":x=(w-text_w)/2:y=185"
        f":box=1:boxcolor=0xFFD640@0.95:boxborderw=14"
        f":enable='between(t,0,{HOOK_SEC})'"
    )


def _title_filter(title: str | None) -> str:
    """
    冷開場的標題條：疊在主畫面最前 TITLE_SEC 秒（取代舊 3 秒片頭卡——
    研究指出 50~60% 流失發生在前 3 秒，片頭卡正好燒掉最貴的時段）。
    位置在 hook 上方，字級略小，一起構成開場資訊組。
    """
    if not title:
        return ""
    text = title.replace("'", "\\'").replace(":", "\\:")
    fp = font_path()
    font_arg = f":fontfile='{fp}'" if fp else ""
    return (
        f"drawtext=text='{text}'{font_arg}:fontsize=60:fontcolor=white"
        f":x=(w-text_w)/2:y=250:shadowcolor=black:shadowx=2:shadowy=2"
        f":box=1:boxcolor=0x0A1B33@0.88:boxborderw=18"
        f":enable='between(t,0,{TITLE_SEC})'"
    )


def _ai_label_filter() -> str:
    """AI 協作標示（NCC 廣電 AI 製播指引：AI 參與需標示），全片左上角小字"""
    fp = font_path()
    font_arg = f":fontfile='{fp}'" if fp else ""
    return (f"drawtext=text='AI協作製作'{font_arg}:fontsize=26"
            f":fontcolor=white@0.55:x=30:y=66")


# ─────────────────────────────────────────────────────────────────────────────
# Step 4b：主畫面
# ─────────────────────────────────────────────────────────────────────────────
def _sub_filter(srt: Path) -> str:
    """字幕濾鏡字串。位置/字級/顏色寫在 .ass 檔頭（write_ass），
    這裡只在有自訂字型檔時才 force_style 覆寫 FontFile。"""
    srt_esc = esc(srt)
    style = sub_style()
    return f"subtitles='{srt_esc}':force_style='{style}'" if style else f"subtitles='{srt_esc}'"


def _pick_bgm() -> Path | None:
    """選 BGM：templates/bgm.mp3（人工指定曲）優先；否則從 assets/bgm 嚴選庫隨機。
    嚴選庫是從報社 templates/背景mp3（56 首曲風很雜）挑出的新聞/紀實氛圍曲，
    要增減曲目直接對 assets/bgm 加刪檔案即可。"""
    if BGM_MP3.exists():
        return BGM_MP3
    try:
        tracks = sorted(BGM_DIR.glob("*.mp3")) + sorted(BGM_DIR.glob("*.m4a"))
    except OSError:
        tracks = []
    return random.choice(tracks) if tracks else None


def _mix_audio(tts: Path, audio_out: Path, main_sec: float):
    """
    TTS + BGM 混音，長度切齊 main_sec，結尾 1 秒淡出。
    BGM 走 sidechain 閃避：旁白/受訪原音出聲時自動壓低、空隙時浮上來，
    不再是整條固定音量硬墊。無 BGM 時只轉檔（行為與過去一致）。
    """
    bgm = _pick_bgm()
    if bgm:
        fade_st = max(0.0, float(main_sec) - 1.0)
        ff(
            "-i", tts,
            "-stream_loop", "-1", "-i", bgm,
            "-filter_complex",
            "[0:a]asplit=2[n1][n2];"
            "[1:a]volume=0.32,aformat=sample_rates=44100:channel_layouts=stereo[bg];"
            "[bg][n1]sidechaincompress=threshold=0.02:ratio=14:attack=60:release=700[duck];"
            # amix 實測仍會把輸出壓 -3dB（normalize=0 也一樣），volume 補償回原響度
            f"[n2][duck]amix=inputs=2:duration=first:normalize=0,volume=3dB,"
            f"afade=t=out:st={fade_st:.2f}:d=1[a]",
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
              main_sec: float = MAIN_SEC, hook: str | None = None,
              title: str | None = None) -> tuple[Path, dict]:
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
    _assemble_main(plan, audio_out, sub_f, out, main_sec, hook=hook, title=title)
    return out, length_info


def _photo_clip(photo: Path, dur: float, out: Path):
    """
    新聞照片 → 9:16 影片段：模糊放大的同一張照片墊滿背景、原圖置中、
    整體緩慢推近（Ken Burns），是電視新聞呈現靜態圖的標準做法，
    比黑幕或不相關空景更能撐住「有畫面感」。
    """
    frames = max(1, int(round(dur * 30)))
    ff(
        "-i", photo,
        "-filter_complex", (
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},boxblur=30:3,eq=brightness=-0.15,setsar=1[bg];"
            f"[0:v]scale={W}:-2,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[comp];"
            f"[comp]zoompan=z='min(1+0.0010*on,1.12)':d={frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30[v]"
        ),
        "-map", "[v]", "-t", f"{dur:.2f}", "-r", "30", "-pix_fmt", "yuv420p",
        "-c:v", "libx264", out
    )


def make_open_card(text: str, out: Path, kind: str = "title") -> int:
    """
    冷開場字卡（PIL 圖卡，取代陽春 drawtext）：依業界引題／主標階層設計。
    kind="title"：主標（headline）＝畫面主導元素，深藍底＋亮藍 accent、白粗體 64px；
    kind="hook"：引題（kicker）＝金黃實心小標籤＋深藍粗體字 38px，放主標上方，
    字級約為主標 6 成（業界慣例：層級靠字級差 1.5 倍以上＋形狀差，不能只靠顏色）。
    文字過寬自動縮字級。回傳卡片高度 px。
    """
    from PIL import Image, ImageDraw, ImageFont

    text = (text or "").strip()
    if kind == "hook":
        fg, fsize, fmin = (10, 27, 51, 255), 38, 28
        pad_x, accent_w, pad_v = 22, 0, 26
    else:
        fg, fsize, fmin = (255, 255, 255, 255), 64, 44
        pad_x, accent_w, pad_v = 30, 10, 42
    max_w = 960
    font = None
    while fsize >= fmin:
        font = ImageFont.truetype(r"C:\Windows\Fonts\msjhbd.ttc", fsize)
        if accent_w + pad_x * 2 + int(font.getlength(text)) <= max_w:
            break
        fsize -= 4
    tw = int(font.getlength(text))
    w = min(max_w, accent_w + pad_x * 2 + tw)
    h = fsize + pad_v
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if kind == "hook":
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=10, fill=(255, 214, 64, 245))
        d.text((pad_x, h // 2), text, font=font, fill=fg, anchor="lm")
    else:
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=14, fill=(10, 27, 51, 225))
        d.rounded_rectangle([0, 0, accent_w + 14, h - 1], radius=14, fill=(46, 139, 255, 255))
        d.rectangle([accent_w + 4, 0, accent_w + 14, h - 1], fill=(10, 27, 51, 225))
        d.text((accent_w + pad_x, h // 2), text, font=font, fill=fg, anchor="lm")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return h


def make_strap_png(name: str, title: str, out: Path) -> Path:
    """
    受訪者名條圖卡（SOT lower-third）：深藍圓角底＋亮藍色邊條，
    姓名粗體大字、職稱小字同行。輸出帶透明背景的 PNG，寬度依文字自動。
    """
    from PIL import Image, ImageDraw, ImageFont

    name = (name or "").strip()
    title = (title or "").strip()
    f_name  = ImageFont.truetype(r"C:\Windows\Fonts\msjhbd.ttc", 58)
    f_title = ImageFont.truetype(r"C:\Windows\Fonts\msjh.ttc", 38)

    pad_x, gap = 34, 26
    accent_w = 10
    h = 104
    w_name  = int(f_name.getlength(name)) if name else 0
    w_title = int(f_title.getlength(title)) if title else 0
    w = accent_w + pad_x + w_name + (gap + w_title if title else 0) + pad_x
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 底：深藍半透明圓角；左緣亮藍 accent 條
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=14, fill=(10, 27, 51, 225))
    d.rounded_rectangle([0, 0, accent_w + 14, h - 1], radius=14, fill=(46, 139, 255, 255))
    d.rectangle([accent_w + 4, 0, accent_w + 14, h - 1], fill=(10, 27, 51, 225))

    x = accent_w + pad_x
    if name:
        d.text((x, h // 2), name, font=f_name, fill=(255, 255, 255, 255), anchor="lm")
        x += w_name + gap
    if title:
        d.text((x, h // 2 + 6), title, font=f_title, fill=(159, 193, 255, 255), anchor="lm")

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def make_main_plan(tts: Path, srt: Path, plan: list[dict],
                   main_sec: float = MAIN_SEC, hook: str | None = None,
                   straps: list[dict] | None = None,
                   draft: bool = False, title: str | None = None) -> tuple[Path, dict]:
    """
    依智慧配對產生的時間軸組裝主畫面。
    plan: [{"path": str|None, "photo": str|None, "start": float, "dur": float}, ...]
          path=None 且 photo=None 代表該段配不到畫面、用黑幕。
          photo 有值代表該段用新聞照片（轉成 Ken Burns 片段）。
    hook: 疊在最前 HOOK_SEC 秒畫面上的純文字鉤子，不影響旁白/字幕。
    draft: 快速預覽模式（480p＋ultrafast），檔名分開、不干擾正式渲染。
    """
    out       = TMP / ("draft_main.mp4" if draft else "main.mp4")
    audio_out = TMP / ("draft_audio.aac" if draft else "audio.aac")
    sub_f = _sub_filter(srt)
    _mix_audio(tts, audio_out, main_sec)

    norm_plan = []
    for i, e in enumerate(plan):
        dur = float(e.get("dur", 0) or 0)
        if dur <= 0.05:
            continue
        if e.get("path"):
            norm_plan.append({"path": Path(e["path"]),
                              "start": float(e.get("start", 0) or 0), "dur": dur,
                              "subject_pos": e.get("subject_pos", "")})
        elif e.get("photo") and Path(e["photo"]).exists():
            # draft 版檔名分開：預覽渲染中途按下正式渲染也不會互寫同一檔
            clip = TMP / f"_photo_seg{'d' if draft else ''}{i}.mp4"
            _photo_clip(Path(e["photo"]), dur, clip)
            norm_plan.append({"path": clip, "start": 0.0, "dur": dur})
        else:
            norm_plan.append({"path": None, "start": 0.0, "dur": dur})
    length_info = _plan_length_info(norm_plan, main_sec)
    _assemble_main(norm_plan, audio_out, sub_f, out, main_sec, hook=hook,
                   straps=straps, draft=draft, title=title)
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


def _crop_scale(subject_pos: str = "", crop_factor: float | None = None) -> str:
    """
    16:9→9:16 直式裁切，依主體水平位置決定保留哪一側，避免主體被裁掉。
    x 偏移 = (可裁範圍) × factor：左 0.2、右 0.8、中/滿版 0.5（正中）。
    crop_factor（0~1）：若有給就用精算值（SOT 臉部置中用臉偵測算出的偏移），
    覆蓋粗略的 subject_pos。對已是 9:16 的來源（照片/黑幕）iw-cw=0，任何 factor 都得 x=0。
    """
    if isinstance(crop_factor, (int, float)):
        factor = min(1.0, max(0.0, float(crop_factor)))
    else:
        factor = {"左": 0.2, "右": 0.8}.get((subject_pos or "").strip(), 0.5)
    return f"crop=ih*9/16:ih:(iw-ih*9/16)*{factor}:0,scale={W}:{H}"


def _assemble_main(plan: list[dict], audio_out: Path, sub_f: str, out: Path,
                   main_sec: float = MAIN_SEC, hook: str | None = None,
                   straps: list[dict] | None = None, draft: bool = False,
                   title: str | None = None):
    """把 plan 的片段（影片/黑幕交錯皆可）串接 → 疊 hook → 上字幕 → 疊角標
    → 疊受訪者名條（straps: [{png,start,end}]，SOT 時段顯示）→ 混音輸出。
    draft=True 是分鏡站的快速預覽：內容完全相同（字幕/名條/數據卡/BGM 都在），
    只是縮到 480p＋最快編碼檔位，十幾秒可出——正式渲染不受影響。"""
    logo_path = resolve_logo_path()
    has_logo  = logo_path is not None

    args: list = []
    filter_parts: list[str] = []
    seg_labels: list[str] = []
    idx = 0

    for e in plan:
        if e.get("path"):
            args += ["-ss", f"{e['start']}", "-i", str(e["path"])]
            filter_parts.append(
                f"[{idx}:v]trim=duration={e['dur']:.2f},setpts=PTS-STARTPTS,"
                f"{_crop_scale(e.get('subject_pos'), e.get('crop_factor'))},setsar=1[seg{idx}]"
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
    # 冷開場字卡（標題＋Hook）：PIL 圖卡走 PNG overlay（好看、可控），
    # PIL 失敗才退回 drawtext 備援。位置從角標安全區（y≈106）之下開始排
    overlay_items: list[dict] = []   # {png,x,y,start,end}
    _pf = "d" if draft else ""
    _fallback_open = []
    y_cursor = 230   # 角標底邊 ~106；使用者指定再往下（原 130）
    # 業界階層：引題（hook，小標籤）在上、主標（title，大條）在下
    if hook:
        _hp = TMP / f"_open_hook{_pf}.png"
        try:
            hh = make_open_card(hook, _hp, kind="hook")
            overlay_items.append({"png": _hp, "x": "(main_w-overlay_w)/2",
                                  "y": str(y_cursor), "start": 0, "end": HOOK_SEC,
                                  "rise": True})
            y_cursor += hh + 12
        except Exception:
            _fallback_open.append(_hook_filter(hook))
    if title:
        _tp = TMP / f"_open_title{_pf}.png"
        try:
            make_open_card(title, _tp, kind="title")
            overlay_items.append({"png": _tp, "x": "(main_w-overlay_w)/2",
                                  "y": str(y_cursor), "start": 0, "end": TITLE_SEC,
                                  "rise": True})
        except Exception:
            _fallback_open.append(_title_filter(title))
    open_f = ",".join(f for f in _fallback_open if f)
    if open_f:
        filter_parts.append(f"[vraw]{open_f}[vhk]")
        base_label = "vhk"

    ai_f = _ai_label_filter()
    if has_logo:
        logo_idx = idx
        args += ["-i", str(logo_path)]
        idx += 1
        filter_parts.append(f"[{base_label}]{sub_f},{ai_f}[vs]")
        filter_parts.append(f"[vs][{logo_idx}:v]overlay=main_w-overlay_w-30:60[vlg]")
        cur = "vlg"
    else:
        filter_parts.append(f"[{base_label}]{sub_f},{ai_f}[vlg]")
        cur = "vlg"

    # 受訪者名條（SOT 時段、左下）＋開場字卡：統一 PNG overlay 時間窗疊加
    for stp in (straps or []):
        overlay_items.append({"png": Path(stp["png"]), "x": "40",
                              "y": "main_h-overlay_h-600",
                              "start": stp["start"], "end": stp["end"]})
    # 進出場動畫（業界規範：進場 0.35s 淡入+上浮 24px、退場 0.25s 原地淡出——
    # 退場要比進場快才不搶注意力；新聞圖卡動作要克制，不做彈跳）
    _FI, _FO = 0.35, 0.25
    for oi_n, ov in enumerate(overlay_items):
        if not Path(ov["png"]).exists():
            continue
        ov_idx = idx
        st_, en_ = float(ov["start"]), float(ov["end"])
        args += ["-loop", "1", "-t", f"{en_ + 0.3:.2f}", "-i", str(ov["png"])]
        idx += 1
        filter_parts.append(
            f"[{ov_idx}:v]format=rgba,"
            f"fade=t=in:st={st_:.2f}:d={_FI}:alpha=1,"
            f"fade=t=out:st={max(st_, en_ - _FO):.2f}:d={_FO}:alpha=1[ovp{oi_n}]")
        y_expr = ov["y"]
        if ov.get("rise"):
            y_expr = f"{ov['y']}+24*max(0,1-(t-{st_:.2f})/{_FI})"
        filter_parts.append(
            f"[{cur}][ovp{oi_n}]overlay=x='{ov['x']}':y='{y_expr}'"
            f":enable='between(t,{st_:.2f},{en_:.2f})'[vov{oi_n}]")
        cur = f"vov{oi_n}"
    if draft:   # 預覽：全部效果照舊疊完，最後縮到 480p，用最快檔位編碼
        filter_parts.append(f"[{cur}]scale=480:-2[vout]")
    else:
        filter_parts.append(f"[{cur}]null[vout]")

    enc = (["-preset", "ultrafast", "-crf", "32", "-r", "24"]
           if draft else ["-r", "30"])
    ff(
        *args,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]", "-map", f"{audio_idx}:a",
        "-t", main_sec, *enc, "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-c:a", "copy",
        out
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4c：片尾
# ─────────────────────────────────────────────────────────────────────────────
def make_outro(draft: bool = False) -> Path:
    """
    片尾優先序：shorts片尾.mp4（設計師提供的動態片尾，已是 1080x1920）
    → outro.png（靜態圖備援）→ 黑底暫代。
    動態片尾一律重新編碼成跟片頭/主畫面一致的規格，確保最後 concat（stream copy）
    不會因規格不一致而出錯或音畫不同步。
    draft=True（POC 低畫質模式）：片尾也縮到 480×854/24fps/ultrafast，跟低畫質主畫面
    規格一致，concat -c copy 才串得起來。
    """
    out = TMP / ("draft_outro.mp4" if draft else "outro.mp4")
    tw, th = (480, 854) if draft else (W, H)          # 主畫面 draft = scale=480:-2 → 480×854
    rate = "24" if draft else "30"                     # 對齊主畫面 draft 的 fps
    denc = ["-preset", "ultrafast", "-crf", "32"] if draft else []
    if OUTRO_MP4.exists():
        # 片尾是第二大流失點，壓到 2.8 秒內（取設計片尾的前段）
        dur = min(_probe_duration(OUTRO_MP4), 2.8)
        ff(
            "-i", OUTRO_MP4,
            "-vf", f"scale={tw}:{th},setsar=1",
            "-t", dur, "-r", rate, *denc, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    elif OUTRO_PNG.exists():
        ff(
            "-loop", "1", "-i", OUTRO_PNG,
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", f"[0:v]scale={tw}:{th},setsar=1[v]",
            "-map", "[v]", "-map", "1:a",
            "-t", OUTRO_SEC, "-r", rate, *denc, "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-c:a", "aac", "-ar", "44100",
            out
        )
    else:
        ff(
            "-f", "lavfi", "-i", f"color=c=black:size={tw}x{th}:rate={rate}",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-map", "0:v", "-map", "1:a",
            "-t", OUTRO_SEC, *denc, "-pix_fmt", "yuv420p",
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
